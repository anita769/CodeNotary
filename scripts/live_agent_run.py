#!/usr/bin/env python3
"""Live LLM-driven pipeline run (the rehearsal-day "seven-role real run").

Drives a scenario through the REAL gateway with every judgment call made
by a live LLM (DeepSeek), not canned replay outputs: triage / diagnosis /
contract drafting / blind-test authoring / mutation rebuttals are all
LLM-produced, validated against the gateway's own rules, and retried with
the gateway's error text as feedback. Deterministic steps (sentinel scan,
gates, deploy, seal) execute for real exactly as in eval replay.

Blind partitions are enforced by the gateway, not by the prompt: the
tester LLM receives only what notary_tester.get_context returns (contract
+ baseline public tests), never the implementation.

Usage:
  CODENOTARY_LLM_KEY=sk-... python3 scripts/live_agent_run.py SCENARIO_ID \
      [--gateway http://127.0.0.1:18090]

The LLM key comes from the environment only — never commit it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

LLM_BASE = os.environ.get("CODENOTARY_LLM_BASE", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("CODENOTARY_LLM_MODEL", "deepseek-v4-flash")
LLM_KEY = os.environ.get("CODENOTARY_LLM_KEY")

GATEWAY = "http://127.0.0.1:18090"


class StepFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def call(sid: str, tool: str, payload: dict | None = None,
         role: str | None = None, allow_fail: bool = False) -> dict:
    body = dict(payload or {})
    if role:
        body["role"] = role
    req = urllib.request.Request(
        f"{GATEWAY}/tools/{sid}/{tool}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        out = json.loads(exc.read().decode())
    if not out.get("ok") and not allow_fail:
        raise StepFailure(f"{tool}: {out.get('error')}")
    return out.get("result", out)


def llm_json(system: str, user: str, retries: int = 3,
             max_tokens: int = 14000) -> dict:
    """Ask the LLM for a JSON object; feed validation/parse errors back."""
    if not LLM_KEY:
        raise StepFailure("CODENOTARY_LLM_KEY not set")
    err = ""
    for attempt in range(retries):
        prompt = user + (f"\n\n上一次输出被拒：{err}\n请修正后重新输出。"
                         if err else "")
        req = urllib.request.Request(
            f"{LLM_BASE}/chat/completions",
            data=json.dumps({
                "model": LLM_MODEL, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
            }).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {LLM_KEY}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"].get("content") or ""
        except Exception as exc:  # network/HTTP — retry as-is
            err = str(exc)
            continue
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            err = f"未找到 JSON 对象（原始 {len(text)} 字符：{text[:80]!r}）"
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            err = f"JSON 解析失败：{exc}"
    raise StepFailure(f"LLM 连续 {retries} 次未给出可用 JSON：{err}")


SYS = ("你是 CodeNotary 可信交付流水线的{role}角色。只输出一个 JSON 对象，"
       "不要 markdown 围栏，不要解释文字。字段缺失或不合规会被流水线拒绝，"
       "被拒绝时按错误提示修正重发。")


def step(name: str, fn) -> dict:
    t0 = time.time()
    out = fn()
    print(f"  [{time.time() - t0:6.1f}s] {name}", flush=True)
    return out


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--gateway", default="http://127.0.0.1:18090")
    ap.add_argument("--rework", action="store_true",
                    help="REJECTED run 的重修闭环：退回→作者带失败反馈重写→重过门禁")
    args = ap.parse_args()
    global GATEWAY
    GATEWAY = args.gateway
    sid = args.scenario
    t_start = time.time()
    print(f"=== live run: {sid} (model={LLM_MODEL}) ===", flush=True)

    if args.rework:
        # L4 重修闭环：REJECTED → AUTHORING → 作者带失败反馈重写 → 重过门禁
        prev_verdicts = {}
        rg = step("重修退回（leader）", lambda: call(
            sid, "notary_flow.request_rework",
            {"reason": "门禁未通过，作者按失败反馈重写（重修预算内）"},
            role="leader"))
        print(f"  重修轮次 {rg.get('rework_round')}/{rg.get('budget')}",
              flush=True)
        verdicts = call(sid, "notary_verdicts.list", role="author")
        # 失败反馈要带测试输出全文（结论摘要不足以定位），截断防爆上下文
        vlist = verdicts.get("verdicts", verdicts)
        if isinstance(vlist, dict):
            for v in vlist.values():
                if isinstance(v, dict) and v.get("test_output"):
                    v["test_output"] = v["test_output"][-3000:]
        mode = call(sid, "notary_change.get_issue", role="triage").get("mode")
        if mode == "external":
            # 外部模式的对抗环是 tester 侧：补丁不可动，盲测可重写
            # （实证：盲测幻觉接口误伤——测试引用了实现没有的属性）
            tester_ctx = call(sid, "notary_tester.get_context", role="tester")
            tests = step("盲测重写（LLM，带失败反馈）", lambda: llm_json(
                SYS.format(role="盲测"), retries=6, max_tokens=14000,
                user='你上一轮写的盲测本身有误（引用了实现中不存在的属性/方法'
                     '造成误伤）。重写验收测试。输出 JSON：{"files": '
                     '{"test_blind_contract.py": "..."}}。硬性要求：'
                     "unittest TestCase 类式；**接口用法严格以基线公开测试为准"
                     "（import 路径、构造方式、可调用的公开属性/方法），"
                     "基线没出现过的属性一律不许用**；每条契约断言至少一个用例。"
                     "\n\n"
                     f"【契约】{json.dumps(tester_ctx.get('contract', {}), ensure_ascii=False)[:2500]}\n"
                     f"【基线公开测试】{json.dumps(tester_ctx.get('baseline_tests', {}), ensure_ascii=False)[:2500]}\n"
                     f"【上轮失败输出】{json.dumps(vlist, ensure_ascii=False)[-3500:]}"))
            step("盲测重交", lambda: call(
                sid, "notary_tester.submit_tests", tests, role="tester"))
            tg = step("测试门禁（重跑）", lambda: call(
                sid, "notary_gate.run_test_gate", role="gatekeeper"))
            print(f"  测试门禁：{tg.get('decision')}", flush=True)
            if tg.get("decision") == "green":
                mg = step("变异门禁（重跑）", lambda: call(
                    sid, "notary_gate.run_mutation_gate", role="gatekeeper"))
                print(f"  变异门禁：{(mg.get('verdict') or mg).get('decision')}",
                      flush=True)
            cg = step("规范门禁（重跑）", lambda: call(
                sid, "notary_gate.run_convention_gate", role="gatekeeper"))
            print(f"  规范门禁：{cg.get('decision')}", flush=True)
            state = call(sid, "notary_state.get")["state"]
            if state == "NOTARIZED":
                step("发布", lambda: call(sid, "notary_release.deploy",
                                          {"version": "1.0.1-live"},
                                          role="release"))
                state = call(sid, "notary_state.get")["state"]
            step("重封印", lambda: call(sid, "notary_evidence.seal",
                                        role="release", allow_fail=True))
            print(f"=== {state} 用时 {time.time() - t_start:.0f}s ===",
                  flush=True)
            return 0
        author_ctx = step("作者取上下文", lambda: call(
            sid, "notary_author.get_context", role="author"))
        impl = step("作者重修（LLM，带失败反馈）", lambda: llm_json(
            SYS.format(role="修复"), retries=6, max_tokens=32000,
            user="上一次修复未通过门禁。按失败反馈重写源码。输出 JSON："
                 "{\"files\": {\"文件名.py\": \"完整源码\"}}。"
                 "只改契约范围内文件，保留对外接口。\n\n"
                 f"【契约】{json.dumps(author_ctx.get('contract', {}), ensure_ascii=False)[:2000]}\n"
                 f"【源码】{json.dumps(author_ctx.get('source', {}), ensure_ascii=False)[:3000]}\n"
                 f"【失败反馈（含测试输出）】{json.dumps(vlist, ensure_ascii=False)[:6000]}"))
        step("作者提交", lambda: call(
            sid, "notary_author.submit_implementation", impl, role="author"))
        tg = step("测试门禁（重跑）", lambda: call(
            sid, "notary_gate.run_test_gate", role="gatekeeper"))
        print(f"  测试门禁：{tg.get('decision')}", flush=True)
        if tg.get("decision") == "green":
            mg = step("变异门禁（重跑）", lambda: call(
                sid, "notary_gate.run_mutation_gate", role="gatekeeper"))
            print(f"  变异门禁：{(mg.get('verdict') or mg).get('decision')}",
                  flush=True)
        cg = step("规范门禁（重跑）", lambda: call(
            sid, "notary_gate.run_convention_gate", role="gatekeeper"))
        print(f"  规范门禁：{cg.get('decision')}", flush=True)
        state = call(sid, "notary_state.get")["state"]
        if state == "NOTARIZED":
            step("发布", lambda: call(sid, "notary_release.deploy",
                                      {"version": "1.0.1-live"},
                                      role="release"))
            state = call(sid, "notary_state.get")["state"]
        step("重封印", lambda: call(sid, "notary_evidence.seal",
                                    role="release", allow_fail=True))
        print(f"=== {state} 用时 {time.time() - t_start:.0f}s ===",
              flush=True)
        return 0

    call(sid, "reset", allow_fail=True)
    issue = step("intake: get_issue",
                 lambda: call(sid, "notary_change.get_issue", role="triage"))
    change = {}
    fixture_mode = issue.get("mode")
    if fixture_mode == "external":
        change = step("intake: get_submitted_change",
                      lambda: call(sid, "notary_change.get_submitted_change",
                                   role="triage"))

    scan = step("哨兵扫描（确定性）",
                lambda: call(sid, "notary_sentinel.scan", role="sentinel"))
    findings = scan.get("findings", [])
    critical = [f for f in findings if f.get("severity") == "critical"]

    if critical:
        # 检疫政策是确定性的：critical 一票隔离，不走 LLM 判断
        step("分诊：critical 一票隔离", lambda: call(
            sid, "notary_flow.triage",
            {"verdict": "reject", "scope": [],
             "route": [],
             "rationale": "哨兵发现 critical 级风险："
                          + "；".join(f["label"] for f in critical)},
            role="triage"))
        step("封印证据", lambda: call(sid, "notary_evidence.seal",
                                      role="release"))
        print(f"=== QUARANTINED （检疫隔离） 用时 "
              f"{time.time() - t_start:.0f}s ===")
        return 0

    change_text = json.dumps(change.get("submitted_change", {}),
                             ensure_ascii=False)[:4000]
    change_section = (f"【送审变更】{change_text}\n" if fixture_mode == "external"
                      else "【送审变更】（内部工单：无外部补丁，修复由流水线"
                           "作者角色按契约编写，属正常受理范围）\n")
    triage = step("分诊（LLM）", lambda: llm_json(
        SYS.format(role="分诊"),
        "你是公证处的受理窗口，职责只有一项：判断这份送审材料是否可受理"
        "（材料完整、诉求明确、可检验）。**不要评判补丁的对错**——补丁是否"
        "正确由后续盲测和确定性门禁判定，你发现它有问题也照常受理，"
        "让门禁去拦。只有材料残缺、诉求不可检验、或明显超出受理范围时才 "
        "reject。输出 JSON：{\"verdict\": \"accept\" 或 \"reject\", "
        "\"scope\": [文件名...], \"route\": [\"rca\",\"contract\",\"author\","
        "\"tester\",\"gates\"], \"rationale\": \"一句话受理理由\"}\n\n"
        f"【工单】{json.dumps(issue.get('issue', {}), ensure_ascii=False)[:2000]}\n"
        f"{change_section}"
        f"【哨兵发现】{json.dumps(findings, ensure_ascii=False)[:1500]}"))
    if triage.get("verdict") not in ("accept", "reject"):
        triage["verdict"] = "accept"
    triage.setdefault("route", ["rca", "contract", "author", "tester",
                                "gates"])
    step("分诊提交", lambda: call(sid, "notary_flow.triage", triage,
                                  role="triage"))
    if triage["verdict"] == "reject":
        step("封印证据", lambda: call(sid, "notary_evidence.seal",
                                      role="release"))
        print(f"=== REJECTED at triage 用时 {time.time() - t_start:.0f}s ===")
        return 0

    repro = ""
    if fixture_mode == "inhouse":
        repro = step("根因复现（确定性）", lambda: call(
            sid, "notary_flow.reproduce", role="rca")).get("stdout", "")
    diagnosis = step("根因分析（LLM）", lambda: llm_json(
        SYS.format(role="根因分析"),
        "基于工单定位根因。输出 JSON：{\"root_cause\": \"...\", "
        "\"evidence\": [\"...\"], \"repro\": \"...\", "
        "\"fix_hypothesis\": \"...\", \"confidence\": 0.0-1.0}\n\n"
        f"【工单】{json.dumps(issue.get('issue', {}), ensure_ascii=False)[:2000]}\n"
        f"【复现输出】{repro[:1500]}\n【送审变更】{change_text[:2000]}"))
    diagnosis["repro"] = repro
    step("根因提交", lambda: call(sid, "notary_flow.diagnosis", diagnosis,
                                 role="rca"))

    contract = step("契约起草（LLM）", lambda: llm_json(
        SYS.format(role="契约"),
        "把验收标准冻结成可检验断言。输出 JSON：{\"assertions\": [\"...\"], "
        "\"in_scope\": [文件名...], \"out_of_scope\": []}。"
        "每条 assertion 是一句可验证的中文陈述（至少 8 字），"
        "必须能被单元测试判定真假。\n\n"
        f"【工单】{json.dumps(issue.get('issue', {}), ensure_ascii=False)[:2000]}\n"
        f"【根因】{json.dumps(diagnosis, ensure_ascii=False)[:2000]}\n"
        f"【检验范围候选】{triage.get('scope')}"))

    def freeze():
        return call(sid, "notary_contract.freeze", contract, role="contract")
    try:
        step("契约冻结", freeze)
    except StepFailure as exc:
        fix = llm_json(SYS.format(role="契约"),
                       f"冻结被拒：{exc}。请修正断言后重新输出完整 JSON。\n"
                       f"上次输出：{json.dumps(contract, ensure_ascii=False)}")
        contract.update(fix)
        step("契约冻结（修正）", freeze)

    if fixture_mode == "inhouse":
        author_ctx = step("作者取上下文", lambda: call(
            sid, "notary_author.get_context", role="author"))
        impl = step("作者修复（LLM）", lambda: llm_json(
            SYS.format(role="修复"),
            "按契约修改源码。输出 JSON：{\"files\": {\"文件名.py\": \"完整源码\"}}。"
            "只改契约范围内的文件，保留模块对外接口。\n\n"
            f"【契约】{json.dumps(author_ctx.get('contract', {}), ensure_ascii=False)[:2000]}\n"
            f"【源码】{json.dumps(author_ctx.get('source', {}), ensure_ascii=False)[:3000]}\n"
            f"【根因】{json.dumps(author_ctx.get('diagnosis', {}), ensure_ascii=False)[:1500]}",
            max_tokens=32000, retries=6))
        step("作者提交", lambda: call(
            sid, "notary_author.submit_implementation", impl, role="author"))

    tester_ctx = step("盲测取上下文", lambda: call(
        sid, "notary_tester.get_context", role="tester"))
    scope_files = (contract.get("in_scope") or triage.get("scope") or [])
    modules = [f[:-3] for f in scope_files if f.endswith(".py")]
    tests = step("盲测编写（LLM，不见实现）", lambda: llm_json(
        SYS.format(role="盲测"),
        "只依据冻结契约编写验收测试。输出 JSON：{\"files\": "
        "{\"test_blind_contract.py\": \"...\"}}。硬性要求：unittest 框架、"
        "TestCase 类式用例（不要 pytest 函数式，discover 不认）；"
        f"用 import 引入被测模块：{modules}；每条契约断言至少一个用例，"
        "边界场景优先。\n\n"
        f"【契约】{json.dumps(tester_ctx.get('contract', {}), ensure_ascii=False)[:2500]}\n"
        f"【基线公开测试（参考风格）】"
        f"{json.dumps(tester_ctx.get('baseline_tests', {}), ensure_ascii=False)[:1500]}\n"
        f"【提示】{tester_ctx.get('blind_notice', '')}"))

    def submit_tests():
        return call(sid, "notary_tester.submit_tests", tests, role="tester")
    try:
        step("盲测提交", submit_tests)
    except StepFailure as exc:
        fix = llm_json(SYS.format(role="盲测"),
                       f"提交被拒：{exc}。修正后重新输出完整 JSON。\n"
                       f"上次输出：{json.dumps(tests, ensure_ascii=False)[:2000]}")
        tests.update(fix)
        step("盲测提交（修正）", submit_tests)

    tg = step("测试门禁（确定性真跑）", lambda: call(
        sid, "notary_gate.run_test_gate", role="gatekeeper"))
    print(f"  测试门禁：{tg.get('decision')}", flush=True)

    if tg.get("decision") == "green":
        mg = step("变异门禁（确定性真跑）", lambda: call(
            sid, "notary_gate.run_mutation_gate", role="gatekeeper"))
        if str(mg.get("status", "")).startswith("awaiting_rebuttal"):
            survivors = call(sid, "notary_gate.get_survivors",
                             role="author")["survivors"]
            for sv in survivors:
                reb = llm_json(
                    SYS.format(role="修复"),
                    "以下变异体在测试后幸存。若它与原代码语义等价，给出等价性"
                    "论证；若不等价，说明它暴露了哪个缺失用例。输出 JSON："
                    "{\"kind\": \"equivalent_mutant\" 或 "
                    "\"missing_test\", \"justification\": \"...\"}\n\n"
                    f"【变异体】{json.dumps(sv, ensure_ascii=False)[:1200]}")
                if reb.get("kind") != "equivalent_mutant":
                    reb["kind"] = "missing_test"
                step(f"申辩 mutant#{sv.get('id')}", lambda reb=reb, sv=sv:
                     call(sid, "notary_rebuttal.submit",
                          {"mutant_id": sv["id"], "kind": reb["kind"],
                           "justification": reb["justification"]},
                          role="author"))
            mg = step("变异终裁", lambda: call(
                sid, "notary_gate.finalize_mutation", role="gatekeeper"))
        print(f"  变异门禁：{(mg.get('verdict') or {}).get('decision')}",
              flush=True)

    cg = step("规范门禁（确定性真跑）", lambda: call(
        sid, "notary_gate.run_convention_gate", role="gatekeeper"))
    print(f"  规范门禁：{cg.get('decision')}", flush=True)

    state = call(sid, "notary_state.get")["state"]
    if state == "NOTARIZED":
        step("发布（确定性）", lambda: call(
            sid, "notary_release.deploy", {"version": "1.0.0-live"},
            role="release"))
        state = call(sid, "notary_state.get")["state"]
    step("封印证据", lambda: call(sid, "notary_evidence.seal",
                                  role="release"))
    print(f"=== {state} 用时 {time.time() - t_start:.0f}s ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
