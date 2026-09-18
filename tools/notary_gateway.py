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
SKILLS_DIR = PKG_ROOT / "skills"
SKILL_REGISTRY_DIR = SKILLS_DIR / "registry"
SKILL_INDEX_PATH = SKILL_REGISTRY_DIR / "index.json"
MATCH_TABLE_PATH = SKILLS_DIR / "match_table.json"

# Gateway version. Skill frontmatter `compat: gateway>=X.Y` clauses are
# checked against this at load time; incompatible skills are REJECTED with
# an explicit reason (fail-closed), never silently skipped.
GATEWAY_VERSION = "0.8.0"

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

# ---------------------------------------------------------------------------
# Team policy (notary.json): every tunable a adopting team may configure.
# Loaded at startup; an unreadable or unknown-key config REFUSES startup
# (fail-closed — a misconfigured notary is worse than a stopped one).
# Defaults below are exactly the v1.1 hardcoded behavior, so replay serves
# as the backward-compatibility proof.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "mutation": {"red_below": 0.60, "green_above": 0.85, "max_mutants": 12},
    "test_timeout_s": 30,
    "rework": {"default_max_rounds": 2},
    # 人工门③：skill 治理。probation = 新注册进试用区（可被 match、结果
    # 标注试用、看板追认转正/一票否决）；strict = 未追认不可被 match。
    "skill_governance": "probation",
    "convention": {"max_line_length": 100, "rules": {
        "diff-scope-discipline": True,
        "exception-handling-convention": True,
        "style-fingerprint": True,
        "input-validation-injection": True,
        "resource-acquisition-without-release": True,
    }},
}
CONFIG: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))


def _merge_config(cfg: dict, user: dict, prefix: str = "") -> None:
    for key, value in user.items():
        if key not in cfg:
            raise SystemExit(
                f"notary.json: unknown key '{prefix}{key}' (typo safety; "
                f"valid keys: {sorted(cfg)})")
        if isinstance(cfg[key], dict):
            if not isinstance(value, dict):
                raise SystemExit(f"notary.json: '{prefix}{key}' must be an object")
            _merge_config(cfg[key], value, prefix + key + ".")
        else:
            # bool is a subclass of int in Python — never allow bool/int mixups
            if isinstance(value, bool) != isinstance(cfg[key], bool) or \
                    not isinstance(value, type(cfg[key])):
                raise SystemExit(
                    f"notary.json: '{prefix}{key}' must be "
                    f"{type(cfg[key]).__name__}")
            cfg[key] = value


def load_config(path: Path) -> None:
    if not path.exists():
        return  # defaults == v1.1 hardcoded behavior
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"notary.json unreadable: {exc} — refusing to start (fail-closed)")
    if not isinstance(user, dict):
        raise SystemExit("notary.json must be a JSON object")
    _merge_config(CONFIG, user)
    if CONFIG["skill_governance"] not in ("probation", "strict"):
        raise SystemExit(
            f"notary.json: skill_governance must be 'probation' or 'strict', "
            f"got {CONFIG['skill_governance']!r}")


