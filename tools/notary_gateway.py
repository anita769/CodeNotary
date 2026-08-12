"""CodeNotary notary tool gateway for AgentTeams.

Deterministic "notary core" exposed as an HTTP tool gateway, in the same
shape as the official OpsPilot Zero mock tool server:

    POST /tools/{scenario_id}/{tool_name}.{function_name}

Hard invariant (mirrors codenotary/state_machine.py): LLM output NEVER
drives state transitions. Workers return text/keywords/structured claims;
this gateway's deterministic code parses simple keywords, runs the gates,
and is the only component allowed to move the pipeline state machine.

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

PKG_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = PKG_ROOT / "scenarios"
TARGET_DIR = PKG_ROOT / "tools" / "notary_target"
RUNS_DIR = PKG_ROOT / "runs"
SKILL_REGISTRY_DIR = PKG_ROOT / "skills" / "registry"

# ---------------------------------------------------------------------------
# Vendored deterministic core (stdlib port of codenotary/models.py +
# codenotary/state_machine.py + codenotary/gate_protocol.recompute_score)
# ---------------------------------------------------------------------------

PIPELINE_STATES = [
    "RECEIVED", "SCREENED", "TRIAGED", "DIAGNOSED", "CONTRACTED",
    "AUTHORING", "TESTING", "GATING", "NOTARIZED", "RELEASED",
    "QUARANTINED", "ESCALATED", "REJECTED", "ROLLED_BACK",
]

_CANONICAL_ORDER = [
    "RECEIVED", "SCREENED", "TRIAGED", "DIAGNOSED", "CONTRACTED",
    "AUTHORING", "TESTING", "GATING",
]
_ADVERSARIAL_PAIR = {"AUTHORING", "TESTING"}
_REQUIRED_GREEN_GATES = {"TEST_PASS", "MUTATION", "CONVENTION"}

# Mutation score bands, mirroring codenotary.gates.mutation_gate.
_MUTATION_RED_BELOW = 0.60
_MUTATION_GREEN_ABOVE = 0.85


class IllegalTransition(Exception):
    """A state transition violates the deterministic table."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def freeze_contract(issue_id: str, assertions: list[str],
                    context_refs: list[str]) -> str:
    """Tamper-evident contract freeze: sha256 over canonical JSON."""
    canonical = json.dumps(
        {"issue_id": issue_id, "assertions": assertions,
         "context_refs": context_refs},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return sha256_text(canonical)


def recompute_score(killed: int, survived: int, exempted: int) -> float:
    """Mutation score with exempted (equivalent) mutants removed."""
    denominator = killed + survived - exempted
    if denominator <= 0:
        return 1.0
    return killed / denominator


class NotaryStateMachine:
    """Deterministic lifecycle state machine for one pipeline run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.state = "RECEIVED"
        self._visited_contracted = False
        self._green_gates: set[str] = set()
        self.history: list[dict[str, Any]] = [
            {"state": "RECEIVED", "verdict": None, "ts": time.time()}
        ]

    def _record(self, verdict: dict | None) -> None:
        self.history.append(
            {"state": self.state, "verdict": verdict, "ts": time.time()})

    def quarantine(self) -> str:
        if self.state not in ("RECEIVED", "SCREENED"):
            raise IllegalTransition(f"quarantine not valid from {self.state}")
        self.state = "QUARANTINED"
        self._record(None)
        return self.state

    def reject_early(self) -> str:
        if self.state in ("GATING", "NOTARIZED", "RELEASED"):
            raise IllegalTransition(f"reject_early not valid from {self.state}")
        self.state = "REJECTED"
        self._record(None)
        return self.state

    def advance_to(self, target: str) -> str:
        cur = self.state
        if cur in _ADVERSARIAL_PAIR and target in _ADVERSARIAL_PAIR:
            pass  # adversarial loop: both directions allowed
        else:
            if cur not in _CANONICAL_ORDER or target not in _CANONICAL_ORDER:
                raise IllegalTransition(
                    f"advance_to({target}) not on canonical path from {cur}")
            if _CANONICAL_ORDER.index(target) <= _CANONICAL_ORDER.index(cur):
                raise IllegalTransition(
                    f"advance_to({target}) is not forward from {cur}")
            if target == "GATING" and not self._visited_contracted:
                raise IllegalTransition(
                    "advance_to(GATING) requires having passed CONTRACTED")
        self.state = target
        if target == "CONTRACTED":
            self._visited_contracted = True
        self._record(None)
        return self.state

    def apply_verdict(self, verdict: dict) -> str:
        """Consume a gate verdict; only valid in GATING.

        red -> REJECTED, yellow -> ESCALATED, green recorded; when
        TEST_PASS+MUTATION+CONVENTION are all green -> NOTARIZED.
        """
        if self.state != "GATING":
            raise IllegalTransition(
                f"apply_verdict only valid in GATING, not {self.state}")
        decision = verdict["decision"]
        if decision == "red":
            self.state = "REJECTED"
        elif decision == "yellow":
            self.state = "ESCALATED"
        else:
            if verdict["gate"] not in self._green_gates:
                self._green_gates.add(verdict["gate"])
                if _REQUIRED_GREEN_GATES <= self._green_gates:
                    self.state = "NOTARIZED"
        self._record(verdict)
        return self.state

    def resolve_human(self, approve: bool) -> str:
        if self.state != "ESCALATED":
            raise IllegalTransition(
                f"resolve_human only valid in ESCALATED, not {self.state}")
        self.state = "GATING" if approve else "REJECTED"
        self._record(None)
        return self.state

    def release(self) -> str:
        if self.state != "NOTARIZED":
            raise IllegalTransition(f"release requires NOTARIZED, not {self.state}")
        self.state = "RELEASED"
        self._record(None)
        return self.state

    def rollback(self) -> str:
        if self.state != "RELEASED":
            raise IllegalTransition(f"rollback requires RELEASED, not {self.state}")
        self.state = "ROLLED_BACK"
        self._record(None)
        return self.state


# ---------------------------------------------------------------------------
# Deterministic scanners / gates
# ---------------------------------------------------------------------------

# (pattern, severity, label). critical -> quarantine; high -> finding.
_SENTINEL_PATTERNS: list[tuple[str, str, str]] = [
    (r"\beval\s*\(", "critical", "dynamic eval on external input"),
    (r"\bexec\s*\(", "critical", "dynamic exec on external input"),
    (r"pickle\.loads\s*\(", "critical", "unsafe deserialization"),
    (r"os\.system\s*\(", "high", "shell invocation via os.system"),
    (r"subprocess\.[a-z]+\([^)]*shell\s*=\s*True", "high",
     "subprocess with shell=True"),
    (r"(?i)(api[_-]?key|secret|password|token)\s*=\s*[\"'][^\"']{6,}[\"']",
     "high", "suspected hardcoded credential"),
    (r"sk-[A-Za-z0-9\-]{8,}", "high", "suspected hardcoded API key"),
    (r"open\s*\([^)]*[\"'][wax]\+?[\"']", "high",
     "unaudited file write (potential data exfiltration)"),
]

# Each operator occurrence yields one mutant per replacement candidate.
_OPERATOR_MUTANTS = {
    ">": [">=", "==", "!=", "<"],
    ">=": [">", "=="],
    "<": ["<=", "==", "!=", ">"],
    "<=": ["<", "=="],
    "==": ["!="],
    "!=": ["=="],
}
_OPERATOR_RE = re.compile(r"(?<!-)(>=|<=|==|!=|>|<)")
_MAX_MUTANTS = 12
_TEST_TIMEOUT_S = 30


def sentinel_scan(files: dict[str, str]) -> dict[str, Any]:
    """Deterministic quarantine scan over change files."""
    findings: list[dict[str, str]] = []
    for path, content in files.items():
        for pattern, severity, label in _SENTINEL_PATTERNS:
            for m in re.finditer(pattern, content):
                line = content[:m.start()].count("\n") + 1
                findings.append({
                    "file": path, "line": line, "severity": severity,
                    "label": label, "match": m.group(0)[:80],
                })
    decision = "quarantine" if any(
        f["severity"] == "critical" for f in findings) else "pass"
    return {"decision": decision, "findings": findings}


def _write_workdir(workdir: Path, sources: dict[str, str],
                   test_files: dict[str, str]) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    for name, content in sources.items():
        (workdir / Path(name).name).write_text(content)
    for name, content in test_files.items():
        (workdir / Path(name).name).write_text(content)


def _run_unittest(workdir: Path) -> dict[str, Any]:
    """Run unittest discovery in workdir; return parsed result."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover",
             "-s", str(workdir), "-p", "test_*.py"],
            capture_output=True, text=True, timeout=_TEST_TIMEOUT_S,
            cwd=str(workdir),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "timed_out": True, "output": "test run timed out"}
    output = (proc.stdout + proc.stderr).strip()
    ran = re.search(r"Ran (\d+) tests?", output)
    tail = "\n".join(output.splitlines()[-40:])
    return {
        "ok": proc.returncode == 0,
        "timed_out": False,
        "tests_ran": int(ran.group(1)) if ran else 0,
        "output": tail,
    }


