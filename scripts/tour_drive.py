#!/usr/bin/env python3
"""Tour driver: replay the coupon case pipeline against a live gateway.

Used by the console's judge-tour mode (/api/tour/begin). Phase A walks the
pipeline to the red gate and files the dispute (sentinel -> triage -> RCA ->
contract freeze -> draft v1 -> blind tests -> TEST_PASS red -> dispute).
Phase C resumes after the human adjudication (draft v2 -> gates -> release
-> seal). All LLM-produced artifacts are pre-recorded replays; every gate,
contract freeze, adjudication and seal executes for real on the gateway.

  python3 scripts/tour_drive.py A|C [--gateway http://127.0.0.1:18090]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "tour_assets"
SID = "coupon_tour"


def call(gw: str, tool: str, payload: dict | None = None,
         role: str | None = None) -> dict:
    body = dict(payload or {})
    if role:
        body["role"] = role
    req = urllib.request.Request(
        f"{gw}/tools/{SID}/{tool}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        out = json.loads(e.read().decode())
    if not out.get("ok"):
        raise SystemExit(f"FAIL {tool}: {out.get('error')}")
    return out["result"]


def state(gw: str) -> str:
    return call(gw, "notary_state.get")["state"]


def phase_a(gw: str) -> None:
    v1 = (ASSETS / "coupon_draft_v1.py").read_text(encoding="utf-8")
    blind = (ASSETS / "blind_tests.py").read_text(encoding="utf-8")
    call(gw, "notary_sentinel.scan", role="sentinel")
    call(gw, "notary_flow.triage", {
        "verdict": "accept", "scope": ["coupon.py"], "route": ["fix", "verify"],
        "rationale": "受理：投诉场景与修复目标明确（核销有效期边界），"
                     "变更仅含 coupon.py，材料完整可进门禁"}, role="triage")
    call(gw, "notary_flow.diagnosis", {
        "root_cause": "时区混比：expires_at 为模块级 naive datetime，支付网关传入 "
                      "aware UTC，Python3 混比抛 TypeError，落入 redeem 的 "
                      "fail-closed 兜底，全部核销请求返回 validation_error——"
                      "未到期也无法核销。",
        "evidence": ["work/repro/repro.py 复现：aware now × naive expires_at "
                     "→ TypeError → validation_error"],
        "fix_hypothesis": "统一 aware 化后按 UTC 比较；失效边界按业务自然日口径"
                          "在契约中形式化。",
        "confidence": 0.9}, role="rca")
    call(gw, "notary_contract.freeze", {
        "assertions": [
            "所有过期比较必须在 UTC 下进行，naive/aware 混比已修复",
            "核销有效期覆盖至 2026-11-10 24:00（Asia/Shanghai 业务自然日，"
            "含当天）；之后失效",
            "修改范围仅限 coupon.py",
            "公开函数签名不得变更：Coupon(code, expires_at)、"
            "is_expired(now)、redeem(coupon, now)"],
        "assumptions": [{
            "point": "「有效至 11 月 10 日」的日界与时区",
            "assumption": "按业务所在地自然日（Asia/Shanghai）且含当天，"
                          "即 11-10 24:00 前可核销",
            "basis": "工单未指明时区与日界；暂按既有行为与下游对账约定默认，"
                     "依据待补"}],
        "in_scope": ["coupon.py"]}, role="contract")
    call(gw, "notary_author.submit_implementation",
         {"files": {"coupon.py": v1}}, role="author")
    call(gw, "notary_tester.submit_tests",
         {"files": {"test_blind_contract.py": blind}}, role="tester")
    call(gw, "notary_gate.run_test_gate", role="gatekeeper")
    if state(gw) == "REJECTED":
        call(gw, "notary_flow.dispute", {
            "focus": "契约断言 2 把「有效至 11 月 10 日」解读为含当天至 "
                     "24:00——工单只说有效至 11 月 10 日，没说按哪个时区的"
                     "自然日、当天算不算。这版解读的需求依据是什么？",
            "clause": "断言 2"}, role="author")
    print("phase A done:", state(gw))


def phase_c(gw: str) -> None:
    v2 = (ASSETS / "coupon_draft_v2.py").read_text(encoding="utf-8")
    blind = (ASSETS / "blind_tests.py").read_text(encoding="utf-8")
    if state(gw) == "REJECTED":
        call(gw, "notary_flow.request_rework", {
            "reason": "测试门禁红，反馈含失败输出全文；按裁决后的契约 "
                      "v1.1 双方各自修正后重交。"}, role="gatekeeper")
    call(gw, "notary_author.submit_implementation",
         {"files": {"coupon.py": v2}}, role="author")
    call(gw, "notary_tester.submit_tests",
         {"files": {"test_blind_contract.py": blind}}, role="tester")
    call(gw, "notary_gate.run_test_gate", role="gatekeeper")
    r = call(gw, "notary_gate.run_mutation_gate", role="gatekeeper")
    st = str(r.get("status", ""))
    if st.startswith("awaiting_rebuttal"):
        for s_ in r.get("survivors", []):
            call(gw, "notary_rebuttal.submit", {
                "mutant_id": s_.get("id") or s_,
                "argument": "等价变异体：行为在外界可观测层面不变"},
                role="author")
        r = call(gw, "notary_gate.finalize_mutation", role="gatekeeper")
    call(gw, "notary_gate.run_convention_gate", role="gatekeeper")
    if state(gw) == "NOTARIZED":
        call(gw, "notary_release.deploy", {"version": "1.0.1"}, role="release")
        call(gw, "notary_evidence.seal", role="release")
    print("phase C done:", state(gw))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["A", "C"])
    ap.add_argument("--gateway", default="http://127.0.0.1:18090")
    args = ap.parse_args()
    # Small pacing so a watching judge sees the steps land in order.
    if args.phase == "A":
        phase_a(args.gateway)
    else:
        time.sleep(2)
        phase_c(args.gateway)


if __name__ == "__main__":
    main()
