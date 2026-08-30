"""Skill decision-table coverage & match-accuracy audit.

Drives the REAL gateway (no LLM) and verifies four properties of the
Skill runtime (skills/match_table.json + seeds + registry):

  1. match accuracy — every trigger signal resolves via notary_skill.match
     to its declared skill, loaded and serveable (notary_skill.get works)
  2. trigger coverage — every signal names >= 1 evalset sample that
     semantically exercises it, and every referenced sample id exists in
     evalset/manifest.json
  3. no orphan skills — every loaded skill is reachable from >= 1 signal
  4. compat hygiene — zero skills rejected at load in a healthy package;
     unversioned skills are listed explicitly (audit visibility)

Output: evalset/skill_coverage.json (canonical, timestamp-free).

Usage:  python3 scripts/eval_skill_coverage.py
Exit:   0 iff all four audits pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
GATEWAY = PKG_ROOT / "tools" / "notary_gateway.py"
EVALSET = PKG_ROOT / "evalset"
MATCH_TABLE = PKG_ROOT / "skills" / "match_table.json"
PORT = 18096
BASE = f"http://127.0.0.1:{PORT}"
SCENARIO = "qb_inhouse_fix"  # any known scenario id; skill tools are run-agnostic


class AuditFailure(Exception):
    pass


def call(tool: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}/tools/{SCENARIO}/{tool}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
    if not body.get("ok"):
        raise AuditFailure(f"tool call failed {tool}: {body}")
    return body["result"]


def main() -> None:
    table = json.loads(MATCH_TABLE.read_text(encoding="utf-8"))
    manifest = json.loads(
        (EVALSET / "manifest.json").read_text(encoding="utf-8"))
    sample_ids = {s["id"] for s in manifest["samples"]}

    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--host", "127.0.0.1", "--port",
         str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows: list[dict] = []
    failures: list[str] = []
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"{BASE}/health", timeout=1):
                    break
            except Exception:
                time.sleep(0.2)

        listing = call("notary_skill.list")
        by_name = {s["name"]: s for s in listing["skills"]}
        rejected = [s for s in listing["skills"] if s["status"] == "rejected"]
        retired = [s for s in listing["skills"] if s["status"] == "retired"]
        unversioned = sorted(s["name"] for s in listing["skills"]
                             if s.get("compat_status") == "unversioned")

        # Audit 4: compat hygiene
        for s in rejected:
            failures.append(
                f"skill {s['name']!r} rejected at load: "
                f"{s.get('reason', '?')} — a healthy package loads clean")

        # Audit 5: registry index integrity — every registry skill must be
        # hash-verified against the append-only index (no tampered, no
        # unindexed); the index itself must exist when registry skills do.
        registry_skills = [s for s in listing["skills"]
                           if s["source"] == "registry"]
        for s in registry_skills:
            if s.get("compat_status") in ("tampered", "unindexed"):
                failures.append(
                    f"registry skill {s['name']!r} integrity failure: "
                    f"{s.get('reason', '?')}")
        index_path = PKG_ROOT / "skills" / "registry" / "index.json"
        index_entries = []
        if registry_skills and not index_path.exists():
            failures.append("registry skills exist but index.json is missing")
        elif index_path.exists():
            index_entries = json.loads(
                index_path.read_text(encoding="utf-8"))["entries"]
            files_on_disk = {p.stem for p in
                             (PKG_ROOT / "skills" / "registry").glob("*.md")}
            registered = {e["name"] for e in index_entries
                          if e.get("action") == "register"}
            for missing in sorted(registered - files_on_disk):
                failures.append(
                    f"index registers {missing!r} but the file is gone")

        # Audit 6: supersedes lineage — targets exist, chains acyclic
        for s in listing["skills"]:
            sup = s.get("supersedes")
            if sup and sup not in by_name:
                failures.append(
                    f"skill {s['name']!r} supersedes unknown {sup!r}")
        for s in listing["skills"]:
            seen: set[str] = set()
            cur: str | None = s["name"]
            while cur:
                if cur in seen:
                    failures.append(
                        f"supersedes cycle detected at {cur!r}")
                    break
                seen.add(cur)
                cur = next((n for n, r in by_name.items()
                            if r.get("supersedes") == cur), None)

        # Audit 7: retired skills are never served — get must refuse each
        for s in retired:
            try:
                call("notary_skill.get", {"name": s["name"]})
                failures.append(
                    f"retired skill {s['name']!r} is still serveable")
            except AuditFailure:
                pass  # expected: explicit refusal

        # Audits 1+2 per signal
        for entry in table["signals"]:
            signal, skill = entry["signal"], entry["skill"]
            row: dict = {"signal": signal, "declared_skill": skill,
                         "coverage": entry.get("coverage", [])}
            # 1. match accuracy
            try:
                res = call("notary_skill.match", {"signal": signal})
                row["matched_skill"] = res["skill"]
                row["match_status"] = res["status"]
                if res["skill"] != skill:
                    failures.append(
                        f"signal {signal!r} matched {res['skill']!r}, "
                        f"declared {skill!r}")
                if res["status"] != "matched":
                    failures.append(
                        f"signal {signal!r} -> {skill!r} status "
                        f"{res['status']!r} (expected 'matched')")
                else:
                    got = call("notary_skill.get", {"name": skill})
                    row["serveable"] = bool(got.get("content"))
                    if not row["serveable"]:
                        failures.append(
                            f"skill {skill!r} matched but not serveable")
            except AuditFailure as exc:
                row["match_status"] = "error"
                failures.append(f"signal {signal!r}: {exc}")
            # 2. trigger coverage
            cov = entry.get("coverage", [])
            if not cov:
                failures.append(f"signal {signal!r} has empty coverage")
            missing = [c for c in cov if c not in sample_ids]
            if missing:
                failures.append(
                    f"signal {signal!r} coverage references unknown "
                    f"samples: {missing}")
            rows.append(row)

        # Audit 3: no orphan skills
        mapped = {e["skill"] for e in table["signals"]}
        loaded = {n for n, s in by_name.items() if s["status"] == "loaded"}
        orphans = sorted(loaded - mapped)
        for o in orphans:
            failures.append(
                f"loaded skill {o!r} is reachable from no trigger signal")

        seeds = sum(1 for s in listing["skills"]
                    if s["source"] == "seed" and s["status"] == "loaded")
        registry = sum(1 for s in listing["skills"]
                       if s["source"] == "registry" and s["status"] == "loaded")

        ok = not failures
        out = {
            "summary": {
                "gateway_version": listing["gateway_version"],
                "signals_total": len(table["signals"]),
                "match_accuracy": (
                    f"{sum(1 for r in rows if r.get('match_status') == 'matched' and r.get('matched_skill') == r['declared_skill'])}"
                    f"/{len(table['signals'])}"),
                "skills_loaded_seeds": seeds,
                "skills_loaded_registry": registry,
                "skills_rejected": len(rejected),
                "skills_retired": sorted(s["name"] for s in retired),
                "skills_unversioned": unversioned,
                "registry_index_entries": len(index_entries),
                "orphan_skills": orphans,
                "audits_passed": ok,
            },
            "signals": rows,
        }
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    (EVALSET / "skill_coverage.json").write_text(
        json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")

    s = out["summary"]
    print(f"match accuracy : {s['match_accuracy']}")
    print(f"loaded skills  : {s['skills_loaded_seeds']} seeds + "
          f"{s['skills_loaded_registry']} registry "
          f"(rejected: {s['skills_rejected']}, "
          f"retired: {len(s['skills_retired'])}, "
          f"unversioned: {len(s['skills_unversioned'])})")
    print(f"index entries  : {s['registry_index_entries']} "
          f"(integrity verified)")
    print(f"orphan skills  : {s['orphan_skills'] or 'none'}")
    for f_ in failures:
        print(f"FAIL: {f_}")
    print(f"results -> {EVALSET / 'skill_coverage.json'}")
    if failures:
        sys.exit(1)
    print("SKILL RUNTIME AUDIT CLEAN: all signals match, all covered, "
          "no orphans, zero rejections")


if __name__ == "__main__":
    main()