def _code_lines(source: str) -> list[bool]:
    """Mark which lines are executable code (not comments/docstrings).

    Small state machine over triple-quoted strings; good enough for the
    vendored target service files. Mutating comment/docstring text yields
    non-executable "equivalent" mutants that only add noise.
    """
    in_string: str | None = None
    mask: list[bool] = []
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        code = True
        if in_string:
            code = False
            if in_string in stripped:
                # closing delimiter; rest of line after it counts as code
                in_string = None
        elif stripped.startswith("#"):
            code = False
        elif stripped.startswith(('"""', "'''")):
            quote = stripped[:3]
            # single-line docstring: opens and closes on the same line
            if stripped.count(quote) >= 2 and len(stripped) > 3:
                code = False
            else:
                in_string = quote
                code = False
        mask.append(code)
    return mask


def generate_mutants(source: str) -> list[dict[str, str]]:
    """Mutants per comparison-operator occurrence (deterministic order).

    Only executable code lines are mutated; comments and docstrings are
    skipped so every mutant is semantically meaningful.
    """
    mutants: list[dict[str, str]] = []
    lines = source.splitlines(keepends=True)
    code_mask = _code_lines(source)
    for lineno, line in enumerate(lines, start=1):
        if not code_mask[lineno - 1]:
            continue
        for m in _OPERATOR_RE.finditer(line):
            for replacement in _OPERATOR_MUTANTS[m.group(0)]:
                mutated = line[:m.start()] + replacement + line[m.end():]
                mutant_source = "".join(
                    mutated if i == lineno else l
                    for i, l in enumerate(lines, 1))
                mutants.append({
                    "id": f"M{len(mutants) + 1:02d}",
                    "line": lineno,
                    "mutation": f"{m.group(0)} -> {replacement}",
                    "source": mutant_source,
                })
                if len(mutants) >= _MAX_MUTANTS:
                    return mutants
    return mutants