class IllegalTransition(Exception):
    """A state transition violates the deterministic table."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def freeze_contract(issue_id: str, assertions: list[str],
                    context_refs: list[str],
                    assumptions: list[dict[str, str]] | None = None) -> str:
    """Tamper-evident contract freeze: sha256 over canonical JSON.

    `assumptions` (ambiguity point / default reading / basis) enter the
    hash only when non-empty: an assumptions-free contract keeps the v1.1
    canonical form byte-identical, so every previously frozen hash and
    sealed evidence pack still recomputes. Once assumptions exist they
    are frozen — silently editing a recorded assumption changes the hash.
    """
    canonical_obj: dict[str, Any] = {
        "issue_id": issue_id, "assertions": assertions,
        "context_refs": context_refs}
    if assumptions:
        canonical_obj["assumptions"] = assumptions
    canonical = json.dumps(
        canonical_obj, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"))
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

    def rework(self) -> str:
        """Rejected -> AUTHORING: bounded rework loop (L4).

        A rejection concludes one notarization attempt, not the issue. The
        same issue may be re-delivered after rework — but accumulated green
        gates belong to the previous attempt and must not bleed into the
        new one, so they are cleared here.
        """
        if self.state != "REJECTED":
            raise IllegalTransition(f"rework not valid from {self.state}")
        self._green_gates = set()
        self.state = "AUTHORING"
        self._record(None)
        return self.state

    def revise_contract(self) -> str:
        """ESCALATED -> CONTRACTED: contract revision (L3 third exit).

        When escalation reveals the contract itself was ambiguous, the way
        forward is a revised contract, not a verdict on the old one. Green
        gates earned under the old contract are void.
        """
        if self.state != "ESCALATED":
            raise IllegalTransition(
                f"revise_contract not valid from {self.state}")
        self._green_gates = set()
        self.state = "CONTRACTED"
        self._record(None)
        return self.state

    def dispute(self) -> str:
        """REJECTED -> ESCALATED: formal dispute of the verdict's basis.

        A red-gate rejection is mechanically correct UNDER the frozen
        contract; disputing it does not retry the gate, it challenges the
        contract clause's requirement basis. The pipeline stops and waits
        for a human adjudicator — ESCALATED is a designed pause, not an
        error. Green gates are kept: adjudication that upholds the
        contract leaves the attempt concluded, and adjudication that
        revises the contract voids them in revise_contract().
        """
        if self.state != "REJECTED":
            raise IllegalTransition(
                f"dispute not valid from {self.state}")
        self.state = "ESCALATED"
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
# Checkpoint resume verification (three-way: state / contract hash / history)
#
# Verification MUST NOT share code with what it verifies, so the transition
# legality below is an independent re-statement of NotaryStateMachine's
# rules — if the two ever disagree, that is itself a finding.
# ---------------------------------------------------------------------------

_RESUME_EXPLICIT_EDGES = {
    ("RECEIVED", "QUARANTINED"), ("SCREENED", "QUARANTINED"),
    ("GATING", "REJECTED"), ("GATING", "ESCALATED"), ("GATING", "NOTARIZED"),
    ("ESCALATED", "GATING"), ("ESCALATED", "CONTRACTED"),
    ("REJECTED", "AUTHORING"), ("REJECTED", "ESCALATED"),
    ("NOTARIZED", "RELEASED"), ("RELEASED", "ROLLED_BACK"),
}


def _legal_resume_edge(prev: str, cur: str) -> bool:
    if prev == cur:
        # A green verdict in GATING records history without leaving GATING.
        return prev == "GATING"
    if prev in _ADVERSARIAL_PAIR and cur in _ADVERSARIAL_PAIR:
        return True  # author/tester adversarial loop, both directions
    if (prev, cur) in _RESUME_EXPLICIT_EDGES:
        return True
    if prev in _CANONICAL_ORDER and cur in _CANONICAL_ORDER:
        return _CANONICAL_ORDER.index(cur) > _CANONICAL_ORDER.index(prev)
    if cur == "REJECTED":
        # reject_early: any pre-gating state; resolve_human reject: ESCALATED.
        return prev not in ("GATING", "NOTARIZED", "RELEASED")
    if cur == "ESCALATED":
        # triage escalate: forbidden only from terminal-ish states.
        return prev not in _TRIAGE_ESCALATE_FORBIDDEN
    return False


def verify_checkpoint(run_dir: Path, data: dict[str, Any]) -> list[str]:
    """Three-way consistency check, run on EVERY resume.

    Judges asked: "when you resume, how do you know the version is the
    right one?" The answer, made structural here:

      1. state    — sm.state is a known state and equals the history tail;
                    the visited_contracted flag agrees with the history.
      2. contract — if the run passed CONTRACTED, a contract exists and its
                    frozen_hash recomputes exactly; the on-disk contract.json
                    (evidence) must equal the checkpointed contract.
      3. history  — every recorded transition is a legal edge under an
                    independent re-statement of the state machine table.

    Returns a list of failure reasons; empty means three-way consistent.
    A failed checkpoint is NEVER resumed — replay from trace remains the
    proof path.
    """
    failures: list[str] = []
    sm = data.get("sm") or {}
    state = sm.get("state")
    history = sm.get("history") or []
    green = set(sm.get("green_gates") or [])
    visited = bool(sm.get("visited_contracted"))

    # -- 1. state ------------------------------------------------------------
    if state not in PIPELINE_STATES:
        failures.append(f"state: unknown state {state!r}")
    if not history:
        failures.append("state: empty history")
    else:
        if history[0].get("state") != "RECEIVED":
            failures.append("state: history does not start at RECEIVED")
        if history[-1].get("state") != state:
            failures.append(
                f"state: head {state!r} != history tail "
                f"{history[-1].get('state')!r}")
        if visited != any(h.get("state") == "CONTRACTED" for h in history):
            failures.append(
                "state: visited_contracted flag disagrees with history")
    if not green <= _REQUIRED_GREEN_GATES:
        failures.append(f"state: unknown green gates {sorted(green)}")
    if state in ("NOTARIZED", "RELEASED", "ROLLED_BACK") \
            and green != _REQUIRED_GREEN_GATES:
        failures.append(f"state: {state} without all gates green")

    # -- 2. contract ----------------------------------------------------------
    contract = data.get("contract")
    if visited and contract is None:
        failures.append(
            "contract: run passed CONTRACTED but checkpoint has no contract")
    if contract is not None:
        recomputed = freeze_contract(
            contract.get("issue_id", ""), contract.get("assertions") or [],
            contract.get("context_refs")
            or [str(run_dir / "diagnosis.json")],  # legacy checkpoints
            contract.get("assumptions") or None)
        if recomputed != contract.get("frozen_hash"):
            failures.append(
                "contract: frozen_hash does not recompute (content tampered "
                "or wrong version)")
        disk_contract = run_dir / "contract.json"
        if disk_contract.exists():
            try:
                on_disk = json.loads(disk_contract.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                on_disk = None
            if on_disk != contract:
                failures.append(
                    "contract: checkpoint disagrees with on-disk "
                    "contract.json (evidence)")

    # -- 3. history -----------------------------------------------------------
    for i in range(1, len(history)):
        prev, cur = history[i - 1].get("state"), history[i].get("state")
        if cur not in PIPELINE_STATES:
            failures.append(f"history: entry {i} has unknown state {cur!r}")
        elif not _legal_resume_edge(prev, cur):
            failures.append(
                f"history: illegal transition {prev} -> {cur} at entry {i}")
    # -- 4. evidence seal (if this run was sealed) ---------------------------
    # Judges asked: "when you resume, how do you know the code, the rules
    # and the evidence are the same versions?" Checks 1-3 cover state and
    # rules (contract hash recomputes); this check covers the evidence:
    # every sealed file must re-hash byte-identical, and the append-only
    # trace must still match its sealed prefix. A run that was adjudicated
    # into a contract revision is re-sealed at freeze time (t_freeze_
    # contract), so a legitimate v1.1 never trips this check.
    manifest = None
    try:
        manifest = json.loads(
            (run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = None
    manifest = manifest or {}
    sealed = manifest.get("files")
    if isinstance(sealed, dict) and sealed:
        for rel, want in sorted(sealed.items()):
            fp = run_dir / rel
            try:
                got = sha256_text(
                    fp.read_bytes().decode("utf-8", errors="replace"))
            except OSError:
                failures.append(f"evidence: sealed file missing {rel}")
                continue
            if got != want:
                failures.append(
                    f"evidence: sealed file modified after seal: {rel}")
        tp = manifest.get("trace_prefix") or {}
        n_lines, want_prefix = tp.get("lines"), tp.get("sha256")
        if n_lines and want_prefix:
            try:
                lines = (run_dir / "trace.jsonl").read_text(
                    encoding="utf-8").splitlines(keepends=True)
            except OSError:
                lines = []
            if len(lines) < n_lines or sha256_text(
                    "".join(lines[:n_lines])) != want_prefix:
                failures.append("evidence: trace prefix seal mismatch "
                                "(append-only log was altered)")
    return failures


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

# Custom-target upload ("自带代码送修"): intake-time strict quarantine.
# Curated fixtures only *find* most of these patterns; customer-uploaded
# code, however, WILL be executed in the sealed runner — so for custom
# targets they are reject-on-sight. Pattern set distilled from the
# OpenClaw threat-library candidates (validated zero-FP across the full
# 36-sample evalset on 2026-08-29; see tools/openclaw_threat_map.json).
_CUSTOM_QUARANTINE_PATTERNS: list[tuple[str, str]] = [
    (r"\beval\s*\(", "dynamic eval"),
    (r"\bexec\s*\(", "dynamic exec"),
    (r"pickle\.loads\s*\(", "unsafe deserialization"),
    (r"\bmarshal\.loads\s*\(", "unsafe deserialization variant"),
    (r"\bos\.system\s*\(", "shell invocation via os.system"),
    (r"subprocess\.[a-z]+\([^)]*shell\s*=\s*True", "subprocess with shell=True"),
    (r"\bsocket\.socket\s*\(", "raw network socket"),
    (r"\burllib\.request|\bhttp\.client", "network channel"),
    (r"\bctypes\b", "dynamic native library loading"),
    (r"__import__\s*\(", "dynamic import"),
    (r"\bimportlib\b", "dynamic import machinery"),
    (r"\bshutil\.rmtree\s*\(|\bos\.remove\s*\(|\bos\.unlink\s*\(",
     "destructive file operation"),
    (r"/dev/tcp", "reverse shell primitive"),
    (r"\bsudo\b", "privilege escalation attempt"),
    (r"\.ssh/|id_rsa|id_ed25519", "SSH credential access"),
    (r"crontab|\.bashrc|\.bash_profile|\.zshrc",
     "persistence via shell profile/cron"),
]

_CUSTOM_MAX_FILES = 10
_CUSTOM_MAX_BYTES = 200_000

# Sandboxed execution preamble for customer-uploaded code (custom_target
# fixtures only): hard resource ceilings, pure stdlib, applied in the
# child process before any uploaded statement runs.
_SANDBOX_PREAMBLE = (
    "import resource\n"
    "resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))\n"
    "resource.setrlimit(resource.RLIMIT_CPU, (60, 60))\n"
    "resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n"
)

_SANDBOX_MAIN = _SANDBOX_PREAMBLE + (
    "import sys, unittest\n"
    "suite = unittest.TestLoader().discover('.')\n"
    "result = unittest.TextTestRunner(verbosity=1).run(suite)\n"
    "sys.exit(0 if result.wasSuccessful() else 1)\n"
)


def custom_quarantine_scan(files: dict[str, str]) -> list[dict[str, Any]]:
    """Reject-on-sight scan for customer-uploaded code (intake-time)."""
    violations: list[dict[str, Any]] = []
    for path, content in files.items():
        for pattern, label in _CUSTOM_QUARANTINE_PATTERNS:
            for m in re.finditer(pattern, content):
                line = content[:m.start()].count("\n") + 1
                violations.append({"file": path, "line": line, "label": label,
                                   "match": m.group(0)[:60]})
    return violations


# Each operator occurrence yields one mutant per replacement candidate.
_OPERATOR_MUTANTS = {    ">": [">=", "==", "!=", "<"],
    ">=": [">", "=="],
    "<": ["<=", "==", "!=", ">"],
    "<=": ["<", "=="],
    "==": ["!="],
    "!=": ["=="],
}
_OPERATOR_RE = re.compile(r"(?<!-)(>=|<=|==|!=|>|<)")


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
        (workdir / Path(name).name).write_text(content, encoding="utf-8")
    for name, content in test_files.items():
        (workdir / Path(name).name).write_text(content, encoding="utf-8")


def _run_unittest(workdir: Path, sandbox: bool = False) -> dict[str, Any]:
    """Run unittest discovery in workdir; return parsed result.

    sandbox=True wraps the run in the rlimit preamble (customer-uploaded
    code paths only — curated fixtures keep the plain invocation).
    """
    argv = [sys.executable, "-m", "unittest", "discover",
            "-s", str(workdir), "-p", "test_*.py"]
    if sandbox:
        (workdir / "_sandbox_main.py").write_text(_SANDBOX_MAIN, encoding="utf-8")
        argv = [sys.executable, "_sandbox_main.py"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=CONFIG["test_timeout_s"],
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
                if len(mutants) >= CONFIG["mutation"]["max_mutants"]:
                    return mutants
    return mutants


def convention_check(files: dict[str, str], in_scope: list[str],
                     out_of_scope: list[str]) -> dict[str, Any]:
    """Deterministic style-fingerprint / scope checks on the change.

    Each rule family can be toggled per team in notary.json
    (convention.rules.<rule>); the line-length budget is likewise
    configurable (convention.max_line_length).
    """
    rules = CONFIG["convention"]["rules"]
    max_line = CONFIG["convention"]["max_line_length"]
    findings: list[dict[str, str]] = []
    scope = set(in_scope) or {"queue_box.py"}
    if rules.get("diff-scope-discipline", True):
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
        if rules.get("exception-handling-convention", True):
            for i, line in enumerate(content.splitlines(), 1):
                if re.search(r"except\s*:", line):
                    findings.append({"file": path, "line": i, "severity": "veto",
                                     "rule": "exception-handling-convention",
                                     "detail": "bare except"})
                elif re.search(r"except\s+Exception\b", line):
                    findings.append({"file": path, "line": i, "severity": "warning",
                                     "rule": "exception-handling-convention",
                                     "detail": "over-broad except Exception"})
        if rules.get("style-fingerprint", True):
            for i, line in enumerate(content.splitlines(), 1):
                if len(line) > max_line:
                    findings.append({"file": path, "line": i, "severity": "warning",
                                     "rule": "style-fingerprint",
                                     "detail": f"line longer than {max_line} chars"})
        if rules.get("input-validation-injection", True):
            for pattern, _sev, label in _SENTINEL_PATTERNS:
                if re.search(pattern, content):
                    findings.append({"file": path, "severity": "veto",
                                     "rule": "input-validation-injection",
                                     "detail": f"security pattern: {label}"})
        # CWE-772: resource acquired but never released. A bare open()
        # (not under a `with` statement) in a file that never calls
        # .close() leaks the handle on the success path — invisible to
        # functional tests (cf. openai/openai-python#2708).
        if rules.get("resource-acquisition-without-release", True) \
                and re.search(r"\bopen\s*\(", content):
            non_ctx = [
                i for i, line in enumerate(content.splitlines(), 1)
                if re.search(r"\bopen\s*\(", line) and "with" not in line
            ]
            if non_ctx and ".close(" not in content:
                for i in non_ctx:
                    findings.append({
                        "file": path, "line": i, "severity": "veto",
                        "rule": "resource-acquisition-without-release",
                        "detail": "open() outside `with` and no .close() "
                                  "in file (CWE-772 resource leak)"})
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
        self.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.scenario_id = scenario_id
        self.mode = self.fixture["mode"]
        self.sm = NotaryStateMachine(scenario_id)
        self.run_dir = RUNS_DIR / scenario_id
        if self.run_dir.exists():
            # fail-closed：目录里已有证据时绝不允许静默抹除。
            # 只有显式 reset（本进程生命周期内）才授权清目录——
            # 实证 0918：陈旧 checkpoint 恢复失败 → run 不在内存 →
            # 一次 skill.match 调用就把已发布 run 的整目录 rmtree 了
            if scenario_id not in _RESET_OK:
                raise RuntimeError(
                    f"run dir '{scenario_id}' already contains evidence; "
                    f"refusing to wipe. Call reset first (explicit), or "
                    f"restart the gateway to resume from checkpoint.")
            _RESET_OK.discard(scenario_id)
            shutil.rmtree(self.run_dir)
        (self.run_dir / "verdicts").mkdir(parents=True)
        (self.run_dir / "evidence" / "quarantine").mkdir(parents=True)
        self.contract: dict[str, Any] | None = None
        self.diagnosis: dict[str, Any] | None = None
        self.implementation: dict[str, str] = {}
        self.tests: dict[str, str] = {}
        self.mutation: dict[str, Any] | None = None
        self.rebuttals: list[dict[str, Any]] = []
        self.security_events: list[dict[str, Any]] = []
        # Bounded rework loop (L4): how many times this issue has been
        # re-delivered after a REJECTED verdict. Budget is fixture-tunable.
        self.rework_round = 0
        self.max_rework_rounds = int(self.fixture.get(
            "max_rework_rounds", CONFIG["rework"]["default_max_rounds"]))
        self.trace_path = self.run_dir / "trace.jsonl"
        self._write_json(self.run_dir / "issue.json", self.fixture["issue"])
        if self.mode == "external":
            # External AI change: files enter via sentinel quarantine, not
            # via the author worker.
            self.implementation = dict(self.fixture["submitted_change"]["files"])

    # -- persistence helpers -------------------------------------------------

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- checkpoint / resume --------------------------------------------------
    #
    # The gateway's run state lives in memory BY DESIGN (replay-from-trace
    # is always possible), but "restart loses everything" is not an
    # acceptable operational property. After every tool call the full run
    # state is checkpointed to runs/<sid>/checkpoint.json; on startup the
    # gateway restores every unfinished run. Replay and checkpoint are
    # complementary: checkpoint is the fast path (resume), replay is the
    # proof path (deterministic recomputation). Every restore passes
    # verify_checkpoint() first — a checkpoint whose state, contract hash
    # and history do not agree three-way is refused, never trusted.

    def checkpoint(self) -> None:
        data = {
            "scenario_id": self.scenario_id,
            "sm": {
                "state": self.sm.state,
                "visited_contracted": self.sm._visited_contracted,
                "green_gates": sorted(self.sm._green_gates),
                "history": self.sm.history,
            },
            "contract": self.contract,
            "diagnosis": self.diagnosis,
            "implementation": self.implementation,
            "tests": self.tests,
            "mutation": self.mutation,
            "rebuttals": self.rebuttals,
            "security_events": self.security_events,
            "rework_round": self.rework_round,
            "max_rework_rounds": self.max_rework_rounds,
        }
        # Atomic write: tmp + rename, so a crash mid-write can never leave
        # a truncated checkpoint.json that restore() would have to reject.
        tmp = self.run_dir / "checkpoint.json.tmp"
        self._write_json(tmp, data)
        tmp.replace(self.run_dir / "checkpoint.json")

    @classmethod
    def restore(cls, run_dir: Path) -> "NotaryRun | None":
        cp = run_dir / "checkpoint.json"
        if not cp.exists():
            return None
        data = json.loads(cp.read_text(encoding="utf-8"))
        scenario_id = data["scenario_id"]
        fixture_path = SCENARIOS_DIR / f"{scenario_id}.json"
        if not fixture_path.exists():
            return None
        # Every resume is verified three-way (state / contract / history).
        # A checkpoint that fails is refused, loudly — replay from trace
        # remains possible, silent trust of a stale or tampered checkpoint
        # is not.
        failures = verify_checkpoint(run_dir, data)
        _append_resume_log(run_dir, scenario_id, data, failures)
        if failures:
            print(f"[resume] {scenario_id}: checkpoint verification FAILED — "
                  f"{'; '.join(failures)}; run NOT resumed "
                  f"(replay from trace remains possible)", flush=True)
            return None
        self = cls.__new__(cls)
        self.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.scenario_id = scenario_id
        self.mode = self.fixture["mode"]
        sm = NotaryStateMachine(scenario_id)
        sm.state = data["sm"]["state"]
        sm._visited_contracted = data["sm"]["visited_contracted"]
        sm._green_gates = set(data["sm"]["green_gates"])
        sm.history = data["sm"]["history"]
        self.sm = sm
        self.run_dir = run_dir
        self.contract = data["contract"]
        self.diagnosis = data["diagnosis"]
        self.implementation = data["implementation"]
        self.tests = data["tests"]
        self.mutation = data["mutation"]
        self.rebuttals = data["rebuttals"]
        self.security_events = data["security_events"]
        self.rework_round = data["rework_round"]
        self.max_rework_rounds = data["max_rework_rounds"]
        self.trace_path = run_dir / "trace.jsonl"
        contract_note = (f"{self.contract['frozen_hash'][:12]}…"
                         if self.contract else "not frozen")
        sealed = {}
        try:
            sealed = (json.loads((run_dir / "manifest.json")
                                 .read_text(encoding="utf-8"))
                      .get("files") or {})
        except (OSError, json.JSONDecodeError):
            pass
        if sealed:
            seal_note = (f"evidence={len(sealed)} sealed files + "
                         f"trace prefix ✓ (4-way consistent)")
        else:
            seal_note = "(3-way consistent)"
        print(f"[resume] {scenario_id}: state={sm.state} ✓ "
              f"contract={contract_note} ✓ "
              f"history={len(sm.history)} transitions legal ✓ "
              f"{seal_note}", flush=True)
        return self

    def log(self, tool: str, payload: Any, result: Any, ms: float,
            role: str | None = None, event: str | None = None) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "run_id": self.scenario_id,
            "tool": tool,
            "role": role or "unknown",
            "state_after": self.sm.state,
            "payload_sha256": sha256_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True))[:16],
            "result_sha256": sha256_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True,
                           default=str))[:16],
            "duration_ms": round(ms, 1),
        }
        if event:
            entry["event"] = event
        with self.trace_path.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def record_security_event(self, tool: str, role: str, detail: str) -> None:
        """Persist a role-policy denial as a first-class security event."""
        self.security_events.append({
            "ts": time.time(), "event": "role-denied", "tool": tool,
            "role": role, "detail": detail})
        self._write_json(self.run_dir / "evidence" / "security_events.json",
                         self.security_events)

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
        embedded = self.fixture.get("embedded_baseline_tests")
        if embedded is not None:
            return dict(embedded)
        names = self.fixture.get("baseline_test_files") or [
            self.fixture["baseline_test_file"]]
        return {name: (TARGET_DIR / name).read_text(encoding="utf-8") for name in names}

    def target_source(self) -> dict[str, str]:
        embedded = self.fixture.get("embedded_target_files")
        if embedded is not None:
            sources = dict(embedded)
        else:
            sources = {name: (TARGET_DIR / name).read_text(encoding="utf-8")
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


def _append_resume_log(run_dir: Path, scenario_id: str, data: dict,
                       failures: list[str]) -> None:
    """Persist every resume verification for the checkpoint panel: the
    three-way check outcome plus the versions recovered (state / contract
    hash / history length). Append-only, like every audit artifact."""
    log_path = run_dir / "resume_log.json"
    try:
        log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        sm = data.get("sm") or {}
        contract = data.get("contract") or {}
        log.append({
            "ts": time.time(),
            "result": "rejected" if failures else "resumed",
            "failures": failures,
            "state": sm.get("state"),
            "contract_version": contract.get("version"),
            "contract_hash": (contract.get("frozen_hash") or "")[:16] or None,
            "history_len": len(sm.get("history") or []),
            "green_gates": sorted(sm.get("green_gates") or []),
        })
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass



RUNS: dict[str, NotaryRun] = {}
# scenario ids explicitly reset in this process lifetime (wipe authorization)
_RESET_OK: set[str] = set()


def restore_all_runs() -> int:
    """Resume every checkpointed run on gateway startup (crash recovery)."""
    if not RUNS_DIR.exists():
        return 0
    restored = 0
    for run_dir in sorted(RUNS_DIR.iterdir()):
        if not run_dir.is_dir():
            continue
        try:
            run = NotaryRun.restore(run_dir)
        except (OSError, json.JSONDecodeError, KeyError):
            continue  # corrupt checkpoint: replay from trace remains possible
        if run is not None:
            RUNS[run.scenario_id] = run
            restored += 1
    return restored


def get_run(scenario_id: str) -> NotaryRun:
    if scenario_id not in RUNS:
        RUNS[scenario_id] = NotaryRun(scenario_id)
    return RUNS[scenario_id]


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# Skill runtime loader: discovery, compat validation, trigger-signal match
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse `---\\nkey: value\\n---` frontmatter (plain strings only)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = v.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        raise ValueError(f"malformed version {v!r}")
    return tuple(int(p) for p in parts)


def _compat_check(compat: str) -> tuple[bool, str]:
    """Validate `gateway>=X.Y[,<Z]` clauses against GATEWAY_VERSION."""
    cur = _version_tuple(GATEWAY_VERSION)
    for clause in compat.split(","):
        clause = clause.strip()
        m = re.fullmatch(r"(?:gateway)?(>=|<)(\d+(?:\.\d+)*)", clause)
        if not m:
            return False, f"unparseable compat clause {clause!r}"
        req = _version_tuple(m.group(2))
        width = max(len(req), len(cur))
        req += (0,) * (width - len(req))
        cur += (0,) * (width - len(cur))
        if m.group(1) == ">=" and cur < req:
            return False, (f"requires gateway>={m.group(2)}, "
                           f"gateway is {GATEWAY_VERSION}")
        if m.group(1) == "<" and cur >= req:
            return False, (f"requires gateway<{m.group(2)}, "
                           f"gateway is {GATEWAY_VERSION}")
        cur = _version_tuple(GATEWAY_VERSION)
    return True, ""


def _load_one_skill(path: Path, source: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    fallback = path.parent.name if source == "seed" else path.stem
    rec: dict[str, Any] = {
        "name": meta.get("name") or fallback,
        "source": source,
        "path": str(path.relative_to(PKG_ROOT)),
        "description": meta.get("description", ""),
        "version": meta.get("version"),
        "compat": meta.get("compat"),
    }
    compat = meta.get("compat")
    if not compat:
        # No compat declared: loadable, but explicitly flagged so the
        # audit trail can tell versioned skills from legacy ones.
        rec["status"] = "loaded"
        rec["compat_status"] = "unversioned"
    else:
        ok, reason = _compat_check(compat)
        if ok:
            rec["status"] = "loaded"
            rec["compat_status"] = "ok"
        else:
            rec["status"] = "rejected"
            rec["compat_status"] = "incompatible"
            rec["reason"] = reason
    return rec


def load_match_table() -> dict[str, Any]:
    return json.loads(MATCH_TABLE_PATH.read_text(encoding="utf-8"))


# -- registry index: append-only integrity ledger ---------------------------

def _load_index() -> dict[str, Any]:
    if not SKILL_INDEX_PATH.exists():
        return {"entries": []}
    return json.loads(SKILL_INDEX_PATH.read_text(encoding="utf-8"))


def _append_index(entry: dict[str, Any]) -> None:
    SKILL_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    idx = _load_index()
    idx["entries"].append(entry)
    SKILL_INDEX_PATH.write_text(
        json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _registry_state() -> tuple[dict[str, dict], set[str], set[str], bool]:
    """Latest register entry per name, retired set, confirmed set, index flag.

    confirm（追认转正）是注册表上的一等动作：人工门③的看板追认从
    probation 转为正式服役；否决则用既有 retire 一票否决。"""
    latest: dict[str, dict] = {}
    retired: set[str] = set()
    confirmed: set[str] = set()
    for e in _load_index()["entries"]:
        if e.get("action") == "register":
            latest[e["name"]] = e
            retired.discard(e["name"])
            confirmed.discard(e["name"])
        elif e.get("action") == "retire":
            retired.add(e["name"])
        elif e.get("action") == "confirm":
            confirmed.add(e["name"])
    return latest, retired, confirmed, SKILL_INDEX_PATH.exists()


def load_skills() -> list[dict[str, Any]]:
    """Discover seeds (skills/<name>/SKILL.md) + registry distillations.

    Loaded fresh on every call (the registry may grow mid-run). Registry
    skills are verified against the append-only index: a file with no
    index entry, or whose content hash differs from the indexed one, is
    REJECTED (fail-closed) — runtime tampering is a load error, never a
    silent serve. Retired skills (tombstoned in the index) are listed for
    audit but never served.
    """
    latest, retired, confirmed, indexed = _registry_state()
    skills = [_load_one_skill(p, "seed")
              for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))]
    for path in sorted(SKILL_REGISTRY_DIR.glob("*.md")):
        rec = _load_one_skill(path, "registry")
        entry = latest.get(rec["name"])
        if not indexed or entry is None:
            rec.update(status="rejected", compat_status="unindexed",
                       reason="registry file has no index entry "
                              "(fail-closed)")
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != entry.get("content_sha256"):
                rec.update(status="rejected", compat_status="tampered",
                           reason="content hash mismatch with registry index")
            rec["supersedes"] = entry.get("supersedes")
        if rec["status"] == "loaded" and rec["name"] in retired:
            rec["status"] = "retired"
        if rec["status"] == "loaded" and rec["name"] not in confirmed:
            # 新注册进试用区：可被 match（probation 模式）、结果标注试用；
            # 种子 skill 随包审计过，不在此列。
            rec["status"] = "probation"
        skills.append(rec)
    for rec in skills:
        if rec["source"] == "seed" and rec["status"] == "loaded" \
                and rec["name"] in retired:
            rec["status"] = "retired"
    return skills


def _resolve_latest(name: str, by_name: dict[str, dict]
                    ) -> tuple[dict | None, list[str]]:
    """Walk the supersedes chain; return the newest non-retired, loaded
    member (rollback = the newest is retired, its predecessor serves)."""
    chain: list[str] = []
    cur: str | None = name
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = next((n for n, s in by_name.items()
                    if s.get("supersedes") == cur), None)
    servable = {"loaded"} | (
        {"probation"} if CONFIG["skill_governance"] == "probation" else set())
    for cand in reversed(chain):
        rec = by_name.get(cand)
        if rec and rec["status"] in servable:
            return rec, chain
    return None, chain


# ---------------------------------------------------------------------------
# Role policy: least-privilege whitelist on sensitive endpoints.
#
# Callers MAY attach "role" to any tool-call payload. Semantics:
#   - role present + endpoint restricted + role not allowed -> explicit
#     refusal + security event (fail-closed)
#   - role absent -> call proceeds, trace marks role "unknown" (auditable)
#   - endpoint not listed -> open to every role
# ---------------------------------------------------------------------------

KNOWN_ROLES = {
    "sentinel", "triage", "rca", "contract", "author", "tester",
    "convention", "gatekeeper", "release", "postmortem", "leader", "human",
    "adjudicator",
}

# Entry conditions: which pipeline states each state-sensitive tool may be
# invoked from (fail-closed). Complements ROLE_POLICY — the role whitelist
# governs WHO may call; this governs WHEN a call is meaningful. Notably:
# implementation/tests cannot be submitted before the contract exists
# (blind partitions are meaningless without a frozen contract), and no
# artifacts may be rewritten after terminal states.
ENTRY_STATES: dict[str, set[str]] = {
    "notary_author.submit_implementation":
        {"CONTRACTED", "AUTHORING", "TESTING", "GATING"},
    "notary_tester.submit_tests":
        {"CONTRACTED", "AUTHORING", "TESTING", "GATING"},
    # REJECTED is included for gates: post-terminal gate runs are
    # evidence-only (record_verdict does not touch the state machine),
    # which is how external red-path samples collect their convention
    # findings after a test-gate rejection.
    "notary_gate.run_test_gate":
        {"AUTHORING", "TESTING", "GATING", "REJECTED"},
    "notary_gate.run_mutation_gate":
        {"AUTHORING", "TESTING", "GATING", "REJECTED"},
    "notary_gate.run_convention_gate":
        {"AUTHORING", "TESTING", "GATING", "REJECTED"},
}

# States from which a triage escalation is still meaningful.
_TRIAGE_ESCALATE_FORBIDDEN = {"NOTARIZED", "RELEASED", "ROLLED_BACK", "REJECTED"}

ROLE_POLICY: dict[str, set[str]] = {
    "notary_change.get_submitted_change": {"sentinel", "triage"},
    "notary_repo.get_baseline_tests": {"tester", "contract"},
    "notary_sentinel.scan": {"sentinel"},
    "notary_flow.triage": {"triage", "leader"},
    "notary_flow.diagnosis": {"rca"},
    "notary_flow.reproduce": {"rca"},
    "notary_flow.request_rework": {"gatekeeper", "leader"},
    "notary_flow.dispute": {"author", "leader"},
    "notary_contract.freeze": {"contract"},
    "notary_author.get_context": {"author"},
    "notary_author.submit_implementation": {"author"},
    "notary_tester.get_context": {"tester"},
    "notary_tester.submit_tests": {"tester"},
    "notary_gate.run_test_gate": {"gatekeeper"},
    "notary_gate.run_mutation_gate": {"gatekeeper"},
    "notary_gate.get_survivors": {"gatekeeper", "author"},
    "notary_gate.finalize_mutation": {"gatekeeper"},
    "notary_gate.run_convention_gate": {"gatekeeper", "convention"},
    "notary_rebuttal.submit": {"author"},
    "notary_flow.resolve_human": {"leader", "human"},
    "notary_flow.adjudicate": {"adjudicator", "leader"},
    "notary_release.deploy": {"release"},
    "notary_release.rollback": {"release"},
    "notary_evidence.seal": {"release", "postmortem"},
    "notary_skill.register": {"postmortem"},
    "notary_skill.retire": {"postmortem", "leader"},
    "notary_skill.confirm": {"leader", "adjudicator"},
    "notary_intake.submit_issue": {"ci", "triage", "leader"},
}


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
    if verdict == "escalate" and run.sm.state in _TRIAGE_ESCALATE_FORBIDDEN:
        raise IllegalTransition(
            f"triage escalate not meaningful from {run.sm.state}")
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
        (workdir / Path(name).name).write_text(content, encoding="utf-8")
    repro = str(_p.get("repro_snippet", "")).strip() \
        or run.fixture.get("repro_snippet") or (
        "from queue_box import Mailbox\n"
        "box = Mailbox()\n"
        "try:\n"
        "    box.pop()\n"
        "except IndexError as exc:\n"
        "    print(f'IndexError: {exc}')\n"
    )
    if run.fixture.get("custom_target"):
        # Customer-uploaded code: rlimit ceilings before any statement runs.
        repro = _SANDBOX_PREAMBLE + repro
    (workdir / "repro.py").write_text(repro, encoding="utf-8")
    proc = subprocess.run([sys.executable, "repro.py"], capture_output=True,
                          text=True, timeout=CONFIG["test_timeout_s"], cwd=str(workdir))
    return {"command": "python3 repro.py  # scenario repro snippet",
            "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(),
            "exit_code": proc.returncode}


def _validate_assumptions(raw: Any) -> list[dict[str, str]]:
    """Contract assumptions schema: recorded ambiguity, not silent guessing.

    Each entry is {point, assumption, basis}: the ambiguity found in the
    issue, the default reading the contract adopts, and what that reading
    rests on (prior behavior / downstream agreement / ...). Assumptions
    may go ahead without blocking, but they are frozen into the contract
    hash and remain challengeable via notary_flow.dispute.
    """
    if not isinstance(raw, list):
        raise ValueError("assumptions must be a list of "
                         "{point, assumption, basis} objects")
    out: list[dict[str, str]] = []
    for i, a in enumerate(raw):
        if not isinstance(a, dict):
            raise ValueError(f"assumptions[{i}] must be an object")
        entry = {}
        for key in ("point", "assumption", "basis"):
            value = str(a.get(key, "")).strip()
            if len(value) < 4:
                raise ValueError(
                    f"assumptions[{i}].{key} must be non-empty; an "
                    f"assumption without its basis is a silent guess")
            entry[key] = value
        out.append(entry)
    return out


def t_freeze_contract(run: NotaryRun, p: dict) -> dict:
    assertions = p.get("assertions") or []
    if not assertions or not all(isinstance(a, str) and len(a) >= 8
                                 for a in assertions):
        raise ValueError("assertions must be a non-empty list of verifiable "
                         "plain-text statements (>= 8 chars each)")
    assumptions = _validate_assumptions(p.get("assumptions") or [])
    revising = run.sm.state == "ESCALATED" and run.contract is not None
    if revising and not assumptions:
        # 修订不清空留痕假设：未显式给出时继承上一版（歧义留痕跨版本延续）
        assumptions = run.contract.get("assumptions") or []
    in_scope = p.get("in_scope") or run.fixture["target_files"]
    out_of_scope = p.get("out_of_scope") or []
    previous_hash = run.contract["frozen_hash"] if revising else None
    previous_version = run.contract.get("version", 1) if revising else 0
    context_refs = [str(run.run_dir / "diagnosis.json")]
    contract_hash = freeze_contract(
        run.fixture["issue"]["id"], assertions, context_refs,
        assumptions or None)
    run.contract = {
        "issue_id": run.fixture["issue"]["id"],
        "assertions": assertions,
        "assumptions": assumptions,
        "context_refs": context_refs,  # recorded so the hash recomputes
                                       # anywhere, not just on this machine
        "in_scope": in_scope,
        "out_of_scope": out_of_scope,
        "version": previous_version + 1,
        "blind_partitions": {
            "author_sees": ["contract", "target source", "diagnosis"],
            "tester_sees": ["contract", "baseline public tests"],
        },
        "frozen_hash": contract_hash,
    }
    if previous_hash:
        # Contract evolution: the revision chains to the version it
        # replaces, so the arbitration trail is tamper-evident.
        run.contract["previous_hash"] = previous_hash
        # 补证附件：修订若源自裁决，把裁决的 references 写进新契约
        adj_path = run.run_dir / "adjudication.json"
        if adj_path.exists():
            adj = json.loads(adj_path.read_text(encoding="utf-8"))
            revise = next((a for a in reversed(adj)
                           if a.get("decision") == "revise"), None)
            if revise and revise.get("references"):
                run.contract["references"] = revise["references"]
                run.contract["adjudication_id"] = revise.get("id")
    run._write_json(run.run_dir / "contract.json", run.contract)
    if revising:
        run.sm.revise_contract()
    else:
        run.sm.advance_to("CONTRACTED")
    resealed = False
    if revising and (run.run_dir / "manifest.json").exists():
        # 封存不是终点快照而是活装订：修订契约改写 contract.json 后
        # 必须重封印，否则恢复校验的封印复算会把合法 v1.1 误判为篡改
        t_seal(run, {})
        resealed = True
    return {"frozen_hash": contract_hash, "version": run.contract["version"],
            "revised": revising, "resealed": resealed,
            "assumptions_recorded": len(assumptions),
            "pipeline_state": run.sm.state}


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
        (part / name).write_text(content, encoding="utf-8")
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
        (part / name).write_text(content, encoding="utf-8")
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
    res = _run_unittest(workdir, sandbox=bool(run.fixture.get("custom_target")))
    decision = "green" if res["ok"] else "red"
    summary = (f"baseline+blind tests: {res.get('tests_ran', 0)} run, "
               f"{'all passed' if res['ok'] else 'failures present'}")
    return run.record_verdict("TEST_PASS", decision, summary,
                              {"test_output": res["output"]})


def t_run_mutation_gate(run: NotaryRun, _p: dict) -> dict:
    _require_change_and_tests(run)
    # A new mutation run voids prior rebuttals: they were scoped to the
    # previous mutant set and are preserved in the finalized verdict.
    run.rebuttals = []
    if run.sm.state in ("AUTHORING", "TESTING"):
        run.sm.advance_to("GATING")
    source_files = run_impl_files(run)
    mutants: list[dict[str, str]] = []
    for fname, fsource in source_files.items():
        for m in generate_mutants(fsource):
            m["file"] = fname
            mutants.append(m)
    # Re-id deterministically across files, cap total.
    mutants = mutants[:CONFIG["mutation"]["max_mutants"]]
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
        res = _run_unittest(workdir, sandbox=bool(run.fixture.get("custom_target")))
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
                    and score < CONFIG["mutation"]["green_above"]}
    survivors_md = ["# Mutation survivors\n"]
    for s in survived:
        survivors_md.append(f"- {s['id']} line {s['line']}: {s['mutation']}")
    (run.run_dir / "survivors.md").write_text("\n".join(survivors_md) + "\n", encoding="utf-8")
    out = {"mutants_total": len(mutants), "killed": len(killed),
           "survived": survived, "invalid": len(invalid),
           "score": round(score, 4),
           "bands": {"red_below": CONFIG["mutation"]["red_below"],
                     "green_above": CONFIG["mutation"]["green_above"]}}
    if run.mutation["awaiting_rebuttal"]:
        out["status"] = ("awaiting_rebuttal: survivors sent to author; "
                         "finalize via notary_gate.finalize_mutation")
    else:
        verdict = run.record_verdict(
            "MUTATION",
            "green" if score > CONFIG["mutation"]["green_above"] else
            "yellow" if score >= CONFIG["mutation"]["red_below"] else "red",
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
        decision = ("green" if score > CONFIG["mutation"]["green_above"] else
                    "yellow" if score >= CONFIG["mutation"]["red_below"] else "red")
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
        verdicts[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return {"verdicts": verdicts, "pipeline_state": run.sm.state,
            "history_len": len(run.sm.history)}


def t_state(run: NotaryRun, _p: dict) -> dict:
    return {"run_id": run.scenario_id, "state": run.sm.state,
            "green_gates": sorted(run.sm._green_gates),
            "rework_round": run.rework_round,
            "max_rework_rounds": run.max_rework_rounds,
            "contract_version": (run.contract or {}).get("version"),
            "history": run.sm.history}


def t_reverify_checkpoint(run: "NotaryRun | None", p: dict) -> dict:
    """Read-only live re-verification of the on-disk checkpoint: the same
    four-way check that gates every resume, runnable without a restart.
    Demo affordance for "show me how you know the versions match" — the
    call itself is traced (查验留痕), but no state changes."""
    sid = run.scenario_id if run is not None else str(p.get("_sid") or "")
    run_dir = RUNS_DIR / sid
    cp_path = run_dir / "checkpoint.json"
    if not cp_path.exists():
        return {"consistent": None,
                "note": "该任务没有检查点（早期回放 run），"
                        "以 trace 轨迹回放到终态为准"}
    data = json.loads(cp_path.read_text(encoding="utf-8"))
    failures = verify_checkpoint(run_dir, data)
    sm = data.get("sm") or {}
    contract = data.get("contract") or {}
    try:
        manifest = json.loads(
            (run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    return {"consistent": not failures, "failures": failures,
            "state": sm.get("state"),
            "contract_version": contract.get("version"),
            "contract_hash": (contract.get("frozen_hash") or "")[:16] or None,
            "history_len": len(sm.get("history") or []),
            "sealed_files": len(manifest.get("files") or {}),
            "gateway_version_at_seal": manifest.get("gateway_version"),
            "gateway_version_now": GATEWAY_VERSION,
            "checked_at": time.time()}


def t_request_rework(run: NotaryRun, p: dict) -> dict:
    """Bounded rework loop (L4): REJECTED -> AUTHORING.

    A rejection concludes one notarization attempt, not the issue. Within
    budget, gatekeeper/leader may send the issue back for rework; when the
    budget is spent, the only ways forward are contract revision (from
    ESCALATED) or termination — loops are bounded by design.
    """
    reason = str(p.get("reason", "")).strip()
    if not reason:
        raise ValueError("reason must be non-empty; rework is an audited "
                         "decision, not a silent retry")
    if run.sm.state != "REJECTED":
        raise IllegalTransition(
            f"request_rework only valid in REJECTED, not {run.sm.state}")
    if run.rework_round >= run.max_rework_rounds:
        raise ValueError(
            f"rework budget exhausted "
            f"({run.rework_round}/{run.max_rework_rounds}); the loop is "
            f"bounded by design — escalate (contract revision via "
            f"notary_contract.freeze from ESCALATED) or archive as terminal")
    run.rework_round += 1
    state = run.sm.rework()
    log_path = run.run_dir / "rework.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    log.append({"round": run.rework_round, "budget": run.max_rework_rounds,
                "reason": reason, "from": "REJECTED", "to": state,
                "ts": time.time()})
    run._write_json(log_path, log)
    return {"rework_round": run.rework_round,
            "budget": run.max_rework_rounds,
            "green_gates_cleared": True,
            "pipeline_state": state}


def t_dispute(run: NotaryRun, p: dict) -> dict:
    """REJECTED -> ESCALATED: dispute the verdict's contract basis.

    The red gate was mechanically correct under contract vN; the dispute
    channel exists for the one question the machine cannot answer: what
    is the requirement basis of this contract clause? The target of a
    dispute is the CONTRACT, never the test process — a dispute about
    gate mechanics is a rework request, not an escalation. The pipeline
    stops and waits for a human adjudicator (designed pause).
    """
    focus = str(p.get("focus", "")).strip()
    if len(focus) < 8:
        raise ValueError(
            "focus must state the challenged clause's basis question "
            "(>= 8 chars); a dispute without a stated focus is not "
            "auditable")
    clause = str(p.get("clause", "")).strip()
    if run.sm.state != "REJECTED":
        raise IllegalTransition(
            f"dispute only valid in REJECTED, not {run.sm.state}; "
            f"the dispute channel challenges a concluded red verdict, "
            f"not a pipeline in flight")
    if run.contract is None:
        raise IllegalTransition(
            "dispute requires a frozen contract to challenge")
    state = run.sm.dispute()
    entry = {"focus": focus, "clause": clause or None,
             "contract_version": run.contract.get("version"),
             "contract_hash": run.contract["frozen_hash"],
             "role": p.get("role"), "from": "REJECTED", "to": state,
             "ts": time.time()}
    log_path = run.run_dir / "dispute.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    log.append(entry)
    run._write_json(log_path, log)
    return {"dispute_id": len(log), "focus": focus,
            "contract_version": entry["contract_version"],
            "pipeline_state": state,
            "note": "escalated for human adjudication; gates keep their "
                    "recorded verdicts, the pipeline waits"}


_ADJUDICATION_DECISIONS = ("uphold", "revise", "request_evidence", "override")


def t_adjudicate(run: NotaryRun, p: dict) -> dict:
    """Human gate ②: adjudicate an ESCALATED dispute (four options).

    The adjudicator rules on the CONTRACT's basis, never on the code —
    after a revision the system re-verifies the code under v1.1 itself.
    Claims (subject/role/scope/credential_id) are recorded for audit;
    the raw token NEVER touches disk. State effects:
      uphold           -> back to REJECTED (the red verdict stands)
      revise           -> stays ESCALATED; the next contract freeze carries
                          this adjudication's references into v1.1
      request_evidence -> stays ESCALATED (supplementation is a verdict)
      override         -> GATING (special release: mandatory reason,
                          flagged yellow in the record)
    """
    decision = str(p.get("decision", "")).strip()
    if decision not in _ADJUDICATION_DECISIONS:
        raise ValueError(
            f"decision must be one of: {' / '.join(_ADJUDICATION_DECISIONS)}")
    rationale = str(p.get("rationale", "")).strip()
    min_len = 8 if decision != "override" else 20
    if len(rationale) < min_len:
        raise ValueError(
            f"rationale too short (>= {min_len} chars"
            + ("; override is a signed exception, argue it properly"
               if decision == "override" else "") + ")")
    if run.sm.state != "ESCALATED":
        raise IllegalTransition(
            f"adjudicate only valid in ESCALATED, not {run.sm.state}")
    claims = p.get("claims") or {}
    actor = str(claims.get("subject") or p.get("role") or "unknown")
    entry = {
        "decision": decision,
        "rationale": rationale,
        "actor": actor,
        "role": claims.get("role") or p.get("role"),
        "scope": claims.get("scope", []),
        "credential_id": claims.get("credential_id"),
        "channel": str(p.get("channel") or "direct"),
        "evidence_reviewed": [str(x) for x in p.get("evidence_reviewed", [])],
        "references": [str(x) for x in p.get("references", [])],
        "contract_version": (run.contract or {}).get("version"),
        "contract_hash": (run.contract or {}).get("frozen_hash"),
        "flagged": decision == "override",  # 特批标黄
        "ts": time.time(),
    }
    log_path = run.run_dir / "adjudication.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    entry["id"] = len(log) + 1
    log.append(entry)
    run._write_json(log_path, log)
    if decision == "uphold":
        state = run.sm.resolve_human(False)
    elif decision == "override":
        state = run.sm.resolve_human(True)
    else:  # revise / request_evidence: pipeline waits for the follow-up act
        state = run.sm.state
    return {"adjudication_id": entry["id"], "decision": decision,
            "actor": actor, "credential_id": entry["credential_id"],
            "pipeline_state": state,
            "note": "裁决对象是规则不是代码；代码由系统按规则重新验证"}


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
    res = _run_unittest(prod, sandbox=bool(run.fixture.get("custom_target")))
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
    removed = False
    if backup.exists():
        shutil.rmtree(prod)
        shutil.copytree(backup, prod)
        restored = True
    elif prod.exists():
        # No previous version to restore: rollback of a first deployment
        # means uninstalling the rejected release, never leaving it live.
        shutil.rmtree(prod)
        removed = True
    log = {"pipeline_state": state, "restored_backup": restored,
           "prod_removed": removed, "ts": time.time()}
    run._write_json(run.run_dir / "evidence" / "rollback_log.json", log)
    return log


def _write_certificate(run: NotaryRun) -> None:
    """Generate the notarization certificate — the one-page deliverable.

    A notary office that issues no certificate is a process, not a product.
    The certificate is written BEFORE sealing so it is itself covered by
    the evidence manifest's sha256 seal.
    """
    verdicts = {}
    vdir = run.run_dir / "verdicts"
    if vdir.is_dir():
        for vf in sorted(vdir.glob("*.json")):
            verdicts[vf.stem] = json.loads(vf.read_text(encoding="utf-8"))
    gate_lines = []
    for gate, v in verdicts.items():
        line = f"| {gate} | {v['decision']} | {v['summary']} |"
        gate_lines.append(line)
    contract = run.contract or {}
    hist = run.sm.history
    cert = f"""# CodeNotary 公证书

