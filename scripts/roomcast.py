#!/usr/bin/env python3
"""roomcast: 把公证流水线的真实执行事件直播到 Matrix 房间。

"10-Agent 接力直播间"：轮询 run 的 trace.jsonl（append-only 事实层），
每个推动流水线的事件翻译成一条人话播报发进房间。播报内容全部来自
落盘事实，不是 LLM 叙事——房间里由此存在两条可互相对照的线：
系统播报（本脚本）与 agent 发言（Worker 回执）。

用法：
  python3 scripts/roomcast.py --sid coupon_room_v1 \
      --room '!xxx:matrix-local...' --token-file /tmp/.at_token

只读 trace，绝不写回网关。房间不存在/权限不足会打日志并继续重试。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent

# 棒次播报模板：工具 → (棒次名, 播报语气)
BEAT_LABELS = {
    "notary_sentinel.scan": ("哨兵", "🛡️ 入口检疫完成"),
    "notary_flow.triage": ("分诊", "🧭 分诊结论"),
    "notary_flow.reproduce": ("根因", "🔬 缺陷复现完成"),
    "notary_flow.diagnosis": ("根因", "🧠 根因已定位"),
    "notary_contract.freeze": ("契约", "📜 验收规则已冻结"),
    "notary_author.submit_implementation": ("作者", "✍️ 修复实现已提交"),
    "notary_tester.submit_tests": ("盲测", "🧪 盲测用例已提交（未见实现）"),
    "notary_gate.run_test_gate": ("门禁", "🚦 测试门禁"),
    "notary_gate.run_mutation_gate": ("门禁", "🚦 变异门禁"),
    "notary_gate.run_convention_gate": ("门禁", "🚦 规范门禁"),
    "notary_gate.finalize_mutation": ("门禁", "🚦 变异终裁"),
    "notary_rebuttal.submit": ("作者", "🛡️ 等价变异体申辩"),
    "notary_flow.dispute": ("送审方", "⚖️ 提出异议，升级人工裁决"),
    "notary_flow.adjudicate": ("裁决", "⚖️ 人工裁决已签署"),
    "notary_flow.request_rework": ("负责", "🔁 退回重修"),
    "notary_release.deploy": ("发布", "🚀 已发布上线"),
    "notary_evidence.seal": ("证据", "🔐 证据已封印（哈希链+签名）"),
    "notary_skill.register": ("复盘", "📚 经验沉淀为新 Skill"),
}
STATE_CN = {
    "RECEIVED": "已受理", "SCREENED": "检疫通过", "TRIAGED": "已分诊",
    "DIAGNOSED": "根因已定位", "CONTRACTED": "验收规则已冻结",
    "AUTHORING": "修复编写中", "TESTING": "盲测检验中", "GATING": "门禁检验中",
    "NOTARIZED": "已公证（验收通过）", "RELEASED": "已发布",
    "QUARANTINED": "检疫隔离", "ESCALATED": "等待人工裁决",
    "REJECTED": "未放行（待修复或异议）", "ROLLED_BACK": "已回滚",
}


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
    offset = 0
    last_state = None
    print(f"[roomcast] {args.sid} → {args.room}", flush=True)
    while True:
        try:
            if trace.exists():
                # run 被 reset 时文件会缩短重建：offset 归零重播
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
                    if tool not in BEAT_LABELS:
                        continue
                    beat, label = BEAT_LABELS[tool]
                    role = e.get("role", "")
                    state = e.get("state_after", "")
                    state_cn = STATE_CN.get(state, state)
                    state_part = (f" → 状态：{state_cn}"
                                  if state and state != last_state else "")
                    last_state = state or last_state
                    # 门禁播报带结论：工具→自己的 verdict 文件，
                    # 不看"最新 mtime"（批量播报时下一门禁文件已落盘，
                    # 实测把测试红灯误报成规范绿灯）
                    _GATE_VERDICT = {
                        "notary_gate.run_test_gate": "test_pass.json",
                        "notary_gate.run_mutation_gate": "mutation.json",
                        "notary_gate.finalize_mutation": "mutation.json",
                        "notary_gate.run_convention_gate": "convention.json",
                    }
                    extra = ""
                    if tool in _GATE_VERDICT:
                        vf = (Path(args.runs_dir) / args.sid
                              / "verdicts" / _GATE_VERDICT[tool])
                        if vf.exists():
                            v = json.loads(vf.read_text(encoding="utf-8"))
                            dec = {"green": "🟢 通过", "red": "🔴 未通过",
                                   "yellow": "🟡 待复核"}.get(
                                       v.get("decision"), v.get("decision"))
                            extra = f"：{dec}"
                    text = (f"📡【{beat}】{label}{extra}"
                            f"（{role}）{state_part}")
                    matrix_post(args.matrix, token, args.room, text)
                    print(f"  >> {text}", flush=True)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