def convention_check(files: dict[str, str], in_scope: list[str],
                     out_of_scope: list[str]) -> dict[str, Any]:
    """Deterministic style-fingerprint / scope checks on the change."""
    findings: list[dict[str, str]] = []
    scope = set(in_scope) or {"queue_box.py"}
    for path in files:
        if Path(path).name not in {Path(s).name for s in scope}:
            findings.append({"file": path, "severity": "veto",
                             "rule": "diff-scope-discipline",
                             "detail": "file outside contract in_scope"})
    for path in out_of_scope:
        if path in files:
            findings.append({"file": path, "severity": "veto",
                             "rule": "diff-scope-discipline",
                             "detail": "file listed in contract out_of_scope"})
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if re.search(r"except\s*:", line):
                findings.append({"file": path, "line": i, "severity": "veto",
                                 "rule": "exception-handling-convention",
                                 "detail": "bare except"})
            elif re.search(r"except\s+Exception\b", line):
                findings.append({"file": path, "line": i, "severity": "warning",
                                 "rule": "exception-handling-convention",
                                 "detail": "over-broad except Exception"})
            if len(line) > 100:
                findings.append({"file": path, "line": i, "severity": "warning",
                                 "rule": "style-fingerprint",
                                 "detail": "line longer than 100 chars"})
        for pattern, _sev, label in _SENTINEL_PATTERNS:
            if re.search(pattern, content):
                findings.append({"file": path, "severity": "veto",
                                 "rule": "input-validation-injection",
                                 "detail": f"security pattern: {label}"})
    decision = "red" if any(f["severity"] == "veto" for f in findings) else "green"
    return {"decision": decision, "findings": findings}


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

class NotaryRun:
    """All per-run state held by the gateway (the deterministic core)."""

    def __init__(self, scenario_id: str) -> None:
        fixture_path = SCENARIOS_DIR / f"{scenario_id}.json"
        if not fixture_path.exists():
            raise KeyError(
                f"unknown scenario '{scenario_id}'; available: {list_scenarios()}")
        self.fixture = json.loads(fixture_path.read_text())
        self.scenario_id = scenario_id
        self.mode = self.fixture["mode"]
        self.sm = NotaryStateMachine(scenario_id)
        self.run_dir = RUNS_DIR / scenario_id
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        (self.run_dir / "verdicts").mkdir(parents=True)
        (self.run_dir / "evidence" / "quarantine").mkdir(parents=True)
        self.contract: dict[str, Any] | None = None
        self.diagnosis: dict[str, Any] | None = None
        self.implementation: dict[str, str] = {}
        self.tests: dict[str, str] = {}
        self.mutation: dict[str, Any] | None = None
        self.rebuttals: list[dict[str, Any]] = []
        self.trace_path = self.run_dir / "trace.jsonl"
        self._write_json(self.run_dir / "issue.json", self.fixture["issue"])
        if self.mode == "external":
            # External AI change: files enter via sentinel quarantine, not
            # via the author worker.
            self.implementation = dict(self.fixture["submitted_change"]["files"])

    # -- persistence helpers -------------------------------------------------

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def log(self, tool: str, payload: Any, result: Any, ms: float) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "run_id": self.scenario_id,
            "tool": tool,
            "state_after": self.sm.state,
            "payload_sha256": sha256_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True))[:16],
            "result_sha256": sha256_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True,
                           default=str))[:16],
            "duration_ms": round(ms, 1),
        }
        with self.trace_path.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def record_verdict(self, gate: str, decision: str, summary: str,
                       extra: dict[str, Any]) -> dict[str, Any]:
        verdict = {
            "gate": gate,
            "decision": decision,
            "summary": summary,
            "issued_by": "notary-gateway (deterministic)",
            "timestamp": time.time(),
            **extra,
        }
        self._write_json(self.run_dir / "verdicts" / f"{gate.lower()}.json",
                         verdict)
        applied = None
        if self.sm.state == "GATING":
            applied = self.sm.apply_verdict(verdict)
        verdict_out = dict(verdict)
        verdict_out["pipeline_state"] = self.sm.state
        if applied is None and self.sm.state != "GATING":
            verdict_out["note"] = (
                f"verdict recorded for evidence; pipeline already "
                f"{self.sm.state}, state machine untouched")
        return verdict_out

    # -- shared context helpers ----------------------------------------------

    def baseline_tests(self) -> dict[str, str]:
        names = self.fixture.get("baseline_test_files") or [
            self.fixture["baseline_test_file"]]
        return {name: (TARGET_DIR / name).read_text() for name in names}

    def target_source(self) -> dict[str, str]:
        sources = {name: (TARGET_DIR / name).read_text()
                   for name in self.fixture["target_files"]}
        sources.update(self.fixture.get("source_overrides", {}))
        return sources

    def effective_sources(self) -> dict[str, str]:
        """Target sources overlaid with the submitted implementation."""
        return {**self.target_source(), **run_impl_files(self)}

    def contract_or_error(self) -> dict[str, Any]:
        if self.contract is None:
            raise ValueError(
                "contract not frozen yet; freeze it via notary_contract.freeze")
        return self.contract