| 项 | 值 |
|---|---|
| 运行 | `{run.scenario_id}` |
| Issue | {run.fixture['issue'].get('id', '?')} — {run.fixture['issue'].get('title', '')} |
| 终态 | **{run.sm.state}** |
| 契约 | frozen_hash `{contract.get('frozen_hash', '—')[:16]}…`（版本 v{contract.get('version', '—')}） |
| 重修轮次 | {run.rework_round}/{run.max_rework_rounds} |
| 安全事件 | {len(run.security_events)} 次越权拒绝 |
| 人工介入 | {sum(1 for h in hist if h['state'] == 'ESCALATED')} 次升级 |

## 门禁裁决

| 门禁 | 判定 | 摘要 |
|---|---|---|
{chr(10).join(gate_lines) if gate_lines else '| — | — | 未进入门禁 |'}

## 证据

本公证书所涉全部判定由确定性代码执行（LLM 仅参与判断环节，不参与裁决）。
证据包封印清单见同目录 `manifest.json`（逐文件 SHA-256）；
逐调用审计见 `trace.jsonl`（含角色与载荷/结果哈希链）。
第三方复算方式见代码包 EVALUATION.md 与技术文档附录 F。
"""
    (run.run_dir / "certificate.md").write_text(cert, encoding="utf-8")


def should_issue_certificate(fixture: dict) -> bool:
    """Advisory runs (customer uploaded no tests) get a fix SUGGESTION,
    not a certificate — the evidence package still seals as usual."""
    return not fixture.get("advisory")


def t_seal(run: NotaryRun, _p: dict) -> dict:
    if should_issue_certificate(run.fixture):
        _write_certificate(run)  # the certificate is part of the sealed package
    # Volatile files are sealed by PREFIX, not by whole-file hash:
    # trace.jsonl keeps growing (the seal call itself is logged right
    # after) and checkpoint.json is rewritten after every call. We bind
    # the first N lines of the trace at seal time; later appends cannot
    # alter them (append-only), and verify recomputes the same prefix.
    # checkpoint.json is intentionally unsealed — its integrity is the
    # job of restore-time three-way verification, not of the evidence seal.
    trace_lines = run.trace_path.read_text(encoding="utf-8").splitlines(keepends=True) \
        if run.trace_path.exists() else []
    manifest: dict[str, Any] = {"files": {}}
    for path in sorted(run.run_dir.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts \
                and "work" not in path.parts \
                and path.name not in ("manifest.json", "trace.jsonl",
                                      "checkpoint.json", "checkpoint.json.tmp") \
                and not path.name.startswith("manifest.sig"):
            # signature artifacts are excluded: re-sealing after a contract
            # revision rewrites them, and a seal that binds its own
            # signature is self-defeating (double-seal false positive,
            # found in live rework run d5_pkg_leq_local)
            manifest["files"][str(path.relative_to(run.run_dir))] = \
                sha256_text(path.read_bytes().decode("utf-8", errors="replace"))
    manifest["trace_prefix"] = {
        "lines": len(trace_lines),
        "sha256": sha256_text("".join(trace_lines))}
    manifest["gateway_version"] = GATEWAY_VERSION
    run._write_json(run.run_dir / "manifest.json", manifest)
    signed = _sign_manifest(run)
    return {"sealed_files": len(manifest["files"]),
            "manifest": str(run.run_dir / "manifest.json"),
            "signed": signed,
            "pipeline_state": run.sm.state}


def _sign_manifest(run: NotaryRun) -> bool:
    """Ed25519-sign manifest.json if keys/notary_ed25519.pem exists.

    The seal proves the evidence was not touched AFTER sealing; the
    signature proves WHO sealed it. Signing uses the openssl CLI (no new
    Python deps); a configured key with no openssl available is a hard
    error, never a silent unsigned seal.
    """
    key = PKG_ROOT / "keys" / "notary_ed25519.pem"
    if not key.exists():
        return False
    manifest_path = run.run_dir / "manifest.json"
    sig_path = run.run_dir / "evidence" / "manifest.sig.bin"
    sig_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
         "-in", str(manifest_path), "-out", str(sig_path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"manifest signing failed: {proc.stderr.strip()}")
    pub_proc = subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout"],
        capture_output=True, text=True)
    fingerprint = sha256_text(pub_proc.stdout)[:16]
    run._write_json(run.run_dir / "evidence" / "manifest.sig.json", {
        "algorithm": "Ed25519",
        "signature_hex": sig_path.read_bytes().hex(),
        "pubkey_fingerprint": fingerprint,
        "signed_file": "manifest.json",
        "note": "verify: openssl pkeyutl -verify -pubin -inkey "
                "notary_ed25519.pub -rawin -in manifest.json -sigfile <sig>"})
    sig_path.unlink()  # canonical form is the hex inside the JSON
    return True


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
    version = str(p.get("version", "1.0.0")).strip() or "1.0.0"
    _version_tuple(version)  # raises on malformed
    supersedes = str(p.get("supersedes", "")).strip() or None
    if supersedes:
        known = {s["name"] for s in load_skills()}
        if supersedes not in known:
            raise ValueError(
                f"supersedes target '{supersedes}' is not a known skill; "
                f"known: {sorted(known)}")
    mm = ".".join(GATEWAY_VERSION.split(".")[:2])
    header = (f"---\nname: {name}\ndescription: distilled from run "
              f"{run.scenario_id}\nversion: {version}\n"
              f"compat: gateway>={mm}\n")
    if supersedes:
        header += f"supersedes: {supersedes}\n"
    header += "---\n\n"
    target.write_text(header + content + "\n", encoding="utf-8")
    _append_index({
        "action": "register", "name": name, "version": version,
        "supersedes": supersedes,
        "content_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source_run": run.scenario_id, "ts": time.time()})
    run._write_json(run.run_dir / "evidence" / "postmortem_skill.json",
                    {"registered": name, "path": str(target)})
    return {"registered": name, "version": version,
            "supersedes": supersedes, "path": str(target)}


def t_confirm_skill(run: NotaryRun, p: dict) -> dict:
    """Probation -> loaded: 看板追认转正（人工门③）。

    追认不是审批前置，而是事后确认：试用期的命中数据（skill_matches.json）
    就是评审材料。追加 confirm 条目进 append-only 注册表。
    """
    name = str(p.get("name", "")).strip()
    reason = str(p.get("reason", "")).strip()
    if not _SAFE_NAME_RE.match(name) or not reason:
        raise ValueError("skill name must be safe and reason non-empty; "
                         "追认须留理由")
    by_name = {s["name"]: s for s in load_skills()}
    rec = by_name.get(name)
    if rec is None:
        raise ValueError(f"unknown skill '{name}'")
    if rec["status"] != "probation":
        raise ValueError(
            f"skill '{name}' is {rec['status']}, not in probation; "
            f"只有试用区的 Skill 需要追认")
    claims = p.get("claims") or {}
    _append_index({
        "action": "confirm", "name": name,
        "by": claims.get("subject") or p.get("role"),
        "credential_id": claims.get("credential_id"),
        "reason": reason, "ts": time.time()})
    return {"confirmed": name, "version": rec.get("version"),
            "governance_after": "loaded",
            "note": "追认已登记；注册表只增不改，可随时用 retire 否决"}

def t_retire_skill(run: NotaryRun, p: dict) -> dict:
    """Tombstone a skill (append-only rollback): it immediately stops being
    served by get/match. Rolling back a version = retiring the newest one;
    its predecessor in the supersedes chain serves again automatically."""
    name = str(p.get("name", "")).strip()
    reason = str(p.get("reason", "")).strip()
    if not _SAFE_NAME_RE.match(name) or not reason:
        raise ValueError("skill name must be safe and reason non-empty; "
                         "retirement is an audited decision")
    rec = next((s for s in load_skills() if s["name"] == name), None)
    if rec is None:
        raise ValueError(f"unknown skill '{name}'")
    if rec["status"] == "retired":
        raise ValueError(f"skill '{name}' is already retired")
    _append_index({"action": "retire", "name": name, "reason": reason,
                   "source_run": run.scenario_id if run else None,
                   "ts": time.time()})
    if run is not None:
        run._write_json(run.run_dir / "evidence" / "retired_skill.json",
                        {"retired": name, "reason": reason})
    return {"retired": name, "reason": reason,
            "note": "tombstone appended; the supersedes-chain predecessor, "
                    "if any, serves again immediately"}


# ---------------------------------------------------------------------------
# Intake (tool-integration reference implementation)
#
# Reference for the six-step tool-onboarding process (tech report §19):
#   1. uniform {ok, result|error} contract     2. tool_catalog.json schema
#   3. ROLE_POLICY role declaration            4. automatic trace audit
#   5. future_mcp_mapping                      6. fuzz coverage
# This endpoint is the worked example: a CI/webhook caller submits an issue,
# the gateway mints a scenario fixture for it (idempotent by content hash),
# and the normal pipeline takes it from there.
# ---------------------------------------------------------------------------

INTAKE_TARGETS: dict[str, dict[str, list[str]]] = {
    "queue_box": {"target_files": ["queue_box.py"],
                  "baseline_test_files": ["test_queue_box_baseline.py"]},
    "dispatcher": {"target_files": ["queue_box.py", "dispatcher.py"],
                   "baseline_test_files": ["test_queue_box_baseline.py",
                                           "test_dispatcher_baseline.py"]},
    "mailbox_router": {"target_files": ["queue_box.py", "mailbox_router.py"],
                       "baseline_test_files": ["test_queue_box_baseline.py",
                                               "test_router_baseline.py"]},
    "coupon_expiry": {"target_files": ["coupon.py"],
                      "baseline_test_files": ["test_coupon_baseline.py"]},
}


def _validate_custom_upload(p: dict, source_files: dict) -> None:
    """Validate a bring-your-own-code ("自带代码送修") upload, fail-closed.

    Order matters: shape and size limits first, quarantine LAST — the
    reject-on-sight scan is the final word before a scenario is minted.

    Advisory mode (p["advisory"]=True): the customer uploaded no tests.
    The pipeline still runs end-to-end, but the run gets a fix SUGGESTION
    instead of a certificate (no tests, no notarization — the first
    notary principle, relaxed only as far as advice is concerned).
    """
    if not isinstance(source_files, dict) or not source_files \
            or not all(isinstance(v, str) for v in source_files.values()):
        raise ValueError(
            "source_files must be a non-empty {name: content} map")
    _check_filenames(source_files)
    if len(source_files) > _CUSTOM_MAX_FILES:
        raise ValueError(
            f"too many source files (>{_CUSTOM_MAX_FILES})")
    if not all(n.endswith(".py") for n in source_files):
        raise ValueError("source files must be .py")
    advisory = bool(p.get("advisory"))
    test_files = p.get("test_files")
    if test_files is None and not advisory:
        raise ValueError(
            "test_files required: we do not notarize code without tests — "
            "bring at least one, however small (or ask for advisory mode, "
            "which yields a fix suggestion without a certificate)")
    if test_files is not None:
        if not isinstance(test_files, dict) \
                or not all(isinstance(v, str) for v in test_files.values()):
            raise ValueError("test_files must be a {name: content} map")
        _check_filenames(test_files)
        if len(test_files) > _CUSTOM_MAX_FILES:
            raise ValueError(f"too many test files (>{_CUSTOM_MAX_FILES})")
        if not all(n.startswith("test_") and n.endswith(".py")
                   for n in test_files):
            raise ValueError("test files must be named test_*.py")
    total = sum(len(v.encode("utf-8")) for v in source_files.values()) \
        + sum(len(v.encode("utf-8"))
              for v in (test_files or {}).values())
    if total > _CUSTOM_MAX_BYTES:
        raise ValueError(
            f"upload too large ({total} bytes > {_CUSTOM_MAX_BYTES})")
    repro = p.get("repro_snippet")
    if repro is not None and len(str(repro)) > 20_000:
        raise ValueError("repro_snippet too large (>20000 chars)")
    violations = custom_quarantine_scan(
        {**source_files, **(test_files or {})})
    if violations:
        raise ValueError(
            "quarantine rejected the upload before any execution: "
            + json.dumps(violations, ensure_ascii=False))


def t_intake_submit_issue(p: dict) -> dict:
    """Create a scenario fixture from an external issue (CI webhook shape).

    Idempotent: the scenario id is derived from the content hash, so a
    retried webhook yields the same scenario instead of a duplicate run.
    """
    title = str(p.get("title", "")).strip()
    report = str(p.get("report", "")).strip()
    expected = str(p.get("expected_behavior", "")).strip()
    target = str(p.get("target", "")).strip()
    if len(title) < 8 or len(report) < 20 or len(expected) < 20:
        raise ValueError("an auditable notarization request needs "
                         "title (>=8 chars), report (>=20) and "
                         "expected_behavior (>=20)")
    files = p.get("files")
    source_files = p.get("source_files")
    if files is not None and source_files is not None:
        raise ValueError("'files' (patch review) and 'source_files' "
                         "(bring-your-own-code fix) are mutually exclusive")
    if source_files is None and target not in INTAKE_TARGETS:
        raise ValueError(f"unknown target {target!r}; registered targets: "
                         f"{sorted(INTAKE_TARGETS)} "
                         f"(or pass source_files for a custom target)")
    if source_files is not None:
        _validate_custom_upload(p, source_files)
    if files is not None:
        if not isinstance(files, dict) or not files \
                or not all(isinstance(v, str) for v in files.values()):
            raise ValueError("files must be a non-empty {name: content} map")
        _check_filenames(files)
        if not any(n.endswith(".py") for n in files):
            raise ValueError("uploaded change must include at least one "
                             ".py file")
    sid_seed = title + report + expected
    # Optional caller tag (e.g. CI run_tag "pr1-v2"): same content + same
    # tag = same scenario (webhook retry stays idempotent); same issue
    # re-delivered under a new tag = a NEW run alongside the old one.
    sid_seed += str(p.get("run_tag", "")).strip()
    if source_files is not None:
        sid_seed += json.dumps(source_files, sort_keys=True)
    sid = "intake_" + sha256_text(sid_seed)[:10]
    path = SCENARIOS_DIR / f"{sid}.json"
    if path.exists():
        raise ValueError(f"issue already intaken as scenario '{sid}'; "
                         f"intake is idempotent by content hash")
    fixture = {
        "scenario_id": sid,
        "mode": "external" if files else "inhouse",
        "title": title,
        "issue": {"id": f"ISSUE-{sid.removeprefix('intake_').upper()}",
                  "source": "external intake (CI webhook / console upload)",
                  "title": title, "report": report,
                  "expected_behavior": expected},
    }
    if source_files is not None:
        test_files = p.get("test_files") or {}
        fixture.update({
            "custom_target": True,
            "embedded_target_files": source_files,
            "embedded_baseline_tests": test_files,
            "target_files": sorted(source_files),
            "baseline_test_files": sorted(test_files),
        })
        if p.get("advisory"):
            fixture["advisory"] = True
        repro = str(p.get("repro_snippet", "")).strip()
        if repro:
            fixture["repro_snippet"] = repro
    else:
        fixture.update({
            "target_files": INTAKE_TARGETS[target]["target_files"],
            "baseline_test_files": INTAKE_TARGETS[target]["baseline_test_files"],
        })
    if files:
        fixture["submitted_change"] = {
            "author": str(p.get("author", "console-upload")).strip()
                      or "console-upload",
            "message": title, "files": files}
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return {"scenario_id": sid, "status": "registered",
            "mode": fixture["mode"],
            "next": f"POST /tools/{sid}/notary_sentinel.scan to begin"}


def t_skill_list(run: NotaryRun, _p: dict) -> dict:
    """Runtime discovery: every seed + registry skill, with load status."""
    skills = load_skills()
    return {
        "gateway_version": GATEWAY_VERSION,
        "skills": skills,
        "loaded": sum(1 for s in skills if s["status"] == "loaded"),
        "rejected": sum(1 for s in skills if s["status"] == "rejected"),
        "retired": sum(1 for s in skills if s["status"] == "retired"),
        "registry_append_only": True,
        "registry_index": str(SKILL_INDEX_PATH.relative_to(PKG_ROOT)),
    }


def t_skill_get(run: NotaryRun, p: dict) -> dict:
    """Fetch one skill's full content — refused if rejected or retired."""
    name = str(p.get("name", "")).strip()
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(f"unsafe skill name: {name!r}")
    skills = load_skills()
    for rec in skills:
        if rec["name"] == name:
            if rec["status"] == "retired":
                raise ValueError(
                    f"skill '{name}' has been retired (tombstoned in the "
                    f"registry index); refusing to serve it (fail-closed)")
            if rec["status"] != "loaded":
                raise ValueError(
                    f"skill '{name}' was rejected at load "
                    f"({rec.get('reason', 'unknown reason')}); "
                    f"refusing to serve it (fail-closed)")
            content = (PKG_ROOT / rec["path"]).read_text(encoding="utf-8")
            return {**rec, "content": content}
    available = sorted(s["name"] for s in skills)
    raise ValueError(f"unknown skill '{name}'; available: {available}")


