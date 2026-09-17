#!/usr/bin/env python3
"""Single-evidence-pack verification ("复算"): don't trust us, recompute.

Zero-LLM re-verification of one EvidencePack (zip or run directory):

  1. signature    — if evidence/manifest.sig.json exists, verify the
                    Ed25519 signature over manifest.json against the
                    notary public key (--pubkey or keys/notary_ed25519.pub)
  2. hash chain   — every file in manifest["files"] recomputes; the trace
                    is bound by its sealed prefix (append-only after seal)
  3. contract     — frozen_hash recomputes from the recorded context_refs
  4. gates        — re-run TEST_PASS / MUTATION / CONVENTION on the packed
                    code+tests and compare each sealed verdict's decision

Gates are re-executed with THIS REPO's gateway code (the law stays the
law); data comes only from the pack. Exit 0 iff everything recomputes.

Usage: python3 scripts/verify_evidence.py EVIDENCE.zip [--pubkey P]
       make verify EVIDENCE=evidence-pack-pr1.zip
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent


def load_gateway():
    spec = importlib.util.spec_from_file_location(
        "notary_gateway", PKG_ROOT / "tools" / "notary_gateway.py")
    gw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gw)
    return gw


def sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool | None, str]] = []

    def add(self, name: str, ok: bool | None, detail: str) -> None:
        self.rows.append((name, ok, detail))
        icon = {True: "✅", False: "❌", None: "⚠️"}[ok]
        print(f"  {icon} {name}: {detail}")

    @property
    def failed(self) -> bool:
        return any(ok is False for _, ok, _ in self.rows)


def verify_signature(run_dir: Path, pubkey: Path | None,
                     require: bool, rep: Report) -> None:
    sig_file = run_dir / "evidence" / "manifest.sig.json"
    if not sig_file.exists():
        rep.add("签名", not require,
                "未签名" + ("（--require-signature 视为失败）" if require
                            else "（封印可证未改动，身份未绑定）"))
        return
    sig = json.loads(sig_file.read_text(encoding="utf-8"))
    if pubkey is None:
        pubkey = PKG_ROOT / "keys" / "notary_ed25519.pub"
    if not pubkey.exists():
        rep.add("签名", False, f"包有签名但缺公钥 {pubkey}")
        return
    with tempfile.TemporaryDirectory() as td:
        sig_bin = Path(td) / "sig.bin"
        sig_bin.write_bytes(bytes.fromhex(sig["signature_hex"]))
        proc = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey",
             str(pubkey), "-rawin", "-in", str(run_dir / "manifest.json"),
             "-sigfile", str(sig_bin)],
            capture_output=True, text=True)
    if proc.returncode == 0:
        rep.add("签名", True,
                f"Ed25519 有效（指纹 {sig.get('pubkey_fingerprint', '?')}）")
    else:
        rep.add("签名", False, "签名验证失败——manifest 被换或密钥不符")


def verify_hash_chain(run_dir: Path, rep: Report) -> None:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files", manifest)  # new format or legacy flat
    bad = []
    for rel, h in files.items():
        p = run_dir / rel
        if not p.exists():
            bad.append(f"{rel} 缺失")
            continue
        actual = sha256_text(p.read_bytes().decode("utf-8", errors="replace"))
        if actual != h:
            bad.append(f"{rel} 哈希不符")
    rep.add("哈希链", not bad,
            f"{len(files)} 个文件全部吻合" if not bad else "；".join(bad))
    prefix = manifest.get("trace_prefix")
    if prefix and (run_dir / "trace.jsonl").exists():
        lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        n = prefix["lines"]
        if len(lines) < n:
            rep.add("trace 前缀", False,
                    f"trace 只有 {len(lines)} 行，少于封印的 {n} 行")
        else:
            ok = sha256_text("".join(lines[:n])) == prefix["sha256"]
            note = f"前 {n} 行吻合"
            if len(lines) > n:
                note += f"（封印后追加 {len(lines) - n} 行，append-only 正常）"
            rep.add("trace 前缀", ok, note)


def verify_contract(run_dir: Path, rep: Report) -> dict | None:
    cp = run_dir / "contract.json"
    if not cp.exists():
        rep.add("契约", None, "无契约（run 未到 CONTRACTED）")
        return None
    contract = json.loads(cp.read_text(encoding="utf-8"))
    canonical = {"issue_id": contract.get("issue_id", ""),
                 "assertions": contract.get("assertions") or [],
                 "context_refs": contract.get("context_refs")
                 or [str(run_dir / "diagnosis.json")]}
    if contract.get("assumptions"):
        canonical["assumptions"] = contract["assumptions"]
    recomputed = sha256_text(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    ok = recomputed == contract.get("frozen_hash")
    rep.add("契约", ok,
            f"frozen_hash 重算一致 {recomputed[:16]}…" if ok
            else "frozen_hash 重算不符——契约内容被改")
    return contract if ok else None


def verify_gates(run_dir: Path, scenario: dict, rep: Report,
                 gw) -> None:
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    impl = {k: v for k, v in (checkpoint.get("implementation") or {}).items()
            if k.endswith(".py")}
    blind = checkpoint.get("tests") or {}
    if not impl:
        rep.add("门禁重算", None, "无实现代码（inhouse 未交付或空 patch）")
        return
    # target sources + baseline tests: pack target/ first, else embedded,
    # else this repo's registry (legacy packs)
    target: dict[str, str] = {}
    target_dir = run_dir.parent.parent / "target"
    if target_dir.is_dir():
        target = {p.name: p.read_text(encoding="utf-8") for p in target_dir.iterdir()
                  if p.is_file()}
    if not target and scenario.get("embedded_target_files"):
        target = dict(scenario["embedded_target_files"])
    if not target:
        tdir = PKG_ROOT / "tools" / "notary_target"
        target = {n: (tdir / n).read_text(encoding="utf-8")
                  for n in scenario.get("target_files", [])
                  if (tdir / n).exists()}
    baseline: dict[str, str] = {}
    if scenario.get("embedded_baseline_tests") is not None:
        baseline = dict(scenario["embedded_baseline_tests"])
    elif target_dir.is_dir():
        baseline = {p.name: p.read_text(encoding="utf-8") for p in target_dir.iterdir()
                    if p.name.startswith("test_")}
    if not baseline:
        tdir = PKG_ROOT / "tools" / "notary_target"
        baseline = {n: (tdir / n).read_text(encoding="utf-8")
                    for n in scenario.get("baseline_test_files", [])
                    if (tdir / n).exists()}
    target.update(scenario.get("source_overrides", {}))
    sources = {**target, **impl}
    tests = {**baseline, **blind}
    sandbox = bool(scenario.get("custom_target"))
    verdicts = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                for p in (run_dir / "verdicts").glob("*.json")}

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td) / "w"
        # -- TEST_PASS --------------------------------------------------------
        gw._write_workdir(workdir, sources, tests)
        res = gw._run_unittest(workdir, sandbox=sandbox)
        decision = "green" if res["ok"] else "red"
        sealed = verdicts.get("test_pass", {}).get("decision")
        rep.add("门禁 TEST_PASS",
                sealed is not None and decision == sealed,
                f"重算 {decision}（{res.get('tests_ran', 0)} 个测试）"
                f" vs 封存 {sealed}")

        # -- MUTATION ---------------------------------------------------------
        sealed_m = verdicts.get("mutation")
        if sealed_m:
            mutants = []
            for fname, fsource in impl.items():
                for m in gw.generate_mutants(fsource):
                    m["file"] = fname
                    mutants.append(m)
            mutants = mutants[:gw.CONFIG["mutation"]["max_mutants"]]
            for i, m in enumerate(mutants, 1):
                m["id"] = f"M{i:02d}"
            killed, survived = 0, 0
            for m in mutants:
                msources = dict(sources)
                msources[m["file"]] = m["source"]
                gw._write_workdir(workdir, msources, tests)
                r = gw._run_unittest(workdir, sandbox=sandbox)
                if "SyntaxError" in r["output"]:
                    continue
                killed, survived = (killed + 1, survived) if not r["ok"] \
                    else (killed, survived + 1)
            exempted = sum(1 for rb in checkpoint.get("rebuttals", [])
                           if rb.get("kind") in ("equivalent_mutant",
                                                 "accept_fix"))
            score = gw.recompute_score(killed, survived, exempted)
            cfg = gw.CONFIG["mutation"]
            decision = ("green" if score >= cfg["green_above"]
                        else "red" if score < cfg["red_below"] else "yellow")
            rep.add("门禁 MUTATION", decision == sealed_m["decision"],
                    f"重算 score={score:.2f}（杀 {killed}/幸存 {survived}"
                    f"/豁免 {exempted}）→ {decision} vs 封存 "
                    f"{sealed_m['decision']}")

        # -- CONVENTION -------------------------------------------------------
        sealed_c = verdicts.get("convention")
        if sealed_c and (run_dir / "contract.json").exists():
            contract = json.loads((run_dir / "contract.json").read_text(encoding="utf-8"))
            result = gw.convention_check(impl, contract["in_scope"],
                                         contract["out_of_scope"])
            rep.add("门禁 CONVENTION",
                    result["decision"] == sealed_c["decision"],
                    f"重算 {result['decision']}（{len(result['findings'])} "
                    f"findings）vs 封存 {sealed_c['decision']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("evidence", help="evidence pack zip or run directory")
    ap.add_argument("--pubkey", type=Path)
    ap.add_argument("--require-signature", action="store_true")
    args = ap.parse_args()

    ev = Path(args.evidence)
    tmp = None
    if ev.suffix == ".zip":
        tmp = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(ev) as zf:
            zf.extractall(tmp.name)
        root = Path(tmp.name)
        runs = [d for d in (root / "runs").iterdir() if d.is_dir()] \
            if (root / "runs").is_dir() else []
        if len(runs) != 1:
            raise SystemExit("包内应恰有一个 run 目录")
        run_dir = runs[0]
    else:
        root, run_dir = ev.parent.parent, ev

    scenario_p = run_dir.parent.parent / "scenarios" / f"{run_dir.name}.json"
    if not scenario_p.exists():  # legacy: scenario next to repo scenarios/
        scenario_p = PKG_ROOT / "scenarios" / f"{run_dir.name}.json"
    scenario = json.loads(scenario_p.read_text(encoding="utf-8")) if scenario_p.exists() \
        else {}

    cfg = run_dir.parent.parent / "notary.json.snapshot"
    gw = load_gateway()
    if cfg.exists():
        gw.load_config(cfg)

    print(f"复算对象：{run_dir.name}\n")
    rep = Report()
    verify_signature(run_dir, args.pubkey, args.require_signature, rep)
    verify_hash_chain(run_dir, rep)
    verify_contract(run_dir, rep)
    if (run_dir / "checkpoint.json").exists() and scenario:
        verify_gates(run_dir, scenario, rep, gw)
    else:
        rep.add("门禁重算", None, "缺 checkpoint 或 scenario，跳过")

    print()
    if rep.failed:
        print("结论：⛔ 证据复算不一致——包被改动过，或判定无法重现")
        sys.exit(1)
    print("结论：✅ 证据复算一致——哈希链/契约/门禁 verdict 全部可重现")
    if tmp:
        tmp.cleanup()




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