RUNS: dict[str, NotaryRun] = {}


def get_run(scenario_id: str) -> NotaryRun:
    if scenario_id not in RUNS:
        RUNS[scenario_id] = NotaryRun(scenario_id)
    return RUNS[scenario_id]


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# Tool implementations. Each returns a JSON-serialisable dict.
# ---------------------------------------------------------------------------

def t_get_issue(run: NotaryRun, _p: dict) -> dict:
    return {"mode": run.mode, "issue": run.fixture["issue"],
            "pipeline_state": run.sm.state}


def t_get_source(run: NotaryRun, _p: dict) -> dict:
    return {"files": run.target_source()}


def t_get_baseline_tests(run: NotaryRun, _p: dict) -> dict:
    return {"files": run.baseline_tests()}


def t_get_submitted_change(run: NotaryRun, _p: dict) -> dict:
    if run.mode != "external":
        raise ValueError("scenario is inhouse mode; no external change")
    return {"submitted_change": run.fixture["submitted_change"]}


def t_sentinel_scan(run: NotaryRun, _p: dict) -> dict:
    files = (run.implementation if run.mode == "external"
             else run.target_source())
    result = sentinel_scan(files)
    manifest = {
        "run_id": run.scenario_id,
        "scanned_files": sorted(files),
        "file_sha256": {k: sha256_text(v) for k, v in files.items()},
        "findings": result["findings"],
        "decision": result["decision"],
        "status": "yellow" if result["findings"] else "green",
    }
    run._write_json(
        run.run_dir / "evidence" / "quarantine" / "manifest.json", manifest)
    if result["decision"] == "quarantine":
        run.sm.quarantine()
    elif run.sm.state == "RECEIVED":
        run.sm.advance_to("SCREENED")
    manifest["pipeline_state"] = run.sm.state
    return manifest


def t_triage(run: NotaryRun, p: dict) -> dict:
    verdict = str(p.get("verdict", "")).strip().lower()
    if verdict not in ("accept", "reject", "escalate"):
        raise ValueError("verdict must be one of: accept / reject / escalate")
    record = {"verdict": verdict, "scope": p.get("scope", []),
              "route": p.get("route", []), "rationale": p.get("rationale", "")}
    run._write_json(run.run_dir / "triage.json", record)
    if verdict == "accept":
        run.sm.advance_to("TRIAGED")
    elif verdict == "reject":
        run.sm.reject_early()
    else:
        run.sm.state = "ESCALATED"
        run.sm._record(None)
    record["pipeline_state"] = run.sm.state
    return record


def t_diagnosis(run: NotaryRun, p: dict) -> dict:
    required = ["root_cause", "evidence", "fix_hypothesis", "confidence"]
    missing = [k for k in required if k not in p]
    if missing:
        raise ValueError(f"diagnosis missing fields: {missing}")
    run.diagnosis = {k: p[k] for k in required}
    if "repro" in p:
        run.diagnosis["repro"] = p["repro"]
    run._write_json(run.run_dir / "diagnosis.json", run.diagnosis)
    run.sm.advance_to("DIAGNOSED")
    return {"stored": True, "pipeline_state": run.sm.state}


def t_reproduce(run: NotaryRun, _p: dict) -> dict:
    """Read-only diagnostic: reproduce the planted bug on current source."""
    workdir = run.run_dir / "work" / "repro"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    for name, content in run.target_source().items():
        (workdir / Path(name).name).write_text(content)
    repro = run.fixture.get("repro_snippet") or (
        "from queue_box import Mailbox\n"
        "box = Mailbox()\n"
        "try:\n"
        "    box.pop()\n"
        "except IndexError as exc:\n"
        "    print(f'IndexError: {exc}')\n"
    )
    (workdir / "repro.py").write_text(repro)
    proc = subprocess.run([sys.executable, "repro.py"], capture_output=True,
                          text=True, timeout=_TEST_TIMEOUT_S, cwd=str(workdir))
    return {"command": "python3 repro.py  # scenario repro snippet",
            "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(),
            "exit_code": proc.returncode}