def t_skill_match(run: NotaryRun, p: dict) -> dict:
    """Decision-table match: trigger signal -> the one skill to consult.

    Accepts the machine key ("retry-duplicate-symptom") or the verbatim
    Chinese trigger text. Signals are mutually exclusive by design, so a
    match is always a single skill — agents never guess between skills.
    """
    signal = str(p.get("signal", "")).strip()
    if not signal:
        raise ValueError("signal must be non-empty; call notary_skill.list "
                         "or consult skills/match_table.json for valid signals")
    table = load_match_table()
    for entry in table["signals"]:
        if signal in (entry["signal"], entry["trigger"]):
            by_name = {s["name"]: s for s in load_skills()}
            if entry["skill"] not in by_name:
                return {"signal": entry["signal"], "trigger": entry["trigger"],
                        "skill": entry["skill"], "status": "not_yet_registered",
                        "note": "skill is a registry distillation not present "
                                "in this environment yet"}
            rec, chain = _resolve_latest(entry["skill"], by_name)
            if rec is None:
                raise ValueError(
                    f"signal '{entry['signal']}' points to skill "
                    f"'{entry['skill']}', but every member of its supersedes "
                    f"chain {chain} is retired or load-rejected; refusing "
                    f"to serve (fail-closed)")
            out = {"signal": entry["signal"], "trigger": entry["trigger"],
                   "skill": rec["name"], "status": "matched",
                   "version": rec.get("version"), "source": rec["source"],
                   "description": rec["description"],
                   "roles": entry.get("roles", [])}
            if rec["status"] == "probation":
                # 试用区命中：结果与命中日志都标注——"命中数据说话，
                # 人看数据决定"（追认制的评审依据）。
                out["governance"] = "probation"
                out["probation_note"] = (
                    "该 Skill 处于试用区：判断可参考，裁决仍由确定性门禁"
                    "与人工门兜底")
            if rec["name"] != entry["skill"]:
                out["resolved_from"] = entry["skill"]
                out["supersedes_chain"] = chain
            if run is not None:
                log_path = run.run_dir / "evidence" / "skill_matches.json"
                log = json.loads(log_path.read_text(encoding="utf-8"))                     if log_path.exists() else []
                log.append({"ts": time.time(), "role": p.get("role"),
                            "signal": entry["signal"],
                            "skill": rec["name"],
                            "version": rec.get("version"),
                            "governance": rec["status"]})
                run._write_json(log_path, log)
            return out
    valid = sorted(e["signal"] for e in table["signals"])
    raise ValueError(f"unknown trigger signal {signal!r}; "
                     f"valid signals: {valid}")


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
    "notary_flow.request_rework": t_request_rework,
    "notary_flow.dispute": t_dispute,
    "notary_flow.adjudicate": t_adjudicate,
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
    "notary_state.reverify_checkpoint": t_reverify_checkpoint,
    "notary_flow.resolve_human": t_resolve_human,
    "notary_release.deploy": t_deploy,
    "notary_release.rollback": t_rollback,
    "notary_evidence.seal": t_seal,
    "notary_evidence.list": t_list_evidence,
    "notary_skill.register": t_register_skill,
    "notary_skill.retire": t_retire_skill,
    "notary_skill.confirm": t_confirm_skill,
    "notary_skill.list": t_skill_list,
    "notary_skill.get": t_skill_get,
    "notary_skill.match": t_skill_match,
}


