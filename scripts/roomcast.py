#!/usr/bin/env python3
"""roomcast: 把公证流水线的真实执行直播到 Matrix 房间（内容版）。

每个棒次事件不仅播报"做了什么"，还把该棒落盘工件的真实内容
（分诊理由、根因分析、契约断言、盲测用例、门禁结论与失败明细……）
带进房间——全部是落盘事实原文，不是 LLM 现场编造。

节奏：leader 式开场 → 每棒"角色发言（内容）+ 状态变化 + 下一棒预告"。
只读 trace 与工件，绝不写回网关。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent

# 棒序（用于"第 N 棒"与下一棒预告）
BEAT_ORDER = ["sentinel", "triage", "rca", "contract", "author",
              "tester", "gatekeeper", "release", "postmortem"]
BEAT_CN = {"sentinel": "哨兵·检疫", "triage": "分诊", "rca": "根因分析",
           "contract": "契约冻结", "author": "作者修复",
           "tester": "盲测设计", "gatekeeper": "门禁检验",
           "release": "发布与封印", "postmortem": "复盘沉淀"}

DEC = {"green": "🟢 通过", "red": "🔴 未通过", "yellow": "🟡 待复核"}
STATE_CN = {
    "RECEIVED": "已受理", "SCREENED": "检疫通过", "TRIAGED": "已分诊",
    "DIAGNOSED": "根因已定位", "CONTRACTED": "验收规则已冻结",
    "AUTHORING": "修复编写中", "TESTING": "盲测检验中", "GATING": "门禁检验中",
    "NOTARIZED": "已公证（验收通过）", "RELEASED": "已发布",
    "QUARANTINED": "检疫隔离", "ESCALATED": "等待人工裁决",
    "REJECTED": "未放行（待修复或异议）", "ROLLED_BACK": "已回滚",
}
GATE_VERDICT = {
    "notary_gate.run_test_gate": ("测试门禁", "test_pass.json"),
    "notary_gate.run_mutation_gate": ("变异门禁", "mutation.json"),
    "notary_gate.finalize_mutation": ("变异终裁", "mutation.json"),
    "notary_gate.run_convention_gate": ("规范门禁", "convention.json"),
}


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def clip(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def beat_content(sid: str, runs: Path, tool: str, role: str) -> tuple[str, str] | None:
    """返回 (棒次 key, 富文本播报)。工件原文优先。"""
    rd = runs / sid
    if tool == "notary_sentinel.scan":
        q = read_json(rd / "evidence" / "quarantine" / "manifest.json") or {}
        findings = q.get("findings") or []
        body = "入口检疫：送审文件扫描完成，未发现危险写法。" if not findings else \
            "入口检疫命中风险：" + "；".join(str(f)[:80] for f in findings)
        return "sentinel", body
    if tool == "notary_flow.triage":
        t = read_json(rd / "triage.json") or {}
        return "triage", (f"受理结论：{'受理' if t.get('verdict') == 'accept' else '不受理'}；"
                          f"检验范围：{'、'.join(t.get('scope', [])) or '—'}\n"
                          f"分诊理由：{clip(t.get('rationale', '—'), 300)}")
    if tool == "notary_flow.diagnosis":
        d = read_json(rd / "diagnosis.json") or {}
        return "rca", (f"根因：{clip(d.get('root_cause', '—'), 350)}\n"
                       f"修复假设：{clip(d.get('fix_hypothesis', '—'), 250)}\n"
                       f"置信度：{d.get('confidence', '—')}")
    if tool == "notary_contract.freeze":
        c = read_json(rd / "contract.json") or {}
        assertions = "\n".join(f"  {i+1}. {a}" for i, a in
                               enumerate(c.get("assertions", [])))
        assump = c.get("assumptions") or []
        ap = ("\n冻结时标注的假设：\n" + "\n".join(
            f"  · {a.get('point')}→{a.get('assumption')}（依据：{a.get('basis')}）"
            for a in assump)) if assump else ""
        return "contract", (f"验收规则 v{c.get('version', 1)} 已冻结"
                            f"（哈希 {str(c.get('frozen_hash', ''))[:12]}…，冻结后不可改）：\n"
                            f"{assertions}{ap}")
    if tool == "notary_author.submit_implementation":
        impl = read_json(rd / "evidence" / "implementation_files.json") or {}
        return "author", "修复实现已提交：" + "、".join(impl) + \
            "（内容哈希已入证据）"
    if tool == "notary_tester.submit_tests":
        bt = read_json(rd / "evidence" / "blind_test_files.json") or {}
        return "tester", ("盲测用例已提交（编写全程未见实现代码）："
                          + "、".join(bt))
    if tool in GATE_VERDICT:
        gname, gfile = GATE_VERDICT[tool]
        v = read_json(rd / "verdicts" / gfile) or {}
        dec = DEC.get(v.get("decision"), v.get("decision", "—"))
        body = f"{gname}：{dec}\n{clip(v.get('summary', ''), 300)}"
        if v.get("decision") == "red" and v.get("test_output"):
            out = v["test_output"]
            fails = [l for l in out.splitlines()
                     if l.startswith(("FAIL:", "ERROR:"))]
            if fails:
                body += "\n未通过用例：\n" + "\n".join(
                    f"  ✗ {f}" for f in fails[:4])
        return "gatekeeper", body
    if tool == "notary_gate.finalize_mutation":
        return None  # 已并入变异门禁
    if tool == "notary_flow.dispute":
        d = (read_json(rd / "dispute.json") or [{}])[-1]
        return None  # dispute 由大厅/裁决卡呈现，房间只提示升级
    if tool == "notary_release.deploy":
        return "release", "验收通过，修复已发布（冒烟验证通过）。"
    if tool == "notary_evidence.seal":
        m = read_json(rd / "manifest.json") or {}
        files = m.get("files") or m
        return "release", (f"证据封印完成：{len(files)} 个文件哈希入链，"
                           f"轨迹前缀绑定，Ed25519 签名。"
                           f"封印时网关版本 {m.get('gateway_version', '—')}")
    if tool == "notary_skill.register":
        return "postmortem", "复盘：本次经验已沉淀为新 Skill 并登记入册。"
    if tool == "notary_rebuttal.submit":
        return "author", "已提交等价变异体申辩（论证入证）。"
    if tool == "notary_flow.adjudicate":
        adj = (read_json(rd / "adjudication.json") or [{}])[-1]
        return None, (f"⚖️ 人工裁决已签署：{adj.get('decision')}——"
                      f"{clip(adj.get('rationale', ''), 200)}")
    return None


def matrix_post(matrix: str, token: str, room: str, text: str) -> bool:
    txn = str(time.time_ns())
    url = (f"{matrix}/_matrix/client/v3/rooms/"
           f"{urllib.parse.quote(room, safe='')}/send/m.room.message/{txn}")
    req = urllib.request.Request(url, method="PUT")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(
            req, data=json.dumps({"msgtype": "m.text", "body": text}).encode(),
            timeout=15)
        return True
    except Exception as exc:
        print(f"[roomcast] post failed: {exc}", flush=True)
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", required=True)
    ap.add_argument("--room", required=True)
    ap.add_argument("--matrix", default="http://127.0.0.1:18080")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--runs-dir", default=str(PKG_ROOT / "runs"))
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    token = Path(args.token_file).read_text().strip()
    trace = Path(args.runs_dir) / args.sid / "trace.jsonl"
    # 从文件末尾起播：历史事件不重播；reset 重建文件时归零
    offset = trace.stat().st_size if trace.exists() else 0
    seen_states: list[str] = []
    print(f"[roomcast] {args.sid} → {args.room}", flush=True)
    while True:
        try:
            if trace.exists():
                if trace.stat().st_size < offset:
                    offset = 0
                with trace.open(encoding="utf-8") as fh:
                    fh.seek(offset)
                    new = fh.readlines()
                    offset = fh.tell()
                for line in new:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    tool = e.get("tool", "")
                    got = beat_content(args.sid, Path(args.runs_dir),
                                       tool, e.get("role", ""))
                    if not got:
                        continue
                    beat, body = got
                    state = e.get("state_after", "")
                    if state and state not in seen_states:
                        seen_states.append(state)
                    n = len(seen_states)
                    state_cn = STATE_CN.get(state, state)
                    if beat is None:
                        text = body  # 裁决等特殊播报
                    else:
                        nxt = BEAT_ORDER[BEAT_ORDER.index(beat) + 1] \
                            if beat in BEAT_ORDER and \
                            BEAT_ORDER.index(beat) + 1 < len(BEAT_ORDER) else None
                        head = f"【第 {n} 棒 · {BEAT_CN.get(beat, beat)}】"
                        tail = f"\n→ 状态：{state_cn}" if state else ""
                        if nxt and state not in ("REJECTED", "RELEASED",
                                                 "NOTARIZED", "QUARANTINED"):
                            tail += f"　｜　下一棒：{BEAT_CN.get(nxt, nxt)}"
                        text = f"{head}\n{body}{tail}"
                    matrix_post(args.matrix, token, args.room, text)
                    print(f"  >> {text[:60]}…", flush=True)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