def t_freeze_contract(run: NotaryRun, p: dict) -> dict:
    assertions = p.get("assertions") or []
    if not assertions or not all(isinstance(a, str) and len(a) >= 8
                                 for a in assertions):
        raise ValueError("assertions must be a non-empty list of verifiable "
                         "plain-text statements (>= 8 chars each)")
    in_scope = p.get("in_scope") or run.fixture["target_files"]
    out_of_scope = p.get("out_of_scope") or []
    contract_hash = freeze_contract(
        run.fixture["issue"]["id"], assertions,
        [str(run.run_dir / "diagnosis.json")])
    run.contract = {
        "issue_id": run.fixture["issue"]["id"],
        "assertions": assertions,
        "in_scope": in_scope,
        "out_of_scope": out_of_scope,
        "blind_partitions": {
            "author_sees": ["contract", "target source", "diagnosis"],
            "tester_sees": ["contract", "baseline public tests"],
        },
        "frozen_hash": contract_hash,
    }
    run._write_json(run.run_dir / "contract.json", run.contract)
    run.sm.advance_to("CONTRACTED")
    return {"frozen_hash": contract_hash, "pipeline_state": run.sm.state}


def t_author_context(run: NotaryRun, _p: dict) -> dict:
    """Author partition: contract + source + diagnosis. NEVER tester tests."""
    return {
        "contract": run.contract_or_error(),
        "diagnosis": run.diagnosis,
        "source": run.target_source(),
        "blind_notice": "tester workspace is not visible to you; do not "
                        "ask for or speculate about blind tests",
    }


def t_tester_context(run: NotaryRun, _p: dict) -> dict:
    """Tester partition: contract + baseline public tests. NEVER the fix."""
    return {
        "contract": run.contract_or_error(),
        "baseline_tests": run.baseline_tests(),
        "blind_notice": "author implementation is not visible to you; write "
                        "tests from the contract alone",
    }


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _check_filenames(files: dict[str, str]) -> None:
    for name in files:
        if not _SAFE_NAME_RE.match(name) or name.startswith("."):
            raise ValueError(f"unsafe file name: {name!r}")


def run_impl_files(run: NotaryRun) -> dict[str, str]:
    return {k: v for k, v in run.implementation.items() if k.endswith(".py")}


def t_submit_implementation(run: NotaryRun, p: dict) -> dict:
    if run.mode == "external":
        raise ValueError("external mode: implementation arrives via "
                         "submitted_change, not the author worker")
    files = p.get("files") or {}
    if not any(name.endswith(".py") for name in files):
        raise ValueError("implementation must include at least one .py file")
    _check_filenames(files)
    run.implementation = dict(files)
    run._write_json(run.run_dir / "evidence" / "implementation_files.json",
                    {k: sha256_text(v) for k, v in files.items()})
    part = run.run_dir / "work" / "author_wt"
    part.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (part / name).write_text(content)
    if run.sm.state == "CONTRACTED":
        run.sm.advance_to("AUTHORING")
    return {"stored": sorted(files), "pipeline_state": run.sm.state}


def t_submit_tests(run: NotaryRun, p: dict) -> dict:
    files = p.get("files") or {}
    if not any(name.startswith("test_") and name.endswith(".py")
               for name in files):
        raise ValueError("tests must include at least one test_*.py file")
    _check_filenames(files)
    run.tests = dict(files)
    run._write_json(run.run_dir / "evidence" / "blind_test_files.json",
                    {k: sha256_text(v) for k, v in files.items()})
    part = run.run_dir / "work" / "tester_wt"
    part.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (part / name).write_text(content)
    if run.sm.state == "CONTRACTED":
        run.sm.advance_to("AUTHORING")
    if run.sm.state == "AUTHORING":
        run.sm.advance_to("TESTING")
    return {"stored": sorted(files), "pipeline_state": run.sm.state}


def _require_change_and_tests(run: NotaryRun) -> None:
    if not run.implementation:
        raise ValueError("no implementation submitted yet")
    if not tests_partition(run):
        raise ValueError("no blind tests submitted yet")


def tests_partition(run: NotaryRun) -> dict[str, str]:
    return {**run.baseline_tests(), **run.tests}


def t_run_test_gate(run: NotaryRun, _p: dict) -> dict:
    _require_change_and_tests(run)
    if run.sm.state in ("AUTHORING", "TESTING"):
        run.sm.advance_to("GATING")
    workdir = run.run_dir / "work" / "test_gate"
    _write_workdir(workdir, run.effective_sources(), tests_partition(run))
    res = _run_unittest(workdir)
    decision = "green" if res["ok"] else "red"
    summary = (f"baseline+blind tests: {res.get('tests_ran', 0)} run, "
               f"{'all passed' if res['ok'] else 'failures present'}")
    return run.record_verdict("TEST_PASS", decision, summary,
                              {"test_output": res["output"]})