# ---------------------------------------------------------------------------
# Observability: Prometheus text exposition (GET /metrics)
# ---------------------------------------------------------------------------

def render_metrics() -> str:
    """Prometheus text exposition of all known runs.

    This is the concrete anchoring point for observability products: any
    Prometheus/Grafana-compatible stack can scrape the gateway directly.
    Values are recomputed from trace files on every scrape (the gateway
    holds no metric state of its own).
    """
    lines = [
        "# HELP codenotary_gateway_info Gateway version info",
        "# TYPE codenotary_gateway_info gauge",
        f'codenotary_gateway_info{{version="{GATEWAY_VERSION}"}} 1',
        "# HELP codenotary_run_state Current pipeline state per run",
        "# TYPE codenotary_run_state gauge",
    ]
    for sid, run in sorted(RUNS.items()):
        lines.append(f'codenotary_run_state{{run="{sid}",'
                     f'state="{run.sm.state}"}} 1')
    lines += [
        "# HELP codenotary_tool_calls_total Tool calls by run, tool, role",
        "# TYPE codenotary_tool_calls_total counter",
    ]
    sec_lines = ["# HELP codenotary_security_events_total Role-policy "
                 "denials per run",
                 "# TYPE codenotary_security_events_total counter"]
    rework_lines = ["# HELP codenotary_rework_rounds Rework rounds used "
                    "vs budget",
                    "# TYPE codenotary_rework_rounds gauge"]
    for sid, run in sorted(RUNS.items()):
        sec_lines.append(
            f'codenotary_security_events_total{{run="{sid}"}} '
            f'{len(run.security_events)}')
        rework_lines.append(
            f'codenotary_rework_rounds{{run="{sid}"}} {run.rework_round}')
        if run.trace_path.exists():
            counts: dict[tuple[str, str], int] = {}
            for line in run.trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                key = (e["tool"], e.get("role", "unknown"))
                counts[key] = counts.get(key, 0) + 1
            for (tool, role), n in sorted(counts.items()):
                lines.append(f'codenotary_tool_calls_total{{run="{sid}",'
                             f'tool="{tool}",role="{role}"}} {n}')
    lines += sec_lines + rework_lines + [""]
    return "\n".join(lines)


# Run-agnostic reads: served without a run (and never create/reset one).
RUNLESS_TOOLS = {"notary_skill.list", "notary_skill.get",
                 "notary_skill.match", "notary_skill.confirm",
                 "notary_skill.retire", "notary_state.reverify_checkpoint"}


class NotaryHandler(BaseHTTPRequestHandler):
    server_version = "CodeNotaryGateway/0.8"

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
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
            if parts == ["metrics"]:
                self._send_text(HTTPStatus.OK, render_metrics())
                return
            if parts == ["policy"]:
                self._send(HTTPStatus.OK, {
                    "ok": True,
                    "result": {
                        "gateway_version": GATEWAY_VERSION,
                        "role_policy": {k: sorted(v)
                                        for k, v in sorted(ROLE_POLICY.items())},
                        "entry_states": {k: sorted(v)
                                         for k, v in sorted(ENTRY_STATES.items())},
                        "known_roles": sorted(KNOWN_ROLES),
                        "config": CONFIG,
                    }})
                return
            if parts == ["scenarios"]:
                self._send(HTTPStatus.OK,
                           {"ok": True, "result": list_scenarios()})
                return
            if len(parts) == 3 and parts[0] == "tools" and parts[2] == "trace":
                run = get_run(parts[1])
                trace = (run.trace_path.read_text(encoding="utf-8")
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
        role: str | None = None
        trace_event: str | None = None
        try:
            if len(parts) != 3 or parts[0] != "tools":
                self._send(HTTPStatus.NOT_FOUND, {
                    "ok": False,
                    "error": "expected /tools/{scenario_id}/{tool_call}"})
                return
            scenario_id, tool_call = parts[1], parts[2]
            payload = self._read_json()
            raw_role = payload.get("role")
            if raw_role is not None:
                role = str(raw_role).strip().lower() or None
            if tool_call == "reset":
                RUNS.pop(scenario_id, None)
                _RESET_OK.add(scenario_id)  # 显式授权下一次重建清目录
                result: Any = {"scenario_id": scenario_id, "status": "reset"}
            elif tool_call == "notary_intake.submit_issue":
                # Role check BEFORE the scenario is minted (a denied intake
                # creates nothing). The successful call is then traced
                # against the newly created run (audit from birth).
                role_check = ROLE_POLICY.get(tool_call)
                if role is not None and role_check is not None \
                        and role not in role_check:
                    raise ValueError(
                        f"role '{role}' is not permitted to call {tool_call} "
                        f"(allowed: {sorted(role_check)})")
                result = t_intake_submit_issue(payload)
                run = get_run(result["scenario_id"])
            else:
                if tool_call not in TOOLS:
                    raise ValueError(
                        f"unknown tool call '{tool_call}', available: "
                        + ", ".join(sorted(TOOLS)))
                if tool_call in RUNLESS_TOOLS and scenario_id not in RUNS:
                    # run-agnostic skill ops: serve without creating (or
                    # resetting!) a run. No run -> no per-run trace write.
                    # Role policy still applies (fail-closed); a denied
                    # runless call simply has no run to record the event in.
                    allowed = ROLE_POLICY.get(tool_call)
                    if role is not None and allowed is not None                             and role not in allowed:
                        raise ValueError(
                            f"role '{role}' is not permitted to call "
                            f"{tool_call} (allowed: {sorted(allowed)})")
                    result = TOOLS[tool_call](None,
                                              {**payload, "_sid": scenario_id})
                    self._send(HTTPStatus.OK, {"ok": True, "result": result})
                    return
                run = get_run(scenario_id)
                allowed = ROLE_POLICY.get(tool_call)
                if role is not None and allowed is not None \
                        and role not in allowed:
                    detail = (f"role '{role}' is not permitted to call "
                              f"{tool_call} (allowed: {sorted(allowed)})")
                    run.record_security_event(tool_call, role, detail)
                    trace_event = "role-denied"
                    raise ValueError(detail)
                entry = ENTRY_STATES.get(tool_call)
                if entry is not None and run.sm.state not in entry:
                    raise IllegalTransition(
                        f"{tool_call} not valid in state {run.sm.state}; "
                        f"entry requires one of: {sorted(entry)}")
                result = TOOLS[tool_call](run, payload)
            # Persist BEFORE responding: a crash between response and
            # checkpoint must never lose a committed state change (the
            # SIGKILL-after-response race found in finals prep).
            self._persist(run, tool_call, payload, result, started, role,
                          trace_event)
            self._send(HTTPStatus.OK, {"ok": True, "result": result})
        except (IllegalTransition, ValueError, KeyError, RuntimeError) as exc:
            result = {"error": str(exc)}
            self._persist(run, tool_call, payload, result, started, role,
                          trace_event)
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - gateway must not die on one call
            result = {"error": f"internal: {exc}"}
            self._persist(run, tool_call, payload, result, started, role,
                          trace_event)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"ok": False, "error": f"internal: {exc}"})

    def _persist(self, run, tool_call: str, payload: dict, result,
                 started: float, role, trace_event) -> None:
        if run is None or not tool_call:
            return
        ms = (time.monotonic() - started) * 1000
        try:
            run.log(tool_call, payload, result, ms,
                    role=role, event=trace_event)
            run.checkpoint()
        except OSError:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the CodeNotary HTTP notary tool gateway.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=18090, type=int)
    parser.add_argument("--config", default=str(PKG_ROOT / "notary.json"),
                        help="team policy file; unreadable/unknown-key "
                             "config refuses startup (fail-closed)")
    args = parser.parse_args()
    load_config(Path(args.config))

    SKILL_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    restored = restore_all_runs()
    if restored:
        print(f"resumed {restored} run(s) from checkpoints")
    server = ThreadingHTTPServer((args.host, args.port), NotaryHandler)
    print(f"CodeNotary notary tool gateway listening on "
          f"http://{args.host}:{args.port}")
    print("Health: GET /health")
    print("Metrics: GET /metrics (Prometheus text exposition)")
    print("Tool call: POST /tools/{scenario_id}/{tool_call}")
    server.serve_forever()




def _force_utf8_stdio() -> None:
    """Cross-platform output safety: Chinese Windows consoles are GBK, and
    printing Unicode status marks would crash the process. Force UTF-8."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


if __name__ == "__main__":
    _force_utf8_stdio()
    main()