def t_run_mutation_gate(run: NotaryRun, _p: dict) -> dict:
    _require_change_and_tests(run)
    if run.sm.state in ("AUTHORING", "TESTING"):
        run.sm.advance_to("GATING")
    source_files = run_impl_files(run)
    mutants: list[dict[str, str]] = []
    for fname, fsource in source_files.items():
        for m in generate_mutants(fsource):
            m["file"] = fname
            mutants.append(m)
    # Re-id deterministically across files, cap total.
    mutants = mutants[:_MAX_MUTANTS]
    for i, m in enumerate(mutants, 1):
        m["id"] = f"M{i:02d}"
    killed, survived, invalid = [], [], []
    for mutant in mutants:
        workdir = run.run_dir / "work" / "mutation" / mutant["id"]
        sources = run.effective_sources()
        sources[mutant["file"]] = mutant["source"]
        try:
            _write_workdir(workdir, sources, tests_partition(run))
        except OSError as exc:
            invalid.append({**{k: mutant[k] for k in ("id", "file", "line", "mutation")},
                            "reason": str(exc)})
            continue
        res = _run_unittest(workdir)
        entry = {k: mutant[k] for k in ("id", "file", "line", "mutation")}
        if "SyntaxError" in res["output"]:
            invalid.append({**entry, "reason": "mutant does not compile"})
        elif res["ok"]:
            survived.append(entry)
        else:
            killed.append(entry)
    score = recompute_score(len(killed), len(survived), 0)
    run.mutation = {"killed": killed, "survived": survived,
                    "invalid": invalid, "score": score, "exempted": 0,
                    "awaiting_rebuttal": bool(survived)
                    and score < _MUTATION_GREEN_ABOVE}
    survivors_md = ["# Mutation survivors\n"]
    for s in survived:
        survivors_md.append(f"- {s['id']} line {s['line']}: {s['mutation']}")
    (run.run_dir / "survivors.md").write_text("\n".join(survivors_md) + "\n")
    out = {"mutants_total": len(mutants), "killed": len(killed),
           "survived": survived, "invalid": len(invalid),
           "score": round(score, 4),
           "bands": {"red_below": _MUTATION_RED_BELOW,
                     "green_above": _MUTATION_GREEN_ABOVE}}
    if run.mutation["awaiting_rebuttal"]:
        out["status"] = ("awaiting_rebuttal: survivors sent to author; "
                         "finalize via notary_gate.finalize_mutation")
    else:
        verdict = run.record_verdict(
            "MUTATION",
            "green" if score > _MUTATION_GREEN_ABOVE else
            "yellow" if score >= _MUTATION_RED_BELOW else "red",
            f"mutation score {score:.2f} "
            f"({len(killed)} killed, {len(survived)} survived)", out)
        out["verdict"] = verdict
    return out


def t_get_survivors(run: NotaryRun, _p: dict) -> dict:
    if not run.mutation:
        raise ValueError("mutation gate has not run yet")
    return {"survivors": run.mutation["survived"],
            "awaiting_rebuttal": run.mutation["awaiting_rebuttal"]}


def t_rebuttal(run: NotaryRun, p: dict) -> dict:
    if not run.mutation or not run.mutation["awaiting_rebuttal"]:
        raise ValueError("no mutation survivors awaiting rebuttal")
    kind = str(p.get("kind", "")).strip()
    if kind not in ("accept_fix", "equivalent_mutant", "dispute"):
        raise ValueError(
            "kind must be one of: accept_fix / equivalent_mutant / dispute")
    mutant_id = str(p.get("mutant_id", "")).strip()
    valid_ids = {s["id"] for s in run.mutation["survived"]}
    if mutant_id not in valid_ids:
        raise ValueError(
            f"unknown survivor mutant_id {mutant_id!r}; "
            f"valid: {sorted(valid_ids)}")
    if any(r["mutant_id"] == mutant_id for r in run.rebuttals):
        raise ValueError(
            f"mutant {mutant_id} already has a rebuttal; one per survivor")
    if not str(p.get("justification", "")).strip():
        raise ValueError("justification must be non-empty")
    entry = {"mutant_id": mutant_id, "kind": kind,
             "justification": p["justification"].strip()}
    run.rebuttals.append(entry)
    run._write_json(run.run_dir / "rebuttals.json", run.rebuttals)
    return {"recorded": entry, "round": len(run.rebuttals)}


def t_finalize_mutation(run: NotaryRun, _p: dict) -> dict:
    if not run.mutation:
        raise ValueError("mutation gate has not run yet")
    if not run.mutation["awaiting_rebuttal"]:
        return {"note": "mutation verdict already applied",
                "pipeline_state": run.sm.state}
    survivors = run.mutation["survived"]
    survivor_ids = {s["id"] for s in survivors}
    # Only rebuttals targeting actual survivors count; submission-time
    # validation already enforces this, the filter is belt-and-braces.
    valid = [r for r in run.rebuttals if r["mutant_id"] in survivor_ids]
    if any(r["kind"] == "dispute" for r in valid):
        verdict = run.record_verdict(
            "MUTATION", "yellow",
            "author disputed survivor classification; human review required",
            {"rebuttals": valid})
        run.mutation["awaiting_rebuttal"] = False
        return {"verdict": verdict}
    exempted_ids = {r["mutant_id"] for r in valid
                    if r["kind"] == "equivalent_mutant"}
    fix_required_ids = {r["mutant_id"] for r in valid
                        if r["kind"] == "accept_fix"}
    # Unrebutted and accept_fix survivors remain real survivors; only
    # equivalent-mutant exemptions leave the denominator.
    surviving_after = len(survivor_ids - exempted_ids)
    killed = len(run.mutation["killed"])
    score = recompute_score(killed, surviving_after, 0)
    run.mutation["awaiting_rebuttal"] = False
    if fix_required_ids:
        decision, note = "yellow", (
            f"author accepted fix for {sorted(fix_required_ids)}; "
            "resubmission required before green")
    else:
        decision = ("green" if score > _MUTATION_GREEN_ABOVE else
                    "yellow" if score >= _MUTATION_RED_BELOW else "red")
        note = ""
    verdict = run.record_verdict(
        "MUTATION", decision,
        f"mutation score after rebuttal {score:.2f} "
        f"({killed} killed, {surviving_after} survived, "
        f"{len(exempted_ids)} exempted)",
        {"killed": killed, "survived_after_rebuttal": surviving_after,
         "exempted": len(exempted_ids), "score": round(score, 4),
         "note": note, "rebuttals": valid})
    return {"verdict": verdict}


def t_run_convention_gate(run: NotaryRun, _p: dict) -> dict:
    _require_change_and_tests(run)
    if run.sm.state in ("AUTHORING", "TESTING"):
        run.sm.advance_to("GATING")
    contract = run.contract_or_error()
    result = convention_check(run.implementation, contract["in_scope"],
                              contract["out_of_scope"])
    veto = sum(1 for f in result["findings"] if f["severity"] == "veto")
    return run.record_verdict(
        "CONVENTION", result["decision"],
        f"{len(result['findings'])} findings ({veto} veto-class)",
        {"findings": result["findings"]})


def t_list_verdicts(run: NotaryRun, _p: dict) -> dict:
    verdicts = {}
    vdir = run.run_dir / "verdicts"
    for path in sorted(vdir.glob("*.json")):
        verdicts[path.stem] = json.loads(path.read_text())
    return {"verdicts": verdicts, "pipeline_state": run.sm.state,
            "history_len": len(run.sm.history)}


def t_state(run: NotaryRun, _p: dict) -> dict:
    return {"run_id": run.scenario_id, "state": run.sm.state,
            "green_gates": sorted(run.sm._green_gates),
            "history": run.sm.history}


def t_resolve_human(run: NotaryRun, p: dict) -> dict:
    approve = bool(p.get("approve"))
    state = run.sm.resolve_human(approve)
    return {"approved": approve, "pipeline_state": state}


def t_deploy(run: NotaryRun, p: dict) -> dict:
    if run.sm.state != "NOTARIZED":
        raise IllegalTransition(
            f"deploy refused: pipeline is {run.sm.state}, not NOTARIZED")
    prod = run.run_dir / "prod"
    backup = run.run_dir / "prod_backup"
    if prod.exists():
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(prod, backup)
        shutil.rmtree(prod)
    prod.mkdir(parents=True)
    sources = run.effective_sources()
    _write_workdir(prod, sources, tests_partition(run))
    res = _run_unittest(prod)
    if not res["ok"]:
        raise RuntimeError(f"post-deploy smoke failed: {res['output']}")
    state = run.sm.release()
    log = {"version": p.get("version", "v0.1.0"), "deployed_to": str(prod),
           "smoke": {"tests_ran": res.get("tests_ran", 0), "ok": res["ok"]},
           "pipeline_state": state, "ts": time.time()}
    run._write_json(run.run_dir / "evidence" / "release_log.json", log)
    return log


def t_rollback(run: NotaryRun, _p: dict) -> dict:
    state = run.sm.rollback()
    prod = run.run_dir / "prod"
    backup = run.run_dir / "prod_backup"
    restored = False
    if backup.exists():
        shutil.rmtree(prod)
        shutil.copytree(backup, prod)
        restored = True
    log = {"pipeline_state": state, "restored_backup": restored,
           "ts": time.time()}
    run._write_json(run.run_dir / "evidence" / "rollback_log.json", log)
    return log


def t_seal(run: NotaryRun, _p: dict) -> dict:
    manifest: dict[str, str] = {}
    for path in sorted(run.run_dir.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts \
                and path.name != "manifest.json":
            manifest[str(path.relative_to(run.run_dir))] = sha256_text(
                path.read_bytes().decode("utf-8", errors="replace"))
    run._write_json(run.run_dir / "manifest.json", manifest)
    return {"sealed_files": len(manifest),
            "manifest": str(run.run_dir / "manifest.json"),
            "pipeline_state": run.sm.state}


def t_list_evidence(run: NotaryRun, _p: dict) -> dict:
    files = [str(p.relative_to(run.run_dir))
             for p in sorted(run.run_dir.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts]
    return {"run_dir": str(run.run_dir), "artifacts": files}


def t_register_skill(run: NotaryRun, p: dict) -> dict:
    name = str(p.get("name", "")).strip()
    content = str(p.get("content", "")).strip()
    if not _SAFE_NAME_RE.match(name) or not content:
        raise ValueError("skill name must be safe and content non-empty")
    SKILL_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    target = SKILL_REGISTRY_DIR / f"{name}.md"
    if target.exists():
        raise ValueError(f"skill '{name}' already registered; registry is "
                         f"append-only — distill increments under a new name")
    header = (f"---\nname: {name}\ndescription: distilled from run "
              f"{run.scenario_id}\n---\n\n")
    target.write_text(header + content + "\n")
    run._write_json(run.run_dir / "evidence" / "postmortem_skill.json",
                    {"registered": name, "path": str(target)})
    return {"registered": name, "path": str(target)}


# ---------------------------------------------------------------------------
# HTTP dispatch
# ---------------------------------------------------------------------------

TOOLS: dict[str, Callable[[NotaryRun, dict], Any]] = {
    "notary_change.get_issue": t_get_issue,
    "notary_change.get_submitted_change": t_get_submitted_change,
    "notary_repo.get_source": t_get_source,
    "notary_repo.get_baseline_tests": t_get_baseline_tests,
    "notary_sentinel.scan": t_sentinel_scan,
    "notary_flow.triage": t_triage,
    "notary_flow.diagnosis": t_diagnosis,
    "notary_flow.reproduce": t_reproduce,
    "notary_contract.freeze": t_freeze_contract,
    "notary_author.get_context": t_author_context,
    "notary_author.submit_implementation": t_submit_implementation,
    "notary_tester.get_context": t_tester_context,
    "notary_tester.submit_tests": t_submit_tests,
    "notary_gate.run_test_gate": t_run_test_gate,
    "notary_gate.run_mutation_gate": t_run_mutation_gate,
    "notary_gate.get_survivors": t_get_survivors,
    "notary_gate.finalize_mutation": t_finalize_mutation,
    "notary_gate.run_convention_gate": t_run_convention_gate,
    "notary_rebuttal.submit": t_rebuttal,
    "notary_verdicts.list": t_list_verdicts,
    "notary_state.get": t_state,
    "notary_flow.resolve_human": t_resolve_human,
    "notary_release.deploy": t_deploy,
    "notary_release.rollback": t_rollback,
    "notary_evidence.seal": t_seal,
    "notary_evidence.list": t_list_evidence,
    "notary_skill.register": t_register_skill,
}


class NotaryHandler(BaseHTTPRequestHandler):
    server_version = "CodeNotaryGateway/0.5"

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parts = [unquote(p) for p in
                 urlparse(self.path).path.strip("/").split("/") if p]
        try:
            if parts == ["health"]:
                self._send(HTTPStatus.OK,
                           {"ok": True, "service": "codenotary-tool-gateway"})
                return
            if parts == ["scenarios"]:
                self._send(HTTPStatus.OK,
                           {"ok": True, "result": list_scenarios()})
                return
            if len(parts) == 3 and parts[0] == "tools" and parts[2] == "trace":
                run = get_run(parts[1])
                trace = (run.trace_path.read_text()
                         if run.trace_path.exists() else "")
                self._send(HTTPStatus.OK, {"ok": True, "result": trace})
                return
            self._send(HTTPStatus.NOT_FOUND,
                       {"ok": False, "error": "unknown endpoint"})
        except Exception as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        parts = [unquote(p) for p in
                 urlparse(self.path).path.strip("/").split("/") if p]
        started = time.monotonic()
        run: NotaryRun | None = None
        tool_call = ""
        payload: dict[str, Any] = {}
        try:
            if len(parts) != 3 or parts[0] != "tools":
                self._send(HTTPStatus.NOT_FOUND, {
                    "ok": False,
                    "error": "expected /tools/{scenario_id}/{tool_call}"})
                return
            scenario_id, tool_call = parts[1], parts[2]
            payload = self._read_json()
            if tool_call == "reset":
                RUNS.pop(scenario_id, None)
                result: Any = {"scenario_id": scenario_id, "status": "reset"}
            else:
                if tool_call not in TOOLS:
                    raise ValueError(
                        f"unknown tool call '{tool_call}', available: "
                        + ", ".join(sorted(TOOLS)))
                run = get_run(scenario_id)
                result = TOOLS[tool_call](run, payload)
            self._send(HTTPStatus.OK, {"ok": True, "result": result})
        except (IllegalTransition, ValueError, KeyError, RuntimeError) as exc:
            result = {"error": str(exc)}
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - gateway must not die on one call
            result = {"error": f"internal: {exc}"}
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"ok": False, "error": f"internal: {exc}"})
        if run is not None and tool_call:
            ms = (time.monotonic() - started) * 1000
            try:
                run.log(tool_call, payload, result, ms)
            except OSError:
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the CodeNotary HTTP notary tool gateway.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=18090, type=int)
    args = parser.parse_args()

    SKILL_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), NotaryHandler)
    print(f"CodeNotary notary tool gateway listening on "
          f"http://{args.host}:{args.port}")
    print("Health: GET /health")
    print("Tool call: POST /tools/{scenario_id}/{tool_call}")
    server.serve_forever()


if __name__ == "__main__":
    main()
