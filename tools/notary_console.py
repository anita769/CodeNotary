"""CodeNotary Console — realtime front-end for the notary pipeline.

Four views: Live run (state machine / beats / gates / trace / evidence with
client-side sha256 verification), Scenario gallery, Skill library, Audit.
The Console READS the same sealed evidence judges can recompute; the only
write paths are proxied, role-tagged gateway calls (intake / reset /
resolve_human), optionally guarded by --token.

Pure stdlib, zero external assets. 2.5s polling.

Usage:  python3 tools/notary_console.py --port 18091 [--runs runs/]
            [--gateway http://127.0.0.1:18090] [--token SECRET]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PKG_ROOT = Path(__file__).resolve().parent.parent

# LLM 接待（交互层）：帮客户把"一句话"整理成公证表单草稿。
# 只在 /api/assist 使用——裁决层（门禁/状态机/公证书）永远无模型。
# key 经环境变量注入，绝不入包；未配置时办事大厅自动降级为手动确认卡。
LLM_BASE = os.environ.get("CODENOTARY_LLM_BASE", "https://api.deepseek.com")
LLM_KEY = os.environ.get("CODENOTARY_LLM_KEY", "")
LLM_MODEL = os.environ.get("CODENOTARY_LLM_MODEL", "deepseek-chat")

BEATS = [
    ("sentinel", "哨兵", ["notary_sentinel.scan"]),
    ("triage", "分诊", ["notary_flow.triage"]),
    ("rca", "根因", ["notary_flow.diagnosis", "notary_flow.reproduce"]),
    ("contract", "契约", ["notary_contract.freeze"]),
    ("author", "作者", ["notary_author.get_context",
                        "notary_author.submit_implementation"]),
    ("tester", "盲测", ["notary_tester.get_context",
                        "notary_tester.submit_tests"]),
    ("gates", "门禁", ["notary_gate.run_test_gate",
                       "notary_gate.run_mutation_gate",
                       "notary_gate.finalize_mutation",
                       "notary_gate.run_convention_gate"]),
    ("rebuttal", "对抗环", ["notary_rebuttal.submit"]),
    ("release", "发布", ["notary_release.deploy"]),
    ("postmortem", "复盘", ["notary_skill.register"]),
]

STATE_MEANING = {
    "RECEIVED": "已受理，等待入口检疫",
    "SCREENED": "检疫通过，等待分诊",
    "TRIAGED": "分诊受理，等待根因诊断",
    "DIAGNOSED": "根因已定位，等待契约冻结",
    "CONTRACTED": "契约已冻结（sha256），双盲开工",
    "AUTHORING": "作者实现中（盲分区：看不到盲测）",
    "TESTING": "盲测编写中（盲分区：看不到实现）",
    "GATING": "三门禁终审执行中",
    "NOTARIZED": "三门禁全绿，已公证，等待发布",
    "RELEASED": "已发布，证据已封印",
    "QUARANTINED": "检疫发现 critical，已隔离",
    "ESCALATED": "等待人工裁决（批准续跑 / 拒绝终止 / 契约修订）",
    "REJECTED": "已拒绝（可重修：request_rework，预算制）",
    "ROLLED_BACK": "已回滚（发布已撤销，证据保留）",
}


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_summary(run_dir: Path) -> dict:
    trace = run_dir / "trace.jsonl"
    events = []
    if trace.exists():
        for line in trace.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    state = events[-1]["state_after"] if events else "RECEIVED"
    beats = {}
    for key, _name, tools in BEATS:
        hits = [e for e in events if e["tool"] in tools]
        if hits:
            beats[key] = {"done": True, "ts": hits[-1]["ts"],
                          "calls": len(hits)}
    verdicts = {}
    vdir = run_dir / "verdicts"
    if vdir.is_dir():
        for vf in sorted(vdir.glob("*.json")):
            v = read_json(vf)
            if v:
                verdicts[vf.stem] = v
    manifest = read_json(run_dir / "manifest.json") or {}
    checkpoint = read_json(run_dir / "checkpoint.json") or {}
    metrics = read_json(run_dir / "metrics.json")
    role_counts: dict[str, int] = {}
    for e in events:
        r = e.get("role", "unknown")
        role_counts[r] = role_counts.get(r, 0) + 1
    if metrics is None and events:
        states = [e["state_after"] for e in events]
        tools = [e["tool"] for e in events]
        metrics = {
            "tool_calls": len(events),
            "wall_time_s": round(events[-1]["ts"] - events[0]["ts"], 3),
            "exec_time_s": round(
                sum(float(e.get("duration_ms") or 0) for e in events)
                / 1000, 1),
            "adversarial_loop_iterations": sum(
                1 for a, b in zip(states, states[1:])
                if {a, b} == {"AUTHORING", "TESTING"} and a != b),
            "rebuttals_submitted": tools.count("notary_rebuttal.submit"),
            "human_interventions": tools.count("notary_flow.resolve_human"),
            "escalations": states.count("ESCALATED"),
            "skill_match_calls": tools.count("notary_skill.match"),
        }
    security = read_json(run_dir / "evidence" / "security_events.json") or []
    cert = (run_dir / "certificate.md")
    fixture = read_json(PKG_ROOT / "scenarios" / f"{run_dir.name}.json") or {}
    return {
        "run_id": run_dir.name,
        "state": state,
        "state_meaning": STATE_MEANING.get(state, ""),
        "advisory": bool(fixture.get("advisory")),
        "events": len(events),
        "first_ts": events[0]["ts"] if events else None,
        "last_ts": events[-1]["ts"] if events else None,
        "beats": beats,
        "verdicts": verdicts,
        "sealed": bool(manifest),
        "sealed_files": sorted(manifest.keys()),
        "manifest": manifest,
        "metrics": metrics,
        "contract": read_json(run_dir / "contract.json"),
        "history": checkpoint.get("sm", {}).get("history", []),
        "rework_round": checkpoint.get("rework_round", 0),
        "max_rework_rounds": checkpoint.get("max_rework_rounds", 2),
        "security_events": security,
        "calls_by_role": role_counts,
        "has_certificate": cert.exists(),
        "trace_tail": events[-60:],
    }


def list_runs(runs_dir: Path) -> list[dict]:
    out = []
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            if d.is_dir() and (d / "trace.jsonl").exists():
                s = run_summary(d)
                out.append({"run_id": s["run_id"], "state": s["state"],
                            "events": s["events"], "sealed": s["sealed"],
                            "advisory": s["advisory"],
                            "last_ts": s["last_ts"]})
    return out


# 双层词表：内部状态枚举（机器用）→ 窗口人话（界面用）。UI 永不直出枚举。
STATE_LABELS = {
    "RECEIVED": "已受理，排队分诊",
    "SCREENED": "检疫通过",
    "TRIAGED": "已分诊",
    "DIAGNOSED": "根因已定位",
    "CONTRACTED": "验收规则已冻结",
    "AUTHORING": "修复编写中",
    "TESTING": "盲测编写中",
    "GATING": "门禁检验中",
    "NOTARIZED": "已公证（验收通过）",
    "RELEASED": "已发布",
    "QUARANTINED": "检疫隔离中",
    "ESCALATED": "等待人工裁决",
    "REJECTED": "未放行（待修复或异议）",
    "ROLLED_BACK": "已回滚",
}


def board_data(runs_dir: Path) -> dict:
    """任务看板：run 按状态分列。ESCALATED 卡带争议焦点与等待时长
    （红点不是报错，是设计好的暂停）。文案一律窗口人话。"""
    now = time.time()
    cards = []
    for r in list_runs(runs_dir):
        run_dir = runs_dir / r["run_id"]
        dispute = read_json(run_dir / "dispute.json")
        contract = read_json(run_dir / "contract.json")
        adj = read_json(run_dir / "adjudication.json")
        cp = read_json(run_dir / "checkpoint.json") or {}
        green = (cp.get("sm") or {}).get("green_gates") or []
        card = {
            **r,
            "state_label": STATE_LABELS.get(r["state"], r["state"]),
            "gates_progress": (f"门禁已过 {len(green)}/3 项"
                               if r["state"] in ("GATING", "NOTARIZED")
                               else None),
            "contract_version": (contract or {}).get("version"),
            "dispute_focus": (dispute[-1]["focus"] if dispute else None),
            "adjudications": len(adj) if adj else 0,
            "waiting_s": (round(now - r["last_ts"]) if r["state"] == "ESCALATED"
                          else None),
            "title": (read_json(run_dir / "issue.json") or {}).get("title",
                                                                  r["run_id"]),
        }
        cards.append(card)
    # 同工单多版本标注：卡面直读"第几版/共几版、最新版到哪了"，
    # 评委不会在准考证 v1/v2 双卡时误读成重复任务。
    by_title: dict[str, list] = {}
    for c in cards:
        by_title.setdefault(c["title"], []).append(c)
    for group in by_title.values():
        if len(group) < 2:
            continue
        # 版本先后=出生时间（trace 首事件），不是最近动作时间——
        # v1 等待裁决期间 last_ts 仍跳动，会把版本序排反
        for c in group:
            first = None
            tf = runs_dir / c["run_id"] / "trace.jsonl"
            try:
                with tf.open(encoding="utf-8") as fh:
                    first = (json.loads(fh.readline()) or {}).get("ts")
            except (OSError, ValueError):
                pass
            c["_birth_ts"] = first if first is not None else c["last_ts"]
        group.sort(key=lambda c: c["_birth_ts"])
        latest = group[-1]
        for i, c in enumerate(group, 1):
            c["version_index"] = i
            c["version_count"] = len(group)
            c["is_latest"] = c is latest
            c["latest_state_label"] = latest["state_label"]
            c["latest_run_id"] = latest["run_id"]
    columns = {
        "inflight": [c for c in cards if c["state"] in (
            "RECEIVED", "SCREENED", "TRIAGED", "DIAGNOSED", "CONTRACTED",
            "AUTHORING", "TESTING", "GATING", "QUARANTINED")],
        "escalated": [c for c in cards if c["state"] == "ESCALATED"],
        "rejected": [c for c in cards if c["state"] == "REJECTED"],
        "notarized": [c for c in cards if c["state"] == "NOTARIZED"],
        "released": [c for c in cards if c["state"] in ("RELEASED",
                                                       "ROLLED_BACK")],
    }
    return {"columns": columns, "total": len(cards)}


def adjudication_context(run_dir: Path) -> dict:
    """裁决卡的数据底座：争议双方立场 + 客观证据，全部来自落盘事实。"""
    dispute = read_json(run_dir / "dispute.json") or []
    contract = read_json(run_dir / "contract.json") or {}
    verdicts = {}
    vdir = run_dir / "verdicts"
    if vdir.is_dir():
        for vf in sorted(vdir.glob("*.json")):
            v = read_json(vf) or {}
            verdicts[vf.stem] = {"gate": GATE_LABELS.get(vf.stem, vf.stem),
                                 "decision": v.get("decision"),
                                 "summary": humanize_summary(
                                     v.get("summary") or "")}
    candidates = [str(p.relative_to(run_dir))
                  for p in sorted(run_dir.rglob("*.json"))
                  if "work" not in p.parts and p.name != "checkpoint.json"]
    return {
        "dispute": dispute[-1] if dispute else None,
        "contract": {"version": contract.get("version"),
                     "frozen_hash": contract.get("frozen_hash"),
                     "assertions": contract.get("assertions", []),
                     "assumptions": contract.get("assumptions", [])},
        "verdicts": verdicts,
        "evidence_candidates": candidates,
        "options": [
            {"key": "uphold", "label": "维持契约",
             "hint": "规则不变，原判成立；作者按返工工单修复后重审"},
            {"key": "revise", "label": "修订契约",
             "hint": "澄清或修订规则措辞，发契约新版本；代码按新规则重验"},
            {"key": "request_evidence", "label": "要求补充证据",
             "hint": "现有材料不足以裁决，列明所缺材料，流水线继续等待"},
            {"key": "override", "label": "特批放行",
             "hint": "签字画押的例外：必填充分理由，记录标黄"},
        ],
    }


# ---------------------------------------------------------------------------
# 办事大厅（团队版）：四类消息信封 + 会话追问。
# 信封由落盘事实确定性生成（不是 LLM 编的）；追问聊天只读证据、不下结论、
# 永不驱动状态机——聊天起草、卡片生效。
# ---------------------------------------------------------------------------

_HALL_CHAT_SYSTEM = (
    "你是 CodeNotary 代码公证处窗口的工作人员。用户会就某个任务的落盘记录提问。"
    "规则：\n"
    "1. 只使用随消息附带的【落盘事实】回答；事实没有的内容，明确说"
    "“记录里没有，我帮您查了，目前确实没有”，不许编造。\n"
    "2. 你只负责把事实用人话摆好，绝不替人下结论、不做裁决建议。\n"
    "3. 每次回答结尾给出“您可以怎么做”的指引（看证据/等通知/去裁决卡）。\n"
    "4. 口气像窗口工作人员：讲来龙去脉、体谅处境；不是系统日志。")


def hall_facts(run_dir: Path) -> dict:
    """追问聊天的事实底座：只从落盘文件取，取不到就是“没有”。"""
    cp = read_json(run_dir / "checkpoint.json") or {}
    sm = cp.get("sm", {})
    return {
        "run_id": run_dir.name,
        "issue": read_json(run_dir / "issue.json") or {},
        "state": sm.get("state"),
        "history_len": len(sm.get("history") or []),
        "contract": read_json(run_dir / "contract.json"),
        "verdicts": {p.stem: (read_json(p) or {})
                     for p in sorted((run_dir / "verdicts").glob("*.json"))}
                    if (run_dir / "verdicts").is_dir() else {},
        "disputes": read_json(run_dir / "dispute.json") or [],
        "adjudications": read_json(run_dir / "adjudication.json") or [],
        "certificate": (run_dir / "certificate.md").exists(),
        "security_events": read_json(run_dir / "evidence"
                                     / "security_events.json") or [],
    }


def hall_timeline(run_dir: Path) -> list[dict]:
    """四类消息信封：🔴待办 / 🔵进展 / 🟢回执 / 📎证据。
    每条含五要素：发生了什么/意味着什么/你能做什么/证据在哪/下一步。"""
    f = hall_facts(run_dir)
    envs: list[dict] = []
    sid = f["run_id"]
    title = f["issue"].get("title", sid)

    envs.append({"kind": "progress", "ts": None,
                 "title": "已受理，取号成功",
                 "what": f"收到送审「{title}」，登记为 {sid}",
                 "meaning": "您的请求已进入公证流水线，按序办理",
                 "action": "无需操作，进展会主动通知您",
                 "evidence": "issue.json",
                 "next": "分诊与检验自动进行"})

    for gate, v in f["verdicts"].items():
        decision = v.get("decision")
        if decision == "red":
            envs.append({
                "kind": "todo", "ts": v.get("timestamp"),
                "title": "需要您处理：检验未通过",
                "what": f"{GATE_LABELS.get(gate, gate)}判定不通过：{humanize_summary(v.get('summary', ''))}",
                "meaning": "按当前验收规则，这次改动暂不可放行。机器修实现、"
                          "人裁决意图——系统可在隔离环境自主试修候选补丁，"
                          "是否写回您的仓库由您授权",
                "action": "您可以：①按修复指引改完重新送审；②若认为规则本身"
                          "缺乏依据，提出异议（/notary dispute）",
                "evidence": f"verdicts/{gate.lower()}.json",
                "next": "等您修复或异议；不处理任务就停在这里"})
        elif decision:
            envs.append({
                "kind": "progress", "ts": v.get("timestamp"),
                "title": f"门禁 {GATE_LABELS.get(gate, gate)}：{decision}",
                "what": humanize_summary(v.get("summary", "")),
                "meaning": "检验在逐项推进",
                "action": "无需操作",
                "evidence": f"verdicts/{gate.lower()}.json",
                "next": "其余门禁继续"})

    for d in f["disputes"]:
        envs.append({
            "kind": "receipt", "ts": d.get("ts"),
            "title": "异议已受理（这通道就是为这件事存在的）",
            "what": f"您提出的异议：{d.get('focus', '')}",
            "meaning": "争议对象是契约条款的立项依据，系统不会自问自答；"
                       "已升级人工裁决，任务保持阻塞，无人可以绕过",
            "action": "您不需要做任何等待操作，结果会通知您"
                      "（含结论、理由、对您的要求）",
            "evidence": "dispute.json",
            "next": "裁决人将在裁决工作区处理"})

    for a in f["adjudications"]:
        LABEL = {"uphold": "维持契约", "revise": "修订契约",
                 "request_evidence": "要求补充证据", "override": "特批放行"}
        envs.append({
            "kind": "receipt", "ts": a.get("ts"),
            "title": f"裁决已生效：{LABEL.get(a['decision'], a['decision'])}",
            "what": f"裁决人 {a.get('actor')} 经 {a.get('channel')} 通道裁定："
                    f"{a.get('rationale', '')}",
            "meaning": "裁决对象是规则不是代码；代码由系统按规则重新验证",
            "action": "查看裁决记录与引用材料",
            "evidence": f"adjudication.json（#{a.get('id')}，"
                        f"引用证据 {len(a.get('evidence_reviewed', []))} 项，"
                        f"补证附件 {len(a.get('references', []))} 条）",
            "next": "按裁决结果继续流水线"})

    if f["certificate"] and f["state"] in ("NOTARIZED", "RELEASED",
                                           "ROLLED_BACK"):
        envs.append({
            "kind": "evidence", "ts": None,
            "title": "公证书已出具",
            "what": "三道确定性门禁全部通过，证书绑定代码与契约双版本",
            "meaning": "这是验收结论，不是发布动作——合并归负责人、"
                       "发布归原有流程",
            "action": "可下载证据包，用 make verify 自行复算",
            "evidence": "certificate.md / manifest.json（逐文件 SHA-256）",
            "next": "负责人在合并确认卡查看就绪意见"})

    if f["state"] == "ESCALATED":
        envs.append({
            "kind": "progress", "ts": None,
            "title": "等待人工裁决中",
            "what": "任务已升级，系统按设计暂停",
            "meaning": "这不是报错，是公证处把只能人答的问题交给人",
            "action": "您可以在下方追问任何证据细节",
            "evidence": "—",
            "next": "裁决落地后自动继续"})

    order = {"todo": 0, "receipt": 1, "progress": 2, "evidence": 3}
    envs.sort(key=lambda e: order.get(e["kind"], 9))
    return envs


def hall_chat_reply(run_dir: Path, history: list, message: str) -> dict:
    """会话追问：LLM 只把落盘事实摆成人话；无 LLM 时用确定性模板兜底。
    两种路径都不产生任何状态变更。"""
    facts = hall_facts(run_dir)
    facts_text = json.dumps(facts, ensure_ascii=False, default=str)[:6000]
    if LLM_KEY:
        msgs = [{"role": "system", "content": _HALL_CHAT_SYSTEM},
                {"role": "user", "content":
                 f"【落盘事实】\n{facts_text}"}]
        msgs.extend(history[-6:])
        msgs.append({"role": "user", "content": message})
        req = urllib.request.Request(
            f"{LLM_BASE}/chat/completions",
            data=json.dumps({"model": LLM_MODEL, "messages": msgs,
                             "max_tokens": 800, "temperature": 0.2}
                            ).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {LLM_KEY}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "llm": True,
                    "reply": data["choices"][0]["message"]["content"]}
        except Exception:
            pass  # fall through to the deterministic window answer
    v = facts["verdicts"]
    lines = [f"我帮您查了落盘记录（{facts['run_id']}）："]
    lines.append(f"· 当前环节：{facts['state']}")
    if facts["contract"]:
        lines.append(f"· 验收规则：v{facts['contract'].get('version')} 版契约，"
                     f"哈希 {facts['contract'].get('frozen_hash', '')[:16]}…")
    for gate, verdict in v.items():
        lines.append(f"· 门禁 {gate}：{verdict.get('decision')} — "
                     f"{verdict.get('summary', '')}")
    if facts["adjudications"]:
        a = facts["adjudications"][-1]
        lines.append(f"· 最近裁决：{a.get('decision')}，裁决人 {a.get('actor')}，"
                     f"理由「{a.get('rationale', '')[:60]}」")
    lines.append("您可以：点上面的信封看证据原文，或继续问我具体某一条。")
    return {"ok": True, "llm": False, "reply": "\n".join(lines)}



GATE_LABELS = {"TEST_PASS": "测试门禁", "MUTATION": "变异测试",
               "CONVENTION": "规范检查", "test_pass": "测试门禁",
               "mutation": "变异测试", "convention": "规范检查"}


def humanize_summary(text: str) -> str:
    """Verdict summaries from the gateway are English machine strings;
    the UI speaks 窗口人话. Data stays English on disk, tone stays human
    on screen (双层词表的第二层)."""
    import re as _re
    m = _re.match(r"baseline\+blind tests: (\d+) run, (.+)", text or "")
    if m:
        n, outcome = m.groups()
        return (f"共执行 {n} 个检验用例，"
                + ("全部通过" if "all passed" in outcome else "存在未通过项"))
    m = _re.match(r"mutation score ([\d.]+) \((\d+) killed, (\d+) survived\)",
                  text or "")
    if m:
        score, killed, survived = m.groups()
        return (f"变异测试得分 {score}：{killed} 个变异体被杀死，"
                f"{survived} 个幸存")
    m = _re.match(r"(\d+) findings? \((\d+) veto-class\)", text or "")
    if m:
        n, veto = m.groups()
        return ("未发现不规范" if n == "0"
                else f"发现 {n} 处不规范（其中 {veto} 处为否决级）")
    return text or ""


_TRIAGE_LABELS = {"accept": "受理", "reject": "不受理",
                     "escalate": "升级人工"}


ROLE_LABELS = {"sentinel": "检疫", "triage": "分诊", "rca": "根因分析",
               "contract": "契约", "author": "修复", "tester": "盲测",
               "gatekeeper": "门禁", "convention": "规范", "release": "发布",
               "postmortem": "复盘", "leader": "负责人", "human": "人工",
               "adjudicator": "裁决人"}


def runview_data(run_dir: Path, runs_root: Path) -> dict:
    """Run 时间线：角色泳道卡 + 三段状态条 + 版本链。
    主层业务摘要；技术细节进各卡 detail 字段（二级展开）。"""
    facts = hall_facts(run_dir)
    cp = read_json(run_dir / "checkpoint.json") or {}
    sm = cp.get("sm", {})
    # 展示态一律以 trace 末事件为准：老 run 无 checkpoint 或 checkpoint
    # 陈旧（qb_inhouse_fix 实证停在 RECEIVED）；live run 的 trace 与
    # checkpoint 逐事件同步，二者本就一致
    seen: list[str] = []
    tf = run_dir / "trace.jsonl"
    if tf.exists():
        with tf.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    st0 = (json.loads(line) or {}).get("state_after")
                except ValueError:
                    continue
                if st0:
                    seen.append(st0)
    if seen:
        sm = {"state": seen[-1],
              "history": [{"state": s} for s in dict.fromkeys(seen)]}
        facts["state"] = sm["state"]
    sid = run_dir.name
    triage = read_json(run_dir / "triage.json") or {}
    diagnosis = read_json(run_dir / "diagnosis.json") or {}
    contract = facts["contract"] or {}
    dispute = facts["disputes"]
    adjudications = facts["adjudications"]
    verdicts = facts["verdicts"]
    tests = (cp.get("tests") or {})
    resume_log = read_json(run_dir / "resume_log.json") or []

    cards = []
    if triage:
        cards.append({
            "role": "分诊", "title": "任务拆解与路由",
            "lines": ["受理结论：" + _TRIAGE_LABELS.get(
                      triage.get("verdict"), "—"),
                      f"检验范围：{'、'.join(triage.get('scope', [])) or '—'}",
                      f"处理路由：{' → '.join(triage.get('route', [])) or '—'}",
                      f"分诊理由：{triage.get('rationale', '—')}"],
            "detail": triage,
            "status": "完成", "next": "根因分析"})
    if diagnosis:
        cards.append({
            "role": "根因分析", "title": "根因定位",
            "lines": [f"根因：{diagnosis.get('root_cause', '—')}",
                      f"修复假设：{diagnosis.get('fix_hypothesis', '—')}",
                      f"置信度：{diagnosis.get('confidence', '—')}"],
            "detail": diagnosis,
            "status": "完成", "next": "冻结验收规则"})
    if contract:
        assump = [f"歧义：{a['point']} → 默认：{a['assumption']}（依据：{a['basis']}）"
                  for a in contract.get("assumptions", [])]
        cards.append({
            "role": "契约", "title": f"验收规则 v{contract.get('version', '—')}",
            "lines": [f"断言 {i+1}：{a}"
                      for i, a in enumerate(contract.get("assertions", []))]
                     + (["—— 已标注假设 ——"] + assump if assump else []),
            "detail": {"frozen_hash": contract.get("frozen_hash"),
                       "previous_hash": contract.get("previous_hash"),
                       "references": contract.get("references"),
                       "in_scope": contract.get("in_scope"),
                       "out_of_scope": contract.get("out_of_scope")},
            "status": "已冻结", "next": "盲测独立验证"})
    if tests:
        cards.append({
            "role": "盲测", "title": "独立测试设计（未见实现代码）",
            "lines": [f"生成测试文件：{'、'.join(tests)}",
                      "验证目标：仅依据冻结契约编写用例，覆盖规则要求的边界场景"],
            "detail": {name: content for name, content in tests.items()},
            "status": "完成", "next": "确定性门禁检验"})
    for gate, v in verdicts.items():
        cards.append({
            "role": "门禁", "title": GATE_LABELS.get(gate, gate),
            "lines": [humanize_summary(v.get("summary", ""))],
            "detail": v,
            "status": {"green": "通过", "red": "未通过",
                       "yellow": "待复核"}.get(v.get("decision"),
                                              v.get("decision", "—")),
            "tone": v.get("decision"),
            "next": "下一项检验" if v.get("decision") == "green"
                    else "隔离试修 / 送审方修复 / 提出异议"})
    for d in dispute:
        cards.append({
            "role": "争议", "title": "送审方异议（正式通道）", "tone": "hitl",
            "lines": [f"争议焦点：{d.get('focus', '—')}",
                      f"涉及条款：{d.get('clause') or '—'}",
                      "争议对象：契约条款的立项依据（非检验过程）"],
            "detail": d,
            "status": "已升级", "next": "等待人工裁决"})
    for a in adjudications:
        LABEL = {"uphold": "维持契约", "revise": "修订契约",
                 "request_evidence": "要求补充证据", "override": "特批放行"}
        cards.append({
            "role": "人工裁决", "title": f"#{a.get('id')} "
                    f"{LABEL.get(a['decision'], a['decision'])}", "tone": "hitl",
            "lines": [f"裁决理由：{a.get('rationale', '—')}",
                      f"签署：{a.get('actor')}（{a.get('channel')}）",
                      f"引用证据 {len(a.get('evidence_reviewed', []))} 项 ｜ "
                      f"补证附件 {len(a.get('references', []))} 条"]
                     + [f"补证：{r}" for r in a.get("references", [])],
            "detail": a,
            "status": "已签署留痕",
            "next": "按裁决结果继续流水线"})
    state = facts["state"]
    if facts["certificate"] and state in ("NOTARIZED", "RELEASED",
                                          "ROLLED_BACK"):
        cards.append({
            "role": "验收", "title": "公证证书",
            "lines": ["三道确定性门禁全部通过，证书绑定代码与契约双版本",
                      "复算：make verify EVIDENCE=<证据包>"],
            "detail": {"certificate": "certificate.md",
                       "manifest": "manifest.json",
                       "signature": "evidence/manifest.sig.json"},
            "status": "已签发" if state in ("NOTARIZED", "RELEASED") else "已封存",
            "next": "负责人合并评估"})

    # 三段状态条：验收 → 合并 → 发布
    stages = {
        "accept": {"done": state in ("NOTARIZED", "RELEASED", "ROLLED_BACK"),
                   "label": "验收",
                   "state": ("通过" if state in ("NOTARIZED", "RELEASED",
                                                 "ROLLED_BACK")
                             else "未通过" if state == "REJECTED"
                             else "待人工裁决" if state == "ESCALATED"
                             else "进行中")},
        "merge": {"done": False, "label": "合并",
                  "state": "未开始" if state not in ("RELEASED", "ROLLED_BACK")
                          else "已执行"},
        "release": {"done": state == "RELEASED", "label": "发布",
                    "state": "已发布" if state == "RELEASED"
                            else "已回滚" if state == "ROLLED_BACK"
                            else "未开始（由 CD 流程执行）"},
    }

    # 版本链：同 issue 的多个 run
    issue_title = (facts["issue"] or {}).get("title")
    chain = []
    if issue_title and runs_root.is_dir():
        siblings = []
        for d in runs_root.iterdir():
            if not d.is_dir():
                continue
            iss = read_json(d / "issue.json") or {}
            if iss.get("title") == issue_title:
                c = read_json(d / "contract.json") or {}
                adj = read_json(d / "adjudication.json") or []
                s = run_summary(d)
                st = s.get("state")
                siblings.append({
                    "run_id": d.name, "current": d.name == sid,
                    "state": st,
                    "state_label": STATE_LABELS.get(st, st or "未知"),
                    "contract_version": c.get("version"),
                    "adjudications": len(adj),
                    "ts": s.get("last_ts", 0)})
        chain = sorted(siblings, key=lambda x: x["ts"])

    binding = read_json(run_dir / "evidence" / "pr_binding.json")

    # ---- stepper（横屏流程条）：节拍 + 条件步（对抗环/人工裁决）+ 验收/合并
    summary = run_summary(run_dir)
    beats = summary["beats"]
    red_gates = {g for g, v in verdicts.items()
                 if v.get("decision") == "red"}
    has_hitl = bool(dispute or adjudications or state == "ESCALATED")
    fixture = read_json(PKG_ROOT / "scenarios" / f"{sid}.json") or {}
    external = fixture.get("mode") == "external"
    steps = []
    for key, name, _tools in BEATS:
        if key in ("release", "postmortem"):
            continue  # 发布/CD 在顶部三段条呈现；复盘非流水线步骤
        if key == "rebuttal" and not beats.get(key, {}).get("done"):
            continue
        done = bool(beats.get(key, {}).get("done"))
        if key == "author" and external:
            name, done = "送审补丁", True  # 外部 PR 即实现，无作者环节
        blocked = key == "gates" and bool(red_gates)
        steps.append({"key": key, "name": name, "done": done,
                      "blocked": blocked,
                      "calls": beats.get(key, {}).get("calls", 0)})
    if has_hitl:
        steps.append({"key": "hitl", "name": "人工裁决", "hitl": True,
                      "done": bool(adjudications),
                      "blocked": state == "ESCALATED"
                      and not adjudications, "calls": len(adjudications)})
    steps.append({"key": "accept", "name": "最终验收",
                  "done": state in ("NOTARIZED", "RELEASED", "ROLLED_BACK"),
                  "blocked": state == "REJECTED", "calls": 0})
    steps.append({"key": "merge", "name": "合并",
                  "done": state in ("RELEASED", "ROLLED_BACK"),
                  "blocked": False, "calls": 0})
    for st in steps:
        if not st["done"]:
            if not st.get("blocked"):
                st["current"] = True
            break

    # ---- 14 状态条
    visited = {h.get("state") for h in sm.get("history", [])}
    sm_strip = [{"state": st2, "done": st2 in visited,
                 "current": st2 == state}
                for st2 in ("RECEIVED", "SCREENED", "TRIAGED", "DIAGNOSED",
                            "CONTRACTED", "AUTHORING", "TESTING", "GATING",
                            "NOTARIZED", "RELEASED", "QUARANTINED",
                            "ESCALATED", "REJECTED", "ROLLED_BACK")
                if st2 in visited or st2 in (
                    "RECEIVED", "SCREENED", "TRIAGED", "DIAGNOSED",
                    "CONTRACTED", "AUTHORING", "TESTING", "GATING",
                    "NOTARIZED", "RELEASED")]

    # ---- Agent 运行（含 skill 调用）
    skill_calls = read_json(run_dir / "evidence" / "skill_matches.json") or []
    agents = []
    artifact_map = {
        "triage": ["triage.json"], "rca": ["diagnosis.json"],
        "contract": ["contract.json"], "tester": ["evidence/blind_test_files.json"],
        "gatekeeper": ["verdicts/"], "adjudicator": ["adjudication.json"],
        "author": ["evidence/implementation_files.json"],
        "release": ["certificate.md", "manifest.json"],
    }
    for role, n in sorted(summary["calls_by_role"].items(),
                          key=lambda kv: -kv[1]):
        if role in ("unknown",):
            continue
        agents.append({
            "role": ROLE_LABELS.get(role, role), "role_key": role,
            "calls": n,
            "artifacts": artifact_map.get(role, []),
            "skill_calls": [m for m in skill_calls
                            if m.get("role") == role]})

    # ---- 验证结果 tab
    verification = []
    for gate, v in verdicts.items():
        out = v.get("test_output", "")
        import re as _re2
        failed_tests = sorted(set(_re2.findall(r"(?:FAIL|ERROR): (\w+)", out)))
        verification.append({
            "gate": GATE_LABELS.get(gate, gate),
            "decision": v.get("decision"),
            "summary": humanize_summary(v.get("summary", "")),
            "failed_tests": failed_tests,
            "detail": v})

    metrics = summary.get("metrics") or {}
    overview = {
        "issue": facts["issue"],
        "metrics": {**metrics,
                    "security_events": len(facts["security_events"]),
                    "skill_match_calls": len(skill_calls)},
        "first_ts": summary.get("first_ts"),
        "last_ts": summary.get("last_ts"),
        "rework": f"{summary.get('rework_round', 0)}/{summary.get('max_rework_rounds', 2)}",
    }

    return {"run_id": sid, "title": facts["issue"].get("title", sid),
            "state": state, "state_label": STATE_LABELS.get(state, state),
            "cards": cards, "stages": stages, "chain": chain,
            "binding": binding, "steps": steps, "sm_strip": sm_strip,
            "agents": agents, "skill_calls": skill_calls,
            "verification": verification, "overview": overview,
            "trace_tail": summary.get("trace_tail", []),
            "security_events": facts["security_events"],
            "resume": resume_log[-1] if resume_log else None}


def skillboard_data() -> dict:
    """Skill 看板：版本链 + 触发信号 + 状态，人话优先。"""
    base = skills_data()
    mt = read_json(PKG_ROOT / "skills" / "match_table.json") or {}
    by_skill: dict[str, list] = {}
    for sig in mt.get("signals", []):
        by_skill.setdefault(sig["skill"], []).append(sig)
    confirmed = {e["name"] for e in base.get("ledger", [])
                 if e.get("action") == "confirm"}
    cards = []
    for sk in base.get("skills", []):
        if "status" not in sk:
            if sk.get("retired"):
                sk["status"] = "retired"
            elif sk.get("source") == "registry"                     and sk["name"] not in confirmed:
                sk["status"] = "probation"
            else:
                sk["status"] = "loaded"
        sigs = by_skill.get(sk["name"], [])
        cards.append({**sk, "signals": [
            {"signal": s["signal"], "trigger": s["trigger"],
             "roles": s.get("roles", []),
             "coverage_n": len(s.get("coverage", []))} for s in sigs]})
    return {"cards": cards, "registry": base.get("registry", []),
            "probation_queue": [c for c in cards
                                if c["status"] == "probation"],
            "ledger": base.get("ledger", []),
            "match_stats": {"note":
                            "逐次命中明细见各 run 的 evidence/skill_matches.json"}}


def gallery_data() -> list[dict]:
    """Scenario gallery: every scenario with its evalset expectation and
    the latest run's actual outcome."""
    manifest = read_json(PKG_ROOT / "evalset" / "manifest.json") or {}
    by_scenario: dict[str, dict] = {}
    for s in manifest.get("samples", []):
        by_scenario.setdefault(s["scenario"], s)
    runs = {r["run_id"]: r for r in list_runs(PKG_ROOT / "runs")}
    cards = []
    for sp in sorted((PKG_ROOT / "scenarios").glob("*.json")):
        fx = read_json(sp)
        if not fx:
            continue
        sid = fx.get("scenario_id", sp.stem)
        sample = by_scenario.get(sid)
        run = runs.get(sid)
        expect_final = None
        if sample:
            ef = sample.get("expect", {}).get("final")
            expect_final = ef if ef and " " not in str(ef) else None
        cards.append({
            "scenario_id": sid,
            "title": fx.get("title", ""),
            "mode": fx.get("mode", ""),
            "source": sample.get("source") if sample else None,
            "sample_id": sample.get("id") if sample else None,
            "prototype": (sample or {}).get("real_world_prototype"),
            "expect_final": expect_final,
            "live_only": bool((sample or {}).get("live_only")),
            "actual_final": run["state"] if run else None,
            "sealed": run["sealed"] if run else False,
        })
    return cards


def skills_data() -> dict:
    """Skill cards + registry ledger, read from files (same sources the
    gateway loader reads)."""
    def meta(text: str) -> dict:
        if not text.startswith("---"):
            return {}
        end = text.find("\n---", 3)
        out = {}
        for line in text[3:end].strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
        return out

    skills = []
    for p in sorted((PKG_ROOT / "skills").glob("*/SKILL.md")):
        m = meta(p.read_text(encoding="utf-8"))
        skills.append({"name": m.get("name", p.parent.name), "source": "seed",
                       "version": m.get("version"), "compat": m.get("compat"),
                       "description": m.get("description", "")})
    reg = PKG_ROOT / "skills" / "registry"
    index = read_json(reg / "index.json") or {"entries": []}
    retired = set()
    for e in index.get("entries", []):
        if e.get("action") == "retire":
            retired.add(e["name"])
    for p in sorted(reg.glob("*.md")):
        m = meta(p.read_text(encoding="utf-8"))
        name = m.get("name", p.stem)
        skills.append({"name": name, "source": "registry",
                       "version": m.get("version"), "compat": m.get("compat"),
                       "retired": name in retired,
                       "description": m.get("description", "")})
    table = read_json(PKG_ROOT / "skills" / "match_table.json") or {}
    return {"skills": skills,
            "signals": table.get("signals", []),
            "ledger": index.get("entries", [])}


def audit_data(runs_dir: Path) -> dict:
    """Role x tool call matrix + all security events, from traces."""
    matrix: dict[str, dict[str, int]] = {}
    events = []
    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir():
            continue
        trace = d / "trace.jsonl"
        if trace.exists():
            for line in trace.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                role = e.get("role", "unknown")
                bucket = matrix.setdefault(role, {})
                bucket[e["tool"]] = bucket.get(e["tool"], 0) + 1
        se = read_json(d / "evidence" / "security_events.json")
        if se:
            events.extend(se)
    alerts = []
    alog = PKG_ROOT / "alerts.log"
    if alog.exists():
        for line in alog.read_text(encoding="utf-8").splitlines()[-50:]:
            try:
                alerts.append(json.loads(line))
            except Exception:
                pass
    return {"matrix": matrix, "security_events": events, "alerts": alerts}


# ---------------------------------------------------------------------------
# Gateway proxy (the only write-capable surface; role-tagged, token-guarded)
# ---------------------------------------------------------------------------

GATEWAY = "http://127.0.0.1:18090"
TOKEN: str | None = None
TOKEN_SECRET: str | None = None
DEMO_REPO: str | None = None  # demo checkout for the merge-readiness card


def gateway_post(sid: str, tool: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{GATEWAY}/tools/{sid}/{tool}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return 502, {"ok": False, "error": f"gateway unreachable: {exc}"}


def gateway_get(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{GATEWAY}{path}", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LLM 接待（/api/assist）：把客户的"一句话 + ZIP"整理成公证表单草稿。
# 交互层用模型，裁决层零模型；多轮对话由客户端携带 history（服务端无会话态）。
# ---------------------------------------------------------------------------

_ASSIST_SYSTEM = (
    "你是 CodeNotary 代码公证处的接待员。用户会描述一个代码问题，"
    "并可能附带代码文件清单与片段。你的任务是把对话整理成一张公证申请表。"
    "只输出 JSON，字段：\n"
    '  "title": 一句话名称（8 字以上，口语）\n'
    '  "report": 问题描述（20 字以上：现象、背景、影响）\n'
    '  "expected_behavior": 验收标准（3-5 条，每条一行，以"1) "开头编号，'
    "必须是可以检查的判断句；最后一条通常是改动范围限定）\n"
    '  "mode": "fix"（用户要我们修）或 "review"（用户带了改好的代码要我们审）\n'
    "如果信息不足，把能确定的字段写好，不确定的字段留空字符串，"
    '并在 "questions" 字段给一个简短追问。\n'
    "不要输出 JSON 以外的任何文字。")


def llm_assist(history: list[dict], files_map: dict) -> dict | None:
    """Call the interaction-layer LLM to draft intake fields.
    Returns parsed draft dict, or None when LLM is unavailable/fails."""
    if not LLM_KEY:
        return None
    listing = []
    budget = 4000
    for name in sorted(files_map):
        if budget <= 0:
            break
        head = files_map[name][:600]
        budget -= len(head)
        listing.append(f"--- {name}\n{head}")
    msgs = [{"role": "system", "content": _ASSIST_SYSTEM}]
    msgs.extend(history[-8:])
    if listing:
        msgs.append({"role": "user", "content":
                     "附带的代码文件：\n" + "\n".join(listing)})
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        data=json.dumps({
            "model": LLM_MODEL, "messages": msgs,
            "response_format": {"type": "json_object"},
            "max_tokens": 1200, "temperature": 0.2,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LLM_KEY}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        draft = json.loads(content)
        return {k: str(draft.get(k, "")).strip()
                for k in ("title", "report", "expected_behavior", "mode",
                          "questions")}
    except Exception:
        return None


def unzip_py_files(zip_b64: str) -> dict:
    """Unzip an uploaded package: .py files only, split into sources/tests.

    Returns {"source_files": {...}, "test_files": {...}, "skipped": [...]}.
    """
    raw = base64.b64decode(zip_b64)
    out: dict = {"source_files": {}, "test_files": {}, "skipped": []}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if name.startswith(".") or "__pycache__" in info.filename:
                continue
            if not name.endswith(".py"):
                out["skipped"].append(info.filename)
                continue
            content = zf.read(info).decode("utf-8", errors="replace")
            if name.startswith("test_"):
                out["test_files"][name] = content
            else:
                out["source_files"][name] = content
    return out


PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodeNotary Console</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;--purple:#bc8cff;--cyan:#39c5cf}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--bg);z-index:20;flex-wrap:wrap}
h1{font-size:17px;display:flex;align-items:center;gap:10px}
.badge{font-size:11px;color:var(--dim);font-weight:400}
.pill{font-size:11px;border:1px solid var(--line);border-radius:99px;padding:3px 10px;color:var(--dim)}
.pill.on{color:var(--green);border-color:var(--green)}
nav{display:flex;gap:4px}
nav button{background:none;border:1px solid transparent;color:var(--dim);padding:5px 14px;border-radius:8px;
cursor:pointer;font-size:13px}
nav button.on{background:var(--panel2);color:var(--fg);border-color:var(--line)}
.btn{background:var(--blue);color:#06101f;border:none;border-radius:8px;padding:7px 16px;font-weight:700;
cursor:pointer;font-size:13px}
.btn.ghost{background:none;border:1px solid var(--line);color:var(--fg);font-weight:400}
.btn.danger{background:none;border:1px solid var(--red);color:var(--red)}
.btn:disabled{opacity:.4;cursor:not-allowed}
main{padding:14px 16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;transition:border-color .2s}
.panel:hover{border-color:#3d444d}
.panel h2{font-size:12px;color:var(--dim);margin-bottom:10px;letter-spacing:.8px;text-transform:uppercase}
.wide{grid-column:1/3}
/* live layout */
.live{display:grid;grid-template-columns:250px 1fr;gap:12px}
.runlist{display:flex;flex-direction:column;gap:4px;max-height:calc(100vh - 120px);overflow:auto}
.ritem{border:1px solid var(--line);border-radius:8px;padding:7px 10px;cursor:pointer;font-size:12px;
display:flex;align-items:center;gap:8px}
.ritem:hover{background:var(--panel2)}
.ritem.sel{border-color:var(--blue);background:var(--panel2)}
.ritem .name{font-family:ui-monospace,monospace;font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.dot.RELEASED{background:var(--green)}.dot.REJECTED,.dot.QUARANTINED{background:var(--red)}
.dot.ESCALATED{background:var(--amber)}.dot.ROLLED_BACK{background:var(--purple)}
.dot.other{background:var(--blue);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.35}}
/* banner */
.banner{display:flex;align-items:center;gap:14px;border:1px solid var(--line);border-radius:10px;
padding:12px 16px;margin-bottom:12px;background:var(--panel)}
.banner .st{font-size:20px;font-weight:800;font-family:ui-monospace,monospace}
.banner .mean{color:var(--dim);font-size:12px;flex:1}
.banner.esc{border-color:var(--amber);background:#d2992211;animation:pulse 2s infinite}
.banner.esc .st{color:var(--amber)}
.banner.green .st{color:var(--green)}.banner.red .st{color:var(--red)}.banner .st{color:var(--blue)}
/* state machine */
.sm{display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.st2{border:1px solid var(--line);border-radius:6px;padding:4px 9px;font-size:11px;color:var(--dim);
font-family:ui-monospace,monospace}
.st2.cur{background:var(--blue);color:#06101f;font-weight:700;border-color:var(--blue);
box-shadow:0 0 10px #58a6ff66}
.st2.done{color:var(--green);border-color:var(--green)}
.st2.side-cur.bad{background:var(--red);color:#fff;border-color:var(--red)}
.st2.side-cur.warn{background:var(--amber);color:#06101f;border-color:var(--amber)}
.arrow{color:var(--dim)}
.smrow{margin-top:8px;font-size:11px;color:var(--dim)}
/* beats */
.beats{display:flex;gap:5px;flex-wrap:wrap}
.beat{border:1px solid var(--line);border-radius:8px;padding:7px 10px;text-align:center;min-width:70px}
.beat .n{font-size:13px}
.beat .s{font-size:10px;color:var(--dim)}
.beat.done{border-color:var(--green)}.beat.done .s{color:var(--green)}
.beat.cur{border-color:var(--blue);box-shadow:0 0 8px #58a6ff44;animation:pulse 1.6s infinite}
/* gates */
.gates{display:flex;gap:10px}
.gate{flex:1;border:1px solid var(--line);border-radius:8px;padding:12px;cursor:pointer}
.gate:hover{background:var(--panel2)}
.gate .t{font-size:12px;color:var(--dim)}
.gate .d{font-size:19px;font-weight:800;margin-top:3px}
.gate.green{border-color:var(--green)}.gate.green .d{color:var(--green)}
.gate.red{border-color:var(--red)}.gate.red .d{color:var(--red)}
.gate.yellow{border-color:var(--amber)}.gate.yellow .d{color:var(--amber)}
.gate.none .d{color:var(--dim)}
.gate .sm2{font-size:11px;color:var(--dim);margin-top:5px}
pre{background:#0a0e14;border:1px solid var(--line);border-radius:8px;padding:10px;
font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;overflow:auto;max-height:280px;white-space:pre-wrap}
/* kv */
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.kv .k{background:#0a0e14;border:1px solid var(--line);border-radius:8px;padding:9px;text-align:center}
.kv .v{font-size:17px;font-weight:700;font-variant-numeric:tabular-nums}
.kv .l{font-size:11px;color:var(--dim)}
/* role chips */
.chip{display:inline-block;border-radius:99px;padding:0 8px;font-size:10px;font-weight:700;
border:1px solid var(--line)}
/* trace */
table.trace{width:100%;border-collapse:collapse;font:11px/1.6 ui-monospace,monospace}
table.trace td{padding:2px 8px;border-bottom:1px solid #21262d;white-space:nowrap}
table.trace tr.warnrow td{color:var(--red)}
.scroll{max-height:280px;overflow:auto;border:1px solid var(--line);border-radius:8px;background:#0a0e14}
/* evidence */
.ev{display:grid;grid-template-columns:250px 1fr;gap:10px}
.evlist{border:1px solid var(--line);border-radius:8px;max-height:280px;overflow:auto}
.evitem{padding:5px 9px;font:11px ui-monospace,monospace;color:var(--dim);cursor:pointer;
border-bottom:1px solid #21262d;display:flex;justify-content:space-between;gap:6px}
.evitem:hover,.evitem.sel{color:var(--fg);background:var(--panel2)}
.evitem .ok{color:var(--green)}
.evitem .bad2{color:var(--red)}
/* gallery */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer}
.card:hover{border-color:var(--blue)}
.card .t{font-size:13px;font-weight:600;margin:6px 0}
.card .id{font:10px ui-monospace,monospace;color:var(--dim)}
.src{display:inline-block;font-size:10px;font-weight:800;border-radius:4px;padding:1px 6px}
.src.D1{background:#58a6ff22;color:var(--blue)}.src.D1-R{background:#39c5cf22;color:var(--cyan)}
.src.D1W{background:#bc8cff22;color:var(--purple)}.src.D3{background:#f8514922;color:var(--red)}
.src.D5{background:#3fb95022;color:var(--green)}.src.D1X{background:#d2992222;color:var(--amber)}
.matchline{font-size:12px;margin-top:8px}
.ok2{color:var(--green)}.no2{color:var(--red)}.dim2{color:var(--dim)}
a{color:var(--blue);text-decoration:none}
/* skill */
.skillgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.skill{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
.skill.ret{opacity:.55;border-style:dashed}
.skill .nm{font:13px ui-monospace,monospace;font-weight:700}
.skill .ds{font-size:12px;color:var(--dim);margin-top:6px;line-height:1.5}
.tag{font-size:10px;border-radius:4px;padding:1px 6px;font-weight:700}
.tag.seed{background:#58a6ff22;color:var(--blue)}.tag.reg{background:#3fb95022;color:var(--green)}
.tag.ret{background:#f8514922;color:var(--red)}
.ledger{margin-top:12px}
/* audit */
.matrix{border-collapse:collapse;font:11px ui-monospace,monospace}
.matrix th,.matrix td{border:1px solid var(--line);padding:4px 8px;text-align:right}
.matrix th{color:var(--dim);font-weight:400}
.matrix td.hot{color:var(--green);font-weight:700}
/* modal */
.overlay{position:fixed;inset:0;background:#000000aa;z-index:50;display:flex;align-items:center;
justify-content:center}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;width:640px;
max-width:94vw;max-height:90vh;overflow:auto}
.modal h3{font-size:15px;margin-bottom:12px}
.modal label{display:block;font-size:12px;color:var(--dim);margin:10px 0 4px}
.modal input,.modal textarea,.modal select{width:100%;background:#0a0e14;border:1px solid var(--line);
border-radius:8px;color:var(--fg);padding:8px;font-size:13px;font-family:inherit}
.modal textarea{min-height:70px;resize:vertical;font-family:ui-monospace,monospace;font-size:12px}
.modal .row{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
.hint{font-size:11px;color:var(--dim);margin-top:2px}
.resultbox{background:#0a0e14;border:1px solid var(--green);border-radius:8px;padding:12px;margin-top:12px;
font-size:12px}
footer{padding:16px;color:var(--dim);font-size:11px;text-align:center}
.summarybar{display:flex;gap:16px;align-items:center;margin-bottom:14px;font-size:13px}
.summarybar .big{font-size:22px;font-weight:800}
@media(max-width:1100px){.grid{grid-template-columns:1fr}.wide{grid-column:1/2}.live{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1>⚖️ CodeNotary Console <span class="badge">可信交付前台 · 证据同源可复算</span></h1>
  <nav>
    <button id="tab-live" class="on" onclick="showView('live')">实时运行</button>
    <button id="tab-gallery" onclick="showView('gallery')">场景画廊</button>
    <button id="tab-skills" onclick="showView('skills')">Skill 库</button>
    <button id="tab-audit" onclick="showView('audit')">权限审计</button>
  </nav>
  <span style="flex:1"></span>
  <span class="pill" id="gw">网关…</span>
  <a class="btn ghost" href="/desk" style="text-decoration:none">🏛 办事大厅</a>
  <button class="btn ghost" id="resetbtn" onclick="doReset()">↺ 重跑此 run</button>
  <button class="btn" onclick="openIntake()">＋ 发起公证</button>
  <span class="pill" id="clock"></span>
</header>

<main>
<!-- ================= LIVE ================= -->
<div id="view-live">
  <div class="live">
    <div class="runlist" id="runlist"></div>
    <div>
      <div class="banner" id="banner"></div>
      <div class="grid">
        <div class="panel wide"><h2>流水线状态机 · 14 状态非链式可回退（回退边 L1–L5 见 18.5）</h2>
          <div class="sm" id="sm"></div>
          <div class="smrow">主链 RECEIVED→…→RELEASED ｜ L1 AUTHORING↔TESTING 对抗环 ｜ L2 GATING→ESCALATED→GATING ｜ L3 ESCALATED→CONTRACTED 契约修订 ｜ L4 REJECTED→AUTHORING 驳回重修（预算制）｜ L5 RELEASED→ROLLED_BACK</div>
        </div>
        <div class="panel wide"><h2>十棒接力</h2><div class="beats" id="beats"></div></div>
        <div class="panel"><h2>三门禁裁决（点击展开 findings）</h2><div class="gates" id="gates"></div></div>
        <div class="panel"><h2>运行指标</h2><div class="kv" id="metrics"></div></div>
        <div class="panel"><h2 style="color:var(--purple)">技能调用记录（本 run）</h2><div class="scroll" id="skillcalls" style="max-height:180px"></div></div>
        <div class="panel wide"><h2 id="gatedetail-title">门禁详情</h2><pre id="gatedetail">点击门禁卡查看 verdict 原文</pre></div>
        <div class="panel wide"><h2>Trace 流（时间 · 角色 · 工具 · 状态 · 哈希链）</h2>
          <div class="scroll"><table class="trace" id="trace"></table></div></div>
        <div class="panel wide"><h2>证据浏览器 · 浏览器端实时验印</h2>
          <div class="ev"><div class="evlist" id="evlist"></div>
          <div><pre id="evview" style="max-height:230px">选择左侧文件查看内容</pre>
          <div id="verify" class="matchline"></div></div></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ================= GALLERY ================= -->
<div id="view-gallery" style="display:none">
  <div class="summarybar" id="gsummary"></div>
  <div class="cards" id="gcards"></div>
</div>

<!-- ================= SKILLS ================= -->
<div id="view-skills" style="display:none">
  <div class="grid">
    <div class="panel wide"><h2>触发信号试验台（决策表运行时实证）</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="sigsel" style="flex:1;background:#0a0e14;border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px"></select>
        <button class="btn ghost" onclick="tryMatch()">调 notary_skill.match →</button>
      </div>
      <div id="matchout" class="matchline"></div>
    </div>
  </div>
  <div class="skillgrid" id="skillgrid" style="margin-top:12px"></div>
  <div class="panel ledger"><h2>registry 台账（追加式完整性账本）</h2>
    <div class="scroll"><table class="trace" id="ledger"></table></div></div>
</div>

<!-- ================= AUDIT ================= -->
<div id="view-audit" style="display:none">
  <div class="summarybar" id="asummary"></div>
  <div class="grid">
    <div class="panel wide"><h2>角色 × 端点调用矩阵（全部 run 聚合）</h2>
      <div class="scroll" style="max-height:340px"><div id="matrix"></div></div></div>
    <div class="panel"><h2>双层白名单（ROLE_POLICY 谁可调 × ENTRY_STATES 何时可调）</h2>
      <div class="scroll" style="max-height:280px"><pre id="policy" style="border:none"></pre></div></div>
    <div class="panel"><h2>安全事件（越权拒绝留痕）</h2>
      <div class="scroll" style="max-height:280px"><table class="trace" id="sec"></table></div></div>
    <div class="panel wide"><h2>运行告警（watchdog alerts.log）</h2>
      <div class="scroll" style="max-height:200px"><table class="trace" id="alerts"></table></div></div>
  </div>
</div>
</main>

<footer>CodeNotary Console · 只读证据 + 三条受审写通道（intake/reset/resolve_human，均带角色与 trace）· 2.5s 轮询 · 与 eval_replay 复算同源</footer>

<!-- intake modal -->
<div class="overlay" id="intakemodal" style="display:none">
  <div class="modal">
    <h3>＋ 发起公证（经 notary_intake.submit_issue，角色 ci，全程入 trace）</h3>
    <div id="intakeform">
      <label>目标代码库（注册靶场）</label>
      <select id="f-target"><option>queue_box</option><option>dispatcher</option><option>mailbox_router</option></select>
      <label>标题（≥8 字）</label><input id="f-title" placeholder="例：pop 空队列错误处理修复">
      <label>问题报告（≥20 字）</label><textarea id="f-report" placeholder="现象、影响、复现路径…"></textarea>
      <label>期望行为（≥20 字，将写入 issue 供契约冻结）</label><textarea id="f-expect" placeholder="空队列 pop 应抛出带业务语义的 IndexError…"></textarea>
      <label>补丁上传（可选——粘贴 AI 生成的修复代码，建档为 external 送审场景）</label>
      <textarea id="f-files" placeholder='{"queue_box.py": "class Mailbox:\n    ..."}'></textarea>
      <div class="hint">留空则为 inhouse（流水线自研修复）；填写则为 external（外部变更送审）。内容哈希幂等：重复提交不会重复建档。</div>
      <div class="row">
        <button class="btn ghost" onclick="closeIntake()">取消</button>
        <button class="btn" onclick="submitIntake()">提交建档</button>
      </div>
    </div>
    <div id="intakeresult" style="display:none"></div>
  </div>
</div>

<script>
const SM_MAIN=["RECEIVED","SCREENED","TRIAGED","DIAGNOSED","CONTRACTED","AUTHORING","TESTING","GATING","NOTARIZED","RELEASED"];
const SM_SIDE=[["QUARANTINED","bad"],["ESCALATED","warn"],["REJECTED","bad"],["ROLLED_BACK","warn"]];
const BEATS=[["sentinel","哨兵"],["triage","分诊"],["rca","根因"],["contract","契约"],["author","作者"],["tester","盲测"],["gates","门禁"],["rebuttal","对抗环"],["release","发布"],["postmortem","复盘"]];
const GATES=[["test_pass","测试门禁"],["mutation","变异门禁"],["convention","惯例门禁"]];
const ROLE_COLOR={sentinel:"#39c5cf",triage:"#58a6ff",rca:"#bc8cff",contract:"#d29922",author:"#3fb950",
tester:"#3fb950cc",gatekeeper:"#f85149",release:"#58a6ff",postmortem:"#bc8cff",leader:"#d29922",
human:"#e6edf3",ci:"#39c5cf",unknown:"#8b949e"};
let cur=null, runs=[], selFile=null, view='live';

async function j(u,opt){const r=await fetch(u,opt);return r.json();}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function ago(ts){if(!ts)return'';const s=(Date.now()/1000-ts)|0;
  return s<60?s+'秒前':s<3600?((s/60)|0)+'分钟前':((s/3600)|0)+'小时前';}

function showView(v){view=v;
  for(const t of ['live','gallery','skills','audit']){
    document.getElementById('view-'+t).style.display=t===v?'':'none';
    document.getElementById('tab-'+t).className=t===v?'on':'';}
  if(v==='gallery')loadGallery(); if(v==='skills')loadSkills(); if(v==='audit')loadAudit();}

/* ---------- live ---------- */
async function refreshRuns(){
  runs=await j('/api/runs');
  const prev=cur;
  cur = prev && runs.some(r=>r.run_id===prev) ? prev : (runs.length?runs[runs.length-1].run_id:null);
  const el=document.getElementById('runlist');
  el.innerHTML=runs.map(r=>{
    const dotCls=['RELEASED','REJECTED','QUARANTINED','ESCALATED','ROLLED_BACK'].includes(r.state)?r.state:'other';
    return `<div class="ritem${r.run_id===cur?' sel':''}" onclick="pickRun('${r.run_id}')">
      <span class="dot ${dotCls}"></span><span class="name">${esc(r.run_id)}</span>
      <span class="dim2" style="font-size:10px">${r.sealed?'🔒':''}${ago(r.last_ts)}</span></div>`;
  }).join('')||'<div class="dim2" style="padding:8px">暂无运行</div>';
}
function pickRun(sid){cur=sid;selFile=null;refresh();}

function renderBanner(s){
  const b=document.getElementById('banner');
  let cls='',extra='';
  if(s.state==='RELEASED')cls='green';
  if(['REJECTED','QUARANTINED'].includes(s.state))cls='red';
  if(s.state==='ESCALATED'){cls='esc';
    extra=`<button class="btn" onclick="doResolve(true)">✓ 批准续跑</button>
           <button class="btn danger" onclick="doResolve(false)">✗ 拒绝终止</button>`;}
  b.className='banner '+cls;
  b.innerHTML=`<span class="st">${s.state}</span><span class="mean">${esc(s.state_meaning)}
    ${s.rework_round?` · 重修 ${s.rework_round}/${s.max_rework_rounds}`:''}
    ${s.has_certificate?' · <a href="javascript:showCert()">📜 公证书</a>':''}</span>${extra}`;
}
window.showCert=async function(){
  const d=await j(`/api/file/${cur}/certificate.md`);
  selFile='certificate.md';
  document.getElementById('evview').textContent=d.content||'(无)';
  document.getElementById('verify').innerHTML='';
  renderEvidence(window._files||[]);
};

function renderSM(s){
  const traceStates=[...new Set(s.trace_tail.map(e=>e.state_after))].concat([s.state]);
  let h='';
  SM_MAIN.forEach((x,i)=>{
    let c='st2';
    if(x===s.state)c+=' cur';else if(traceStates.includes(x))c+=' done';
    h+=`<span class="${c}">${x}</span>`;
    if(i<SM_MAIN.length-1)h+='<span class="arrow">→</span>';
  });
  h+='<span class="arrow" style="margin:0 8px">│</span>';
  SM_SIDE.forEach(([x,t])=>{
    let c='st2';if(x===s.state)c+=` side-cur ${t}`;
    h+=`<span class="${c}">${x}</span>`;});
  document.getElementById('sm').innerHTML=h;
}

function renderBeats(b){
  let lastDone=-1;BEATS.forEach(([k],i)=>{if(b[k]&&b[k].done)lastDone=i;});
  document.getElementById('beats').innerHTML=BEATS.map(([k,n],i)=>{
    const done=b[k]&&b[k].done;
    const cls=done?'beat done':(i===lastDone+1?'beat cur':'beat');
    const sub=done?`${b[k].calls} 调用 · ${ago(b[k].ts)}`:(i===lastDone+1?'进行中':'待执行');
    return `<div class="${cls}"><div class="n">${n}</div><div class="s">${sub}</div></div>`;
  }).join('<span class="arrow" style="align-self:center">→</span>');
}

function renderGates(v){
  document.getElementById('gates').innerHTML=GATES.map(([k,n])=>{
    const g=v[k];const d=g?g.decision:'—';const cls=g?g.decision:'none';
    const sum=g?(g.summary||'').slice(0,60):'未执行';
    return `<div class="gate ${cls}" onclick="showGate('${k}')"><div class="t">${n}</div>
      <div class="d">${d.toUpperCase()}</div><div class="sm2">${esc(sum)}</div></div>`;}).join('');
  window._verdicts=v;
}
window.showGate=function(k){const g=window._verdicts[k];
  document.getElementById('gatedetail-title').textContent='门禁详情 · '+k;
  document.getElementById('gatedetail').textContent=g?JSON.stringify(g,null,2):'该门禁尚未执行';};

function renderMetrics(m,s){
  const wall=m?m.wall_time_s:'—';
  const items=[['终态',s.state],['工具调用',m?m.tool_calls:s.events],['时长(s)',wall],
    ['封印文件',s.sealed_files.length],['对抗环迭代',m?m.adversarial_loop_iterations:'—'],
    ['举证条数',m?m.rebuttals_submitted:'—'],['人工介入',m?m.human_interventions:'—'],
    ['重修轮次',`${s.rework_round}/${s.max_rework_rounds}`],['Skill 咨询',m?(m.skill_match_calls??0):'—'],
    ['越权拒绝',s.security_events.length]];
  document.getElementById('metrics').innerHTML=items.map(([l,v])=>
    `<div class="k"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');
}

function renderSkillCalls(t){
  const rows=(t||[]).filter(e=>e.tool&&e.tool.startsWith('notary_skill'));
  const el=document.getElementById('skillcalls');
  if(!rows.length){el.innerHTML='<div class="dim2" style="padding:8px">本次运行没有技能调用记录</div>';return;}
  el.innerHTML='<table class="trace">'+rows.map(e=>{
    const role=e.role||'unknown';
    return `<tr><td class="dim2">${new Date(e.ts*1000).toLocaleTimeString()}</td>
      <td><span class="chip" style="color:var(--purple);border-color:var(--purple)">${role}</span></td>
      <td style="color:var(--purple)">${e.tool}</td><td>${e.state_after}</td>
      <td class="dim2 mono">${(e.result_sha256||'').slice(0,16)}</td></tr>`;}).join('')+'</table>';
}

function renderTrace(t){
  const rows=t.map(e=>{
    const role=e.role||'unknown';const c=ROLE_COLOR[role]||'#8b949e';
    const isSkill=e.tool&&e.tool.startsWith('notary_skill');
    return `<tr${e.event?' class="warnrow"':''}><td class="dim2">${new Date(e.ts*1000).toLocaleTimeString()}</td>
      <td><span class="chip" style="color:${c};border-color:${c}">${role}</span></td>
      <td${isSkill?' style="color:var(--purple);font-weight:700"':''}>${e.tool}</td><td>${e.state_after}</td><td class="dim2">${e.duration_ms}ms</td>
      <td class="dim2">${(e.payload_sha256||'').slice(0,8)}→${(e.result_sha256||'').slice(0,8)}</td>
      <td>${e.event?'⚠'+e.event:''}</td></tr>`;}).join('');
  const el=document.getElementById('trace');el.innerHTML=rows;
  el.parentElement.scrollTop=el.parentElement.scrollHeight;
}

function renderEvidence(files){
  const cert=files.filter(f=>f==='certificate.md');
  const rest=files.filter(f=>f!=='certificate.md');
  document.getElementById('evlist').innerHTML=cert.concat(rest).map(f=>
    `<div class="evitem${f===selFile?' sel':''}" onclick="showFile('${f}')">
      <span>${f==='certificate.md'?'📜 ':''}${f}</span></div>`).join('')
    ||'<div class="evitem">尚未封印</div>';
}
window.showFile=async function(f){
  selFile=f;
  const d=await j(`/api/file/${cur}/${encodeURIComponent(f)}`);
  document.getElementById('evview').textContent=d.content||'(空或二进制)';
  renderEvidence(window._files||[]);
  const v=document.getElementById('verify');
  if(d.declared_sha256&&d.content!==undefined){
    const buf=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(d.content));
    const hex=[...new Uint8Array(buf)].map(b=>b.toString(16).padStart(2,'0')).join('');
    const ok=hex===d.declared_sha256;
    v.innerHTML=ok
      ?`<span class="ok2">✓ 封印一致</span> <span class="dim2">浏览器重算 SHA-256 = manifest 登记值（${hex.slice(0,16)}…）</span>`
      :`<span class="no2">✗ 封印不一致！</span> <span class="dim2">重算 ${hex.slice(0,16)}… ≠ 登记 ${d.declared_sha256.slice(0,16)}…</span>`;
  }else v.innerHTML='';
};

async function doReset(){
  if(!cur||!confirm(`重置 ${cur}？（run 产物将重建）`))return;
  await j('/api/reset/'+cur,{method:'POST'});refresh();
}
async function doResolve(approve){
  const r=await j('/api/resolve/'+cur,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({approve})});
  if(!r.ok)alert('裁决失败: '+(r.error||''));refresh();
}

async function refresh(){
  try{
    const gw=await j('/api/gw');
    const g=document.getElementById('gw');
    if(gw.ok){g.textContent='网关 '+gw.version+' ●';g.className='pill on';}
    else{g.textContent='网关离线';g.className='pill';}
    await refreshRuns();
    if(!cur)return;
    const s=await j('/api/run/'+cur);
    renderBanner(s);renderSM(s);renderBeats(s.beats);renderGates(s.verdicts);
    renderMetrics(s.metrics,s);renderTrace(s.trace_tail);renderSkillCalls(s.trace_tail);
    window._files=s.sealed_files;renderEvidence(s.sealed_files);
    document.getElementById('clock').textContent=new Date().toLocaleTimeString();
  }catch(e){/* busy */}
}

/* ---------- gallery ---------- */
async function loadGallery(){
  const cards=await j('/api/scenarios');
  const ok=cards.filter(c=>c.expect_final&&c.actual_final===c.expect_final).length;
  document.getElementById('gsummary').innerHTML=
    `<span class="big">${cards.length}</span> 场景 · <span class="ok2">${ok} 结局符合预期</span> · 点击卡片跳实时运行`;
  document.getElementById('gcards').innerHTML=cards.map(c=>{
    const match=c.expect_final?(c.actual_final===c.expect_final
      ?'<span class="ok2">✓ 实际 '+c.actual_final+'</span>'
      :`<span class="no2">✗ 实际 ${c.actual_final||'未跑'}</span>`):'<span class="dim2">live-only</span>';
    return `<div class="card" onclick="pickRun('${c.scenario_id}');showView('live')">
      <div>${c.source?`<span class="src ${c.source}">${c.source}</span>`:''}
      <span class="id">${c.sealed?' 🔒':''}</span></div>
      <div class="t">${esc(c.title)}</div>
      <div class="id">${c.scenario_id}</div>
      <div class="matchline">${c.expect_final?`预期 ${c.expect_final} · `:''}${match}
      ${c.prototype?` · <a href="${c.prototype}" onclick="event.stopPropagation()">原型 ↗</a>`:''}</div></div>`;}).join('');
}

/* ---------- skills ---------- */
async function loadSkills(){
  const d=await j('/api/skills');
  const sel=document.getElementById('sigsel');
  sel.innerHTML=d.signals.map(s=>`<option value="${s.signal}">${s.signal} — ${esc(s.trigger)}</option>`).join('');
  document.getElementById('skillgrid').innerHTML=d.skills.map(s=>{
    const tags=s.retired?'<span class="tag ret">已退役</span>'
      :(s.source==='seed'?'<span class="tag seed">种子</span>':'<span class="tag reg">沉淀</span>');
    return `<div class="skill${s.retired?' ret':''}">
      <div class="nm">${esc(s.name)} ${tags}</div>
      <div class="dim2" style="font-size:11px;margin-top:2px">v${s.version||'?'} · ${s.compat||'unversioned'}</div>
      <div class="ds">${esc(s.description)}</div></div>`;}).join('');
  document.getElementById('ledger').innerHTML=d.ledger.map(e=>
    `<tr><td class="dim2">${e.ts?new Date(e.ts*1000).toLocaleString():''}</td>
     <td><span class="chip" style="color:${e.action==='retire'?'var(--red)':'var(--green)'};border-color:${e.action==='retire'?'var(--red)':'var(--green)'}">${e.action}</span></td>
     <td>${esc(e.name)}</td><td class="dim2">v${e.version||''} ${e.supersedes?'⤳ '+e.supersedes:''}</td>
     <td class="dim2">${(e.content_sha256||'').slice(0,10)}</td>
     <td class="dim2">${esc(e.source_run||e.reason||'')}</td></tr>`).join('');
}
window.tryMatch=async function(){
  const sig=document.getElementById('sigsel').value;
  const r=await j('/api/skill_match',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({signal:sig,sid:cur})});
  const out=document.getElementById('matchout');
  if(r.ok){const d=r.result;
    out.innerHTML=`→ <b>${esc(d.skill)}</b>（${d.status}，v${d.version||'?'}）
      ${d.resolved_from?`<span class="dim2"> · 经版本链自 ${esc(d.resolved_from)} 解析</span>`:''}
      <span class="dim2"> · 适用角色: ${(d.roles||[]).join('/')}</span>`;}
  else out.innerHTML=`<span class="no2">${esc(r.error||'error')}</span>`;
};

/* ---------- audit ---------- */
async function loadAudit(){
  const d=await j('/api/audit');
  const roles=Object.keys(d.matrix).sort();
  const tools=[...new Set(Object.values(d.matrix).flatMap(m=>Object.keys(m)))].sort();
  let h='<table class="matrix"><tr><th>role \\ tool</th>'+tools.map(t=>`<th>${t.replace('notary_','')}</th>`).join('')+'</tr>';
  for(const r of roles){h+=`<tr><th>${r}</th>`+tools.map(t=>{
    const n=d.matrix[r][t];return n?`<td class="hot">${n}</td>`:'<td></td>';}).join('')+'</tr>';}
  document.getElementById('matrix').innerHTML=h+'</table>';
  document.getElementById('sec').innerHTML=d.security_events.length?d.security_events.map(e=>
    `<tr class="warnrow"><td class="dim2">${e.ts?new Date(e.ts*1000).toLocaleString():''}</td>
     <td>${e.role}</td><td>${e.tool}</td></tr>`).join(''):'<tr><td class="dim2">零越权调用</td></tr>';
  document.getElementById('alerts').innerHTML=d.alerts.length?d.alerts.map(a=>
    `<tr><td class="dim2">${a.ts?new Date(a.ts*1000).toLocaleString():''}</td>
     <td><span class="chip" style="color:var(--amber);border-color:var(--amber)">${a.rule}</span></td>
     <td>${esc(a.run||'')}</td><td class="dim2">${esc(a.detail||'')}</td></tr>`).join('')
    :'<tr><td class="dim2">暂无告警（watchdog 未运行或全正常）</td></tr>';
  document.getElementById('asummary').innerHTML=
    `<span class="big ${d.security_events.length?'no2':'ok2'}">${d.security_events.length?'':'✓'}</span>
     ${d.security_events.length?d.security_events.length+' 次越权拒绝（全部留痕）':'零越权调用'} · ${d.alerts.length} 条告警`;
  const p=await j('/api/policy');
  document.getElementById('policy').textContent=p.ok?JSON.stringify(p.result,null,2):'(网关离线：policy 不可得)';
}

/* ---------- intake modal ---------- */
function openIntake(){document.getElementById('intakemodal').style.display='flex';
  document.getElementById('intakeform').style.display='';
  document.getElementById('intakeresult').style.display='none';}
function closeIntake(){document.getElementById('intakemodal').style.display='none';}
async function submitIntake(){
  const filesRaw=document.getElementById('f-files').value.trim();
  let files=null;
  if(filesRaw){try{files=JSON.parse(filesRaw);}catch(e){alert('补丁需为 JSON：{"文件名.py": "内容"}');return;}}
  const body={target:document.getElementById('f-target').value,
    title:document.getElementById('f-title').value.trim(),
    report:document.getElementById('f-report').value.trim(),
    expected_behavior:document.getElementById('f-expect').value.trim()};
  if(files)body.files=files;
  const r=await j('/api/intake',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const out=document.getElementById('intakeresult');
  if(r.ok){
    const sid=r.result.scenario_id;
    const task=`@codenotary-leader 请让你的 Team 公证一条新的变更任务。\n\nscenario_id: ${sid}\n模式：${r.result.mode==='external'?'外部 AI 变更送审（external）':'流水线自研修复（inhouse）'}\n\n请按完整公证流程处理，并输出本次公证报告。`;
    out.innerHTML=`<div class="resultbox">✓ 已建档 <b>${sid}</b>（${r.result.mode}）<br><br>
      下一步：复制以下文案到 Element「Team: codenotary」房间发送即开工：
      <pre style="margin-top:8px;max-height:160px">${esc(task)}</pre>
      <div class="row"><button class="btn ghost" onclick="navigator.clipboard.writeText(\`${task.replace(/`/g,'')}\`)">复制任务文案</button>
      <button class="btn" onclick="closeIntake();pickRun('${sid}');showView('live')">去看运行 →</button></div></div>`;
  }else{
    out.innerHTML=`<div class="resultbox" style="border-color:var(--red)">✗ ${esc(r.error||'未知错误')}</div>`;
  }
  document.getElementById('intakeform').style.display='none';
  out.style.display='';
}

const h=location.hash.replace('#','');
const [hv,hr]=h.split('/');
if(hr)cur=hr;
if(['live','gallery','skills','audit'].includes(hv))showView(hv);
refresh();setInterval(refresh,2500);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 办事大厅（/desk）——送审开发者的客户视图。
# 与工程后台（/）共用同一批 /api 端点，零网关改动；术语政策：本页不出现
# 状态机/门禁/trace 等内部名词，全部翻译为办事语言。
# ---------------------------------------------------------------------------

DESK_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodeNotary 代码公证处 · 办事大厅</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;--purple:#bc8cff;--cyan:#39c5cf}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.7 -apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--bg);z-index:20;flex-wrap:wrap}
h1{font-size:17px;display:flex;align-items:center;gap:10px}
.badge{font-size:11px;color:var(--dim);font-weight:400}
.pill{font-size:11px;border:1px solid var(--line);border-radius:99px;padding:3px 10px;color:var(--dim)}
.pill.on{color:var(--green);border-color:var(--green)}
.btn{background:var(--blue);color:#06101f;border:none;border-radius:8px;padding:8px 18px;font-weight:700;
cursor:pointer;font-size:13px;text-decoration:none;display:inline-block}
.btn.ghost{background:none;border:1px solid var(--line);color:var(--fg);font-weight:400}
.btn.danger{background:none;border:1px solid var(--red);color:var(--red)}
.btn.big{font-size:15px;padding:12px 26px;border-radius:10px}
main{max-width:880px;margin:0 auto;padding:20px 16px 60px}
.hero{padding:34px 0 26px}
.hero .big{font-size:26px;font-weight:800;line-height:1.4}
.hero .sub{color:var(--dim);margin-top:12px;font-size:14px;max-width:640px}
.dtabs{display:flex;gap:6px;margin-top:12px}
.dtabs button{background:none;border:1px solid var(--line);color:var(--dim);padding:7px 18px;
border-radius:8px;cursor:pointer;font-size:13px}
.dtabs button.on{background:var(--panel2);color:var(--fg);border-color:var(--blue);font-weight:700}
input[type=file]{width:100%;background:#0a0e14;border:1px dashed var(--line);border-radius:8px;
color:var(--dim);padding:9px 11px;font-size:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:16px}
.panel h2{font-size:15px;margin-bottom:6px}
.note{color:var(--dim);font-size:12px;margin-top:4px}
label{display:block;font-size:13px;font-weight:600;margin:16px 0 6px}
input[type=text],textarea,select{width:100%;background:#0a0e14;border:1px solid var(--line);border-radius:8px;
color:var(--fg);padding:9px 11px;font:13px/1.6 inherit;font-family:inherit}
textarea{min-height:88px;resize:vertical}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--blue)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px;cursor:pointer}
.card:hover{border-color:var(--blue)}
.card .rid{font:11px ui-monospace,monospace;color:var(--dim)}
.card .st2{font-size:14px;font-weight:700;margin:6px 0 2px}
.card .tm{font-size:11px;color:var(--dim)}
.chip{display:inline-block;font-size:11px;font-weight:700;border-radius:99px;padding:2px 10px}
.chip.doing{background:#58a6ff22;color:var(--blue)}
.chip.wait{background:#d2992222;color:var(--amber)}
.chip.pass{background:#3fb95022;color:var(--green)}
.chip.fail{background:#f8514922;color:var(--red)}
.chip.gone{background:#bc8cff22;color:var(--purple)}
.banner{border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-top:16px;background:var(--panel)}
.banner .t{font-size:19px;font-weight:800}
.banner .d{color:var(--dim);margin-top:4px;font-size:13px}
.banner.green{border-color:var(--green)}.banner.green .t{color:var(--green)}
.banner.red{border-color:var(--red)}.banner.red .t{color:var(--red)}
.banner.amber{border-color:var(--amber);background:#d2992211}.banner.amber .t{color:var(--amber)}
.steps{display:flex;gap:6px;margin-top:18px;flex-wrap:wrap}
.step{flex:1;min-width:110px;border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center;
background:var(--panel)}
.step .n{font-size:13px;font-weight:700}
.step .s{font-size:11px;color:var(--dim);margin-top:3px}
.step.done{border-color:var(--green)}.step.done .n{color:var(--green)}
.step.cur{border-color:var(--blue);box-shadow:0 0 10px #58a6ff44;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.5}}
pre{background:#0a0e14;border:1px solid var(--line);border-radius:8px;padding:12px;
font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;overflow:auto;max-height:380px;white-space:pre-wrap}
.mono{font-family:ui-monospace,monospace}
.reason{border-left:3px solid var(--red);background:#f851490d;border-radius:0 8px 8px 0;
padding:10px 14px;margin-top:10px}
.reason .p{font-weight:700}
.reason .w{color:var(--dim);font-size:13px;margin-top:3px}
.tip{border-left:3px solid var(--blue);background:#58a6ff0d;border-radius:0 8px 8px 0;
padding:10px 14px;margin-top:10px;color:var(--dim)}
ol.howto{margin:8px 0 0 20px;color:var(--fg)}
ol.howto li{margin-top:6px}
.row{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
.gate3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}
.g3{border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center;background:var(--panel)}
.g3 .t{font-size:12px;color:var(--dim)}
.g3 .d{font-size:17px;font-weight:800;margin-top:4px}
.g3.green{border-color:var(--green)}.g3.green .d{color:var(--green)}
.g3.red{border-color:var(--red)}.g3.red .d{color:var(--red)}
.g3.none .d{color:var(--dim)}
a{color:var(--blue);text-decoration:none}
.empty{color:var(--dim);text-align:center;padding:26px 0 10px}
/* 对话工作区 */
.workspace{display:grid;grid-template-columns:230px 1fr;gap:12px;margin-top:16px}
.sidepanel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;
display:flex;flex-direction:column;min-height:300px}
.sidehead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.sidehead .t{font-size:13px;font-weight:700;color:var(--dim)}
.newconv{background:var(--blue);color:#06101f;border:none;border-radius:6px;padding:4px 12px;
font-weight:700;cursor:pointer;font-size:13px}
.convlist{display:flex;flex-direction:column;gap:6px;flex:1}
.conv{border:1px solid var(--line);border-radius:8px;padding:8px 10px;cursor:pointer;font-size:12px;
background:var(--panel)}
.conv:hover{background:var(--panel2)}
.conv.sel{border-color:var(--blue)}
.conv .t{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conv .s{font-size:10px;color:var(--dim);margin-top:2px}
.conv .del{float:right;color:var(--dim);padding:0 3px}
.conv .del:hover{color:var(--red)}
.chatlog{display:flex;flex-direction:column;gap:10px;max-height:560px;overflow:auto;padding:4px 2px}
.msg{border-radius:10px;padding:10px 14px;font-size:13px;line-height:1.6;max-width:88%}
.msg.user{background:#2E75B622;border:1px solid #2E75B655;align-self:flex-end}
.msg.assistant{background:var(--panel);border:1px solid var(--line);align-self:flex-start}
.msg.sys{background:none;border:none;color:var(--dim);font-size:12px;align-self:center;padding:2px}
.composer{display:flex;gap:8px;margin-top:10px;align-items:flex-end}
.composer textarea{flex:1;min-height:44px;max-height:120px}
.slashmenu{position:absolute;background:var(--panel2);border:1px solid var(--blue);border-radius:8px;
z-index:30;min-width:320px;box-shadow:0 4px 16px #0008}
.slashitem{padding:7px 12px;cursor:pointer;font-size:12px;display:flex;gap:8px}
.slashitem:hover,.slashitem.sel{background:#2E75B633}
.slashitem .cmd{font-family:ui-monospace,monospace;color:var(--blue);font-weight:700}
.slashitem .ds{color:var(--dim)}
.skilltag{display:inline-block;background:#3fb95022;color:var(--green);border-radius:4px;
padding:1px 7px;font-size:11px;font-weight:700;margin:2px 4px 2px 0}
.footer{margin-top:40px;color:var(--dim);font-size:11px;text-align:center}
</style>
</head>
<body>
<header>
  <h1>⚖️ CodeNotary 代码公证处 <span class="badge">办事大厅</span></h1>
  <span style="flex:1"></span>
  <span class="pill" id="gw">…</span>
  <a class="btn ghost" href="/pipeline">工程后台 →</a>
</header>
<main id="app"></main>
<script>
const app=document.getElementById('app');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function j(u,o){const r=await fetch(u,o);return r.json();}
const TERMINAL=['RELEASED','REJECTED','QUARANTINED','ROLLED_BACK'];
let timer=null;

/* ---------------- 翻译层：内部状态 → 办事语言 ---------------- */
const CHIP={
  doing:['doing','办理中'],wait:['wait','等你拿主意'],pass:['pass','已发证'],
  fail:['fail','被退回'],gone:['gone','已撤销']};
function chipOf(state,advisory){
  if(state==='ESCALATED')return CHIP.wait;
  if(state==='NOTARIZED')return ['pass','体检全过，待发证'];
  if(state==='RELEASED')return advisory?['wait','建议已出']:CHIP.pass;
  if(state==='REJECTED'||state==='QUARANTINED')return CHIP.fail;
  if(state==='ROLLED_BACK')return CHIP.gone;
  return CHIP.doing;
}
const STATE_TEXT={
  RECEIVED:['收到了','排队安检中。'],
  SCREENED:['安检过了','正在给问题定性。'],
  TRIAGED:['问题定性了','在找根子上的原因。'],
  DIAGNOSED:['原因找到了','正在把验收标准一条一条定下来。'],
  CONTRACTED:['验收标准冻结了','写代码的和挑刺的分头开工，互相看不到对方的东西。'],
  AUTHORING:['正在写实现','写代码的看不到测试——防止照着测试凑答案。'],
  TESTING:['正在写测试','挑刺的看不到实现——测的才是真本事。'],
  GATING:['最后一关','三道体检同时来：功能测试、变异测试、行为检查。'],
  NOTARIZED:['三道全绿','公证书正在签发。'],
  RELEASED:['办好了','公证书在下面，谁都可以验真伪。'],
  ESCALATED:['需要你拿主意','系统拿不准，停下来等你。它不会替你猜。'],
  REJECTED:['这次没通过','原因在下面，条条具体，也都是能改的。'],
  QUARANTINED:['安检拦下了','补丁里有危险写法，没让进门。'],
  ROLLED_BACK:['后来撤销了','这次公证已作废，证据都还留着。']};
const STEPS=[
  ['受理安检','先扫危险写法'],
  ['分析定位','找问题的根子'],
  ['定标准','验收标准冻结'],
  ['分头干活','写码挑刺互不见面'],
  ['三道体检','功能 · 变异 · 行为'],
  ['出证','公证书或退件原因']];
function stageOf(state,history){
  const m={RECEIVED:0,SCREENED:0,TRIAGED:1,DIAGNOSED:1,CONTRACTED:2,AUTHORING:3,TESTING:3,
    GATING:4,NOTARIZED:5,RELEASED:5,REJECTED:5,QUARANTINED:0,ROLLED_BACK:5};
  if(state==='ESCALATED'){
    const prev=(history||[]).filter(s=>s!=='ESCALATED').pop();
    return m[prev]??2;
  }
  return m[state]??0;
}

/* ---------------- 翻译层：审查发现 → 人话 ---------------- */
function translateFinding(detail,rule){
  const d=(detail||'').toLowerCase(),r=(rule||'').toLowerCase();
  const has=(...ws)=>ws.some(w=>d.includes(w)||r.includes(w));
  if(has('unaudited file write'))return{
    p:'代码会往磁盘写文件，可这不在说好的改动范围内。',
    w:'写文件这个动作可能把数据带出去。要么把它写进验收标准，要么去掉。'};
  if(has('os.system'))return{
    p:'用 os.system 直接执行系统命令。',
    w:'AI 图省事爱这么写，但那个字符串可以是任何命令。换成更稳的写法，或者去掉。'};
  if(has('shell=true','shell_mode'))return{
    p:'subprocess 开了 shell 模式。',
    w:'传进去的字符串会被当成命令解析，外部数据混进去就是注入。'};
  if(has('hardcoded credential','hardcoded api key','hardcoded_credential'))return{
    p:'代码里写死了密码或密钥。',
    w:'合入仓库就等于公开。移到环境变量或配置中心去。'};
  if(has('unsafe deserialization','pickle'))return{
    p:'用 pickle 读外部数据。',
    w:'构造过的 pickle 数据能在你机器上跑任意代码。换 JSON 这类安全格式。'};
  if(has('dynamic eval','dynamic exec','eval','exec'))return{
    p:'把字符串当代码执行（eval/exec）。',
    w:'字符串里是什么没法审查，等于留了暗门。'};
  if(has('resource-acquisition-without-release'))return{
    p:'打开了资源（比如文件）却没有释放。',
    w:'短时间没事，跑久了资源漏光，服务会垮。用 with，或者记得 close()。'};
  if(has('bare except','bare-except'))return{
    p:'用 except 裸捕所有异常。',
    w:'出什么错都被吞掉，出了问题没法查。写清楚捕哪一种。'};
  return{p:detail||rule||'一处不符合规范的写法。',
    w:'对照验收标准改，或者来问问我们为什么算违规。'};
}

/* ---------------- 大厅 ---------------- */
async function hall(prefill){
  stopPoll();
  const runs=await j('/api/runs');
  runs.sort((a,b)=>(b.last_ts||0)-(a.last_ts||0));
  const cards=runs.map(r=>{
    const[cls,txt]=chipOf(r.state,r.advisory);
    const tm=r.last_ts?new Date(r.last_ts*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
    return`<div class="card" onclick="location.hash='#/r/${encodeURIComponent(r.run_id)}'">
      <div class="rid">公证编号 ${esc(r.run_id)}</div>
      <div class="st2"><span class="chip ${cls}">${txt}</span></div>
      <div class="tm">${tm}</div></div>`;}).join('');
  app.innerHTML=`
  <div class="hero">
    <div class="big">带问题来，AI 帮你修；带补丁来，公证处帮你审。</div>
    <div class="sub">十个角色各查一遍：功能对不对、测试扎不扎实、行为守不守规矩。
    全过了，发你一份公证书——谁都可以独立验算真伪。没过，告诉你卡在哪、怎么改。</div>
  </div>

  <div class="panel" style="margin-top:16px">
    <h2>开始一次公证</h2>
    <div class="note">📎 附上代码包，再说一句你的问题；打 <b>/</b> 有命令（/skill /example /help），/reset 重新开始。</div>
    <div class="chatlog" id="chatlog" style="margin-top:10px"></div>
    <div id="slashbox"></div>
    <div class="composer">
      <input type="file" id="c-zip" accept=".zip" style="display:none" onchange="attachZip(this)">
      <button class="btn ghost" title="附上代码包（.zip）" onclick="document.getElementById('c-zip').click()">📎</button>
      <textarea id="c-input" placeholder="说说你的问题……（打 / 有命令：/skill /example /help）"
        oninput="onChatInput(this)" onkeydown="onChatKey(event,this)"></textarea>
      <button class="btn" onclick="sendChat()">发送</button>
    </div>
    <div class="note" id="c-zipinfo" style="margin-top:4px"></div>
  </div>

  <details class="panel" style="margin-top:16px">
    <summary style="cursor:pointer;font-weight:700">高级模式：逐项填写</summary>
    <div id="dform" style="margin-top:10px">
    <div class="note">带 * 的必填。写细一点，审得就准一点。</div>
    <div class="dtabs">
      <button id="dtab-fix" class="on" onclick="dmode('fix')">我要修代码</button>
      <button id="dtab-review" onclick="dmode('review')">我要审代码</button>
      <span style="flex:1"></span>
      <button class="btn ghost" style="align-self:center" onclick="fillExample()">给我个例子</button>
    </div>

    <label>给这次改动起个名字 *</label>
    <input type="text" id="f-title" placeholder="比如：修复消息丢失的问题" value="${esc(prefill?.title||'')}">

    <div id="mode-fix">
      <label>你的代码文件（.py，可多选）*</label>
      <input type="file" id="f-src" multiple accept=".py" onchange="showFiles(this,'fl-src')">
      <div class="note" id="fl-src"></div>
      <label>你的测试文件（test_*.py）*</label>
      <input type="file" id="f-tst" multiple accept=".py" onchange="showFiles(this,'fl-tst')">
      <div class="note" id="fl-tst"></div>
      <label>怎么触发这个问题？（可选，但强烈建议）</label>
      <textarea id="f-repro" placeholder="贴一小段能触发问题的代码。它会作为证据入档，帮流水线更快定位。"></textarea>
    </div>

    <div id="mode-review" style="display:none">
      <label>改动发生在哪个服务 *</label>
      <select id="f-target">
        <option value="queue_box">消息队列（queue_box）</option>
        <option value="dispatcher">消息分发器（dispatcher）</option>
        <option value="mailbox_router">邮箱路由器（mailbox_router）</option>
      </select>
      <label>补丁文件（.py，可多选；不上传 = 让流水线的作者角色自己改）</label>
      <input type="file" id="f-patch" multiple accept=".py" onchange="showFiles(this,'fl-patch')">
      <div class="note" id="fl-patch"></div>
    </div>

    <label>这次改动要解决什么问题？ *</label>
    <textarea id="f-report" placeholder="把背景交代清楚：什么现象、什么时候发现的、影响是什么。（至少 20 个字）">${esc(prefill?.report||'')}</textarea>
    <label>怎样才算改好了？ * <a href="javascript:void 0" style="font-weight:400;font-size:12px" onclick="insertSkeleton()">插个骨架 →</a></label>
    <textarea id="f-expect" placeholder="一条一行，写得能检查。比如：消费失败的消息不丢，重试 3 次后进死信列表。（至少 20 个字）">${esc(prefill?.expected_behavior||'')}</textarea>
    <div class="note">这会成为验收标准，之后照这个验——写清楚，后面不扯皮。</div>
    <div class="row"><button class="btn" onclick="submitIntake()">提交公证</button></div>
    <div class="note" style="margin-top:8px">接入你们 CI 后，这些字段由工单模板自动带过来，不用手填。</div>
    <div id="intakeresult"></div>
  </div>
  </details>

  <div class="panel">
    <h2>我的公证</h2>
    ${runs.length?`<div class="cards">${cards}</div>`:`<div class="empty">还没有记录。第一次来？点「＋ 新对话」，把代码包给我，再说一句你的问题。</div>`}
  </div>
  <div class="footer">CodeNotary · 公证过程在工程后台全程可见（右上角进去）</div>`;
  renderAll();
}

let _dmode='fix';
const EXAMPLES={
  fix:{title:'计算器需要支持减法',
    report:'我们的结算服务用的是自己写的 calc 模块，目前只有加法。财务对账时要算差额，现在得在外面临时写 a-b，容易出错。请给模块补一个减法函数，正数负数都要对。',
    expect:'1) calc.py 新增 sub(a, b)，返回 a - b；\n2) 负数运算结果正确；\n3) 现有 add(a, b) 行为不变；\n4) 只改 calc.py。',
    repro:'from calc import sub\nprint(sub(10, 3))   # 期望 7\nprint(sub(-2, -5))  # 期望 3',
    src:{'calc.py':'def add(a, b):\n    return a + b\n'},
    tst:{'test_calc.py':'import unittest\nfrom calc import add\n\nclass TestCalc(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n'}},
  review:{title:'修复空队列取消息报错的问题',
    report:'生产环境发现：消费者从空队列取消息时，抛的是列表越界的原始错误，监控根本看不懂发生了什么。希望空队列时给一个干净、明确的错误信息，正常取消息的行为不变。',
    expect:"1) 空队列 pop 抛出 IndexError，消息为 'pop from empty mailbox'；\n2) 非空队列保持 FIFO，先进先出；\n3) len() 与 push 行为不变；\n4) 只改 queue_box.py。",
    repro:'',tst:null,
    src:{'queue_box.py':'# A tiny FIFO mailbox used by the demo target service.\n\n\nclass Mailbox:\n    # A minimal FIFO message box.\n\n    def __init__(self) -> None:\n        self._items: list[str] = []\n\n    def __len__(self) -> int:\n        return len(self._items)\n\n    def push(self, msg: str) -> None:\n        self._items.append(msg)\n\n    def pop(self) -> str:\n        # 空队列给出干净的错误，而不是原始列表越界\n        if not self._items:\n            raise IndexError("pop from empty mailbox")\n        return self._items.pop(0)\n'}}};
function fillExample(){
  const ex=EXAMPLES[_dmode];
  const dirty=['f-title','f-report','f-expect'].some(id=>document.getElementById(id).value.trim());
  if(dirty&&!confirm('会用示例覆盖当前已填的内容，继续？'))return;
  document.getElementById('f-title').value=ex.title;
  document.getElementById('f-report').value=ex.report;
  document.getElementById('f-expect').value=ex.expect;
  if(_dmode==='fix'){
    document.getElementById('f-repro').value=ex.repro;
    document.getElementById('fl-src').innerHTML='示例源码（存成 calc.py 即可试）：'+
      Object.entries(ex.src).map(([n,c])=>`<div style="margin-top:4px"><a href="javascript:void 0" onclick='navigator.clipboard.writeText(${JSON.stringify(c)}).then(()=>alert("已复制 ${n}"))'>复制 ${n}</a><pre style="max-height:80px;margin-top:4px">${esc(c)}</pre></div>`).join('');
    document.getElementById('fl-tst').innerHTML='示例测试（存成 test_calc.py）：'+
      Object.entries(ex.tst).map(([n,c])=>`<div style="margin-top:4px"><a href="javascript:void 0" onclick='navigator.clipboard.writeText(${JSON.stringify(c)}).then(()=>alert("已复制 ${n}"))'>复制 ${n}</a><pre style="max-height:100px;margin-top:4px">${esc(c)}</pre></div>`).join('');
  }else if(ex.src){
    document.getElementById('fl-patch').innerHTML='示例补丁（存成 queue_box.py 即可试）：'+
      Object.entries(ex.src).map(([n,c])=>`<div style="margin-top:4px"><a href="javascript:void 0" onclick='navigator.clipboard.writeText(${JSON.stringify(c)}).then(()=>alert("已复制 ${n}"))'>复制 ${n}</a><pre style="max-height:120px;margin-top:4px">${esc(c)}</pre></div>`).join('');
  }
}
function insertSkeleton(){
  const el=document.getElementById('f-expect');
  if(el.value.trim()&&!confirm('会用骨架覆盖当前内容，继续？'))return;
  el.value='1) 功能：……（要达到什么效果）\n2) 边界：……（空输入、异常情况怎么表现）\n3) 范围：只改 文件名.py，其他文件不动。';
  el.focus();
}
document.addEventListener('focusout',e=>{
  if(e.target&&e.target.id==='f-report'){
    const t=document.getElementById('f-title');
    if(!t.value.trim()){
      const first=e.target.value.trim().split('\n')[0].replace(/[。！？.!?].*$/,'');
      if(first.length>=8)t.value=first.slice(0,40);
    }
  }
});
function dmode(m){
  _dmode=m;
  document.getElementById('mode-fix').style.display=m==='fix'?'':'none';
  document.getElementById('mode-review').style.display=m==='review'?'':'none';
  document.getElementById('dtab-fix').className=m==='fix'?'on':'';
  document.getElementById('dtab-review').className=m==='review'?'on':'';
}
async function readFiles(input){
  const out={};
  for(const f of input.files){
    out[f.name]=await f.text();
  }
  return out;
}
function showFiles(input,targetId){
  const el=document.getElementById(targetId);
  if(!input.files.length){el.textContent='';return;}
  const names=[...input.files].map(f=>`${f.name}（${(f.size/1024).toFixed(1)}KB）`);
  el.textContent='已选 '+input.files.length+' 个：'+names.join('、');
}
async function submitIntake(){
  const body={
    title:document.getElementById('f-title').value.trim(),
    report:document.getElementById('f-report').value.trim(),
    expected_behavior:document.getElementById('f-expect').value.trim()};
  if(_dmode==='fix'){
    const src=await readFiles(document.getElementById('f-src'));
    const tst=await readFiles(document.getElementById('f-tst'));
    if(!Object.keys(src).length){alert('先把你的代码文件选上（.py，可多选）。');return;}
    if(!Object.keys(tst).length){alert('至少带一个测试文件（test_*.py）。没有测试的代码我们不收。');return;}
    body.source_files=src;body.test_files=tst;
    const repro=document.getElementById('f-repro').value.trim();
    if(repro)body.repro_snippet=repro;
  }else{
    body.target=document.getElementById('f-target').value;
    const patch=await readFiles(document.getElementById('f-patch'));
    if(Object.keys(patch).length)body.files=patch;
  }
  const r=await j('/api/intake',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  intakeDone(document.getElementById('intakeresult'),r,false);
}

/* ---------------- 对话工作区（多对话 + 聊天 + slash 命令） ---------------- */
let CONV=null, _slashIdx=0, _slashList=[];
function readZipB64(file){
  return new Promise((res,rej)=>{
    const r=new FileReader();
    r.onload=()=>res(String(r.result).split(',')[1]);
    r.onerror=rej;r.readAsDataURL(file);
  });
}
function saveConv(){try{localStorage.setItem('codenotary_conv',JSON.stringify(CONV));}catch(e){}}
function loadConv(){try{CONV=JSON.parse(localStorage.getItem('codenotary_conv')||'null');}catch(e){CONV=null;}}
function freshConv(){return {id:'c'+Date.now().toString(36),title:'新对话',created:Date.now(),
  msgs:[],skills:[],zip:null,zipname:'',files:null,scenario_id:null};}
function getConv(){if(!CONV)CONV=freshConv();return CONV;}

function renderAll(){renderChat();}

function msgHtml(m,i){
  if(m.role==='user')return`<div class="msg user">${esc(m.text)}</div>`;
  if(m.role==='sys')return`<div class="msg sys">${esc(m.text)}</div>`;
  if(m.role==='assistant')return`<div class="msg assistant">${m.html}</div>`;
  if(m.role==='card'){
    return`<div class="msg assistant" style="max-width:96%">
      <div style="font-weight:700;margin-bottom:6px">确认一下，对吗？（第 ${m.ver} 版草稿）</div>
      ${m.skillsHtml||''}
      <label>给这次改动起个名字 *</label><input type="text" id="cd-title-${i}" value="${esc(m.draft.title||'')}">
      <label>问题描述 *</label><textarea id="cd-report-${i}">${esc(m.draft.report||'')}</textarea>
      <label>验收标准（我们会照着验）*</label><textarea id="cd-expect-${i}">${esc(m.draft.expected_behavior||'')}</textarea>
      <div class="note">${m.filesNote}</div>
      ${m.advisory?`<div class="banner amber" style="margin-top:8px"><div class="t" style="font-size:13px">包里没有测试文件</div><div class="d">可以继续——但这次只给修复建议、不发公证书。想拿证：把 test_*.py 加进包里再发。</div></div>`:''}
      <div class="row">
        <button class="btn" onclick="confirmCard(${i},${m.advisory})">确认提交</button>
        <button class="btn ghost" onclick="document.getElementById('c-input').focus()">还想补充？直接在下面说</button>
      </div></div>`;
  }
  if(m.role==='done'){
    return`<div class="msg assistant">✓ 已受理，公证编号 <b class="mono">${esc(m.sid)}</b>。
      离开工还差一步：到 Element 的「Team: codenotary」房间发下面这条消息。
      <pre style="margin-top:8px;max-height:140px">${esc(m.task)}</pre>
      <div class="row"><button class="btn ghost" onclick='navigator.clipboard.writeText(${JSON.stringify(m.task)}).then(()=>alert("已复制"))'>复制任务文案</button>
      <a class="btn" href="#/r/${encodeURIComponent(m.sid)}">去看进度 →</a></div></div>`;
  }
  return'';
}

function renderChat(){
  const c=getConv();const log=document.getElementById('chatlog');
  if(!c.msgs.length){
    log.innerHTML=`<div class="empty">先点 📎 附上代码包（.zip），再说一句你的问题。<br>打 <b>/</b> 有命令：<span class="mono">/skill /example /skeleton /reset /help</span>；/reset 重新开始</div>`;
    return;
  }
  log.innerHTML=c.msgs.map((m,i)=>msgHtml(m,i)).join('');
  log.scrollTop=log.scrollHeight;
}

function histOf(c){
  const h=[];
  for(const m of c.msgs){
    if(m.role==='user')h.push({role:'user',content:m.text});
    else if(m.role==='card')h.push({role:'assistant',content:JSON.stringify(m.draft)});
  }
  if(c.skills.length){
    h.push({role:'user',content:`（起草时请遵循这些技能的要求：${c.skills.join('、')}）`});
  }
  return h;
}

async function attachZip(input){
  const c=getConv();
  const f=input.files[0];if(!f)return;
  c.zip=await readZipB64(f);c.zipname=f.name;
  document.getElementById('c-zipinfo').textContent=`已附上 ${f.name}（${(f.size/1024).toFixed(1)}KB）`;
  saveConv();renderAll();
}

async function sendChat(){
  const c=getConv();
  const inp=document.getElementById('c-input');
  const text=inp.value.trim();
  if(!text)return;
  if(text.startsWith('/')){inp.value='';hideSlash();
    if(text.startsWith('/skill '))handleSkillCmd(text);else handleSlash(text);
    return;}
  inp.value='';hideSlash();
  c.msgs.push({role:'user',text});
  if(c.title==='新对话')c.title=text.slice(0,18);
  saveConv();renderChat();
  const payload={message:text,history:histOf(c)};
  if(c.zip)payload.zip_b64=c.zip;
  else if(c.files)payload.files={...c.files.source_files,...c.files.test_files};
  const r=await j('/api/assist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)});
  if(!r.ok){c.msgs.push({role:'sys',text:'没走通：'+(r.error||'')});saveConv();renderChat();return;}
  c.files={source_files:r.source_files||{},test_files:r.test_files||{}};
  const noTests=(r.warnings||[]).includes('no_tests');
  const d=r.draft||{};
  const srcN=Object.keys(c.files.source_files).length, tstN=Object.keys(c.files.test_files).length;
  const ver=c.msgs.filter(m=>m.role==='card').length+1;
  const skillNote=c.skills.length?`；参考技能：${c.skills.join('、')}`:'';
  c.msgs.push({role:'card',ver,
    draft:{title:d.title||'',report:d.report||text,expected_behavior:d.expected_behavior||''},
    advisory:noTests,
    filesNote:`识别到：源码 ${srcN} 个${tstN?`；测试 ${tstN} 个`:''}${skillNote}${r.llm?'':'（智能整理不可用，这是手动确认卡，自己填也能提交）'}`,
    skillsHtml:c.skills.map(s=>`<span class="skilltag">/skill ${esc(s)}</span>`).join('')});
  saveConv();renderChat();
}

async function confirmCard(i,advisory){
  const c=getConv();
  const body={title:document.getElementById(`cd-title-${i}`).value.trim(),
    report:document.getElementById(`cd-report-${i}`).value.trim(),
    expected_behavior:document.getElementById(`cd-expect-${i}`).value.trim(),
    source_files:c.files.source_files};
  if(Object.keys(c.files.test_files||{}).length)body.test_files=c.files.test_files;
  if(advisory)body.advisory=true;
  const r=await j('/api/intake',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  if(r.ok){
    const sid=r.result.scenario_id;
    const task=`@codenotary-leader 请让你的 Team 公证一条新的变更任务。\n\nscenario_id: ${sid}\n模式：${r.result.mode==='external'?'外部 AI 变更送审（external）':'流水线自研修复（inhouse）'}\n\n请按完整公证流程处理，并输出本次公证报告。`;
    c.msgs[i]={role:'done',sid,task};
    c.scenario_id=sid;
  }else{
    c.msgs.push({role:'sys',text:'没提交上去：'+(r.error||'')});
  }
  saveConv();renderChat();
}

/* ---------------- slash 命令 ---------------- */
const SLASHES=[
  {cmd:'/skill',ds:'装入技能，起草时参考（/skill 名字，打空格有候选）'},
  {cmd:'/example',ds:'给一个完整示例'},
  {cmd:'/skeleton',ds:'验收标准骨架'},
  {cmd:'/reset',ds:'清空当前对话'},
  {cmd:'/help',ds:'命令清单'}];
function onChatInput(el){
  const v=el.value;
  const box=document.getElementById('slashbox');
  _slashIdx=0;
  if(v.startsWith('/')&&!v.includes('\n')){
    const q=v.split(' ')[0].toLowerCase();
    if(v.startsWith('/skill')&&v.length>6){
      const sq=v.slice(6).trim().toLowerCase();
      _slashList=(window._skillnames||[]).filter(n=>n.toLowerCase().includes(sq)).slice(0,6)
        .map(n=>({cmd:'/skill '+n,ds:'装入技能 '+n}));
    }else{
      _slashList=SLASHES.filter(s=>s.cmd.startsWith(q));
    }
    if(_slashList.length){
      box.innerHTML=`<div class="slashmenu" style="bottom:64px;left:0">${_slashList.map((s,i)=>
        `<div class="slashitem ${i===_slashIdx?'sel':''}" onclick="applySlash('${esc(s.cmd)}')"><span class="cmd">${esc(s.cmd)}</span><span class="ds">${esc(s.ds)}</span></div>`).join('')}</div>`;
      return;
    }
  }
  box.innerHTML='';_slashList=[];
}
function hideSlash(){document.getElementById('slashbox').innerHTML='';_slashList=[];}
function onChatKey(e,el){
  if(_slashList.length){
    if(e.key==='ArrowDown'){e.preventDefault();_slashIdx=Math.min(_slashIdx+1,_slashList.length-1);
      const items=document.querySelectorAll('.slashitem');
      items.forEach((it,i)=>it.className='slashitem'+(i===_slashIdx?' sel':''));return;}
    if(e.key==='ArrowUp'){e.preventDefault();_slashIdx=Math.max(_slashIdx-1,0);
      const items=document.querySelectorAll('.slashitem');
      items.forEach((it,i)=>it.className='slashitem'+(i===_slashIdx?' sel':''));return;}
    if(e.key==='Enter'){e.preventDefault();applySlash(_slashList[_slashIdx].cmd);return;}
    if(e.key==='Escape'){hideSlash();return;}
  }
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}
}
function applySlash(cmd){
  document.getElementById('c-input').value='';
  hideSlash();
  if(cmd.startsWith('/skill '))handleSkillCmd(cmd);else handleSlash(cmd);
}
function handleSlash(cmd){
  const c=getConv();
  switch(cmd){
    case '/example':{
      const ex=EXAMPLES.fix;
      c.msgs.push({role:'assistant',html:`给你个完整示例，直接改着用：
        <pre style="max-height:200px;margin-top:6px">名字：${esc(ex.title)}\n\n问题：${esc(ex.report)}\n\n验收标准：\n${esc(ex.expect)}\n\n复现代码：\n${esc(ex.repro)}</pre>
        <div class="note">示例源码 calc.py 和测试 test_calc.py 在「高级模式 → 给我个例子」里有一键复制。</div>`});
      break;}
    case '/skeleton':
      c.msgs.push({role:'assistant',html:'验收标准骨架，改写成你的：<pre style="margin-top:6px">1) 功能：……（要达到什么效果）\n2) 边界：……（空输入、异常情况怎么表现）\n3) 范围：只改 文件名.py，其他文件不动。</pre>'});
      break;
    case '/reset':
      CONV=freshConv();CONV.msgs.push({role:'sys',text:'对话已清空，重新开始'});
      document.getElementById('c-zipinfo').textContent='';
      break;
    case '/help':
      c.msgs.push({role:'assistant',html:SLASHES.map(s=>`<div style="margin-top:3px"><span class="mono" style="color:var(--blue)">${esc(s.cmd)}</span>　${esc(s.ds)}</div>`).join('')});
      break;
    default:
      c.msgs.push({role:'sys',text:`未知命令，/help 看清单`});
  }
  saveConv();renderChat();
}
async function handleSkillCmd(text){
  const c=getConv();
  const name=text.slice(7).trim();
  if(!name){c.msgs.push({role:'sys',text:'用法：/skill 技能名（打 /skill 空格有候选）'});saveConv();renderChat();return;}
  const r=await j('/api/skill_get?name='+encodeURIComponent(name));
  if(!r.ok){
    c.msgs.push({role:'sys',text:`没找到技能「${name}」（打 /skill 空格看候选）`});
  }else{
    if(!c.skills.includes(name))c.skills.push(name);
    c.msgs.push({role:'assistant',html:`<span class="skilltag">/skill ${esc(name)}</span> 已装入。<b>${esc(r.result.description||'')}</b><div class="note" style="margin-top:4px">之后的起草会参考它。这是系统技能库里的一个——流水线干活用的也是同一套。</div>`});
  }
  saveConv();renderChat();
}

function intakeDone(out,r,advisory){
  if(r.ok){
    const sid=r.result.scenario_id;
    const task=`@codenotary-leader 请让你的 Team 公证一条新的变更任务。\n\nscenario_id: ${sid}\n模式：${r.result.mode==='external'?'外部 AI 变更送审（external）':'流水线自研修复（inhouse）'}\n\n请按完整公证流程处理，并输出本次公证报告。`;
    out.innerHTML=`<div class="tip" style="margin-top:14px">✓ 已受理，公证编号 <b class="mono">${esc(sid)}</b>。
      ${advisory?'（建议模式：这次只出修复建议，不发公证书）':''}
      离开工还差一步：到 Element 的「Team: codenotary」房间发下面这条消息。
      <pre style="margin-top:8px;max-height:150px">${esc(task)}</pre>
      <div class="row">
        <button class="btn ghost" onclick='navigator.clipboard.writeText(${JSON.stringify(task)}).then(()=>alert("已复制"))'>复制这条消息</button>
        <a class="btn" href="#/r/${encodeURIComponent(sid)}">去看进度 →</a>
      </div></div>`;
  }else{
    let msg=esc(r.error||'未知错误');
    try{
      const m=String(r.error).match(/(\[\{.*\}\])/s);
      if(m){
        const vs=JSON.parse(m[1]);
        msg='安检没让进门，危险写法在这些地方：'+vs.map(v=>
          `<div style="margin-top:6px">• <b class="mono">${esc(v.file)}</b> 第 ${v.line} 行：${esc(translateFinding(v.label,'').p)}</div>`).join('');
      }
    }catch(e){}
    out.innerHTML=`<div class="reason" style="margin-top:14px"><div class="p">没提交上去</div>
      <div class="w">${msg}</div></div>`;
  }
}

/* ---------------- 进度 / 结果 ---------------- */
function stopPoll(){if(timer){clearInterval(timer);timer=null;}}

async function runPage(sid){
  stopPoll();
  const d=await j('/api/run/'+encodeURIComponent(sid));
  if(d.error){app.innerHTML=`<div class="banner red"><div class="t">没找到这个公证编号</div>
    <div class="d">${esc(sid)}</div></div><div class="row"><a class="btn ghost" href="#/">← 返回大厅</a></div>`;return;}
  const state=d.state;
  let[t1,t2]=STATE_TEXT[state]||[state,''];
  if(state==='RELEASED'&&d.advisory){
    t1='修复建议已经出来了';t2='这次没有公证书——原因和补救办法在下面。';}
  const[cls,ctxt]=chipOf(state,d.advisory);
  const bannerCls=cls==='pass'?'green':cls==='fail'?'red':cls==='wait'?'amber':'';
  const cur=stageOf(state,d.history);
  const steps=STEPS.map((s,i)=>{
    const c=i<cur?'done':i===cur?(TERMINAL.includes(state)?'done':'cur'):'';
    return`<div class="step ${c}"><div class="n">${i<cur||(TERMINAL.includes(state)&&i===cur)?'✓ ':''}${s[0]}</div><div class="s">${s[1]}</div></div>`;}).join('');

  let mid='';
  if(state==='ESCALATED'){
    mid=`<div class="banner amber" style="margin-top:14px"><div class="t">需要你拿主意</div>
      <div class="d">进行到一半，系统发现有地方两种理解都说得通——多半是最初的验收标准有歧义。
      它不会替你猜，所以停下来等你。看完给个话。</div>
      <div class="row">
        <button class="btn" onclick="doResolve('${esc(sid)}',true)">按我的意思继续</button>
        <button class="btn danger" onclick="doResolve('${esc(sid)}',false)">不行，退回去</button>
      </div></div>`;
  }

  /* 三道体检小卡 */
  const v=d.verdicts||{};
  const g=(key,name)=>{
    const x=v[key];
    if(!x)return`<div class="g3 none"><div class="t">${name}</div><div class="d">—</div></div>`;
    const c=x.decision==='green'?'green':x.decision==='red'?'red':'none';
    const t=x.decision==='green'?'通过':x.decision==='red'?'没通过':x.decision;
    return`<div class="g3 ${c}"><div class="t">${name}</div><div class="d">${t}</div></div>`;};
  const gates=`<div class="gate3">${g('test_pass','功能测试')}${g('mutation','变异测试')}${g('convention','行为检查')}</div>`;
  const skillN=(d.trace_tail||[]).filter(e=>e.tool&&e.tool.startsWith('notary_skill')).length;
  const skillNote=skillN?`<div class="note" style="margin-top:8px">🔧 本次公证参考了技能库（${skillN} 次调用记录，明细在工程后台「技能调用记录」面板）</div>`:'';

  let result='';
  const adv=d.advisory;
  if((state==='RELEASED'||state==='NOTARIZED')&&adv){
    result=`<div class="panel"><h2>为什么这次没有公证书</h2>
      <div class="note">你的包里没有测试文件。没有测试的"修好了"只是一句话——所以这次我们给的是
      <b>修复建议</b>，不是公证。三道体检照样跑了（结果在上面），建议本身在下面。
      想要公证书：把 test_*.py 加进包里重新提交一次，全流程会再走一遍，这次带证。</div></div>
      <div class="panel"><h2>修复建议（变更预览）</h2>
      <div class="note">流水线在你的代码上写出的修改（逐文件）</div>
      <div id="chg">读取中…</div>
      <div class="row"><button class="btn" onclick="reapply('${esc(sid)}')">补上测试，重新送审</button>
      <a class="btn ghost" href="#/">返回大厅</a></div></div>`;
    loadChange(sid);
  }else if(state==='RELEASED'||state==='NOTARIZED'){
    result=`<div class="panel"><h2>变更预览</h2>
      <div class="note">流水线写的修复（逐文件）</div>
      <div id="chg">读取中…</div></div>
      <div class="panel"><h2>公证书</h2>
      <div class="note">这份公证书在签发时一并封存。点「验一验」，浏览器会重新算一遍指纹，
      和封存的值对得上，就是原件。</div>
      <pre id="cert">读取中…</pre>
      <div class="row">
        <button class="btn ghost" onclick="verifyCert('${esc(sid)}')">验一验</button>
        <button class="btn ghost" onclick="dlCert('${esc(sid)}')">下载公证书</button>
        <a class="btn ghost" href="#/">返回大厅</a>
      </div><div id="verifyout" class="note" style="margin-top:8px"></div></div>`;
    loadCert(sid);
    loadChange(sid);
  }else if(state==='REJECTED'||state==='QUARANTINED'){
    const reasonsHtml=await buildReasons(sid,d,state);
    result=`${reasonsHtml}
      <div class="panel"><h2>怎么办</h2>
      <ol class="howto">
        <li>对照上面的原因改代码。</li>
        <li>改完重新提交一次公证——点下面的按钮，刚才填的内容给你留着。</li>
        <li>觉得判得有争议？到「Team: codenotary」房间找平台团队。所有证据都封存在案，可以当面对质。</li>
      </ol>
      <div class="row"><button class="btn" onclick="reapply('${esc(sid)}')">改好了，重新送审</button>
      <a class="btn ghost" href="#/">返回大厅</a></div></div>`;
  }else if(state==='ROLLED_BACK'){
    result=`<div class="panel"><h2>这次公证已撤销</h2>
      <div class="note">上线后发现不对，发布被撤回，交付物已卸除。全部证据保留，可在工程后台查阅。</div></div>`;
  }

  app.innerHTML=`
  <div class="row" style="margin-top:14px"><a class="btn ghost" href="#/">← 返回大厅</a>
    <span class="pill mono" style="align-self:center">公证编号 ${esc(sid)}</span></div>
  <div class="banner ${bannerCls}"><div class="t">${esc(t1)}</div><div class="d">${esc(t2)}</div></div>
  <div class="steps">${steps}</div>
  ${mid}${gates}${skillNote}${result}`;

  if(!TERMINAL.includes(state)){
    timer=setInterval(()=>{if(location.hash.startsWith('#/r/'))runPageKeep(sid);},3000);
  }
}
/* 轮询时保留滚动位置（不整页重绘输入区） */
async function runPageKeep(sid){
  const y=window.scrollY;await runPage(sid);window.scrollTo(0,y);
}

async function doResolve(sid,approve){
  const r=await j('/api/resolve/'+encodeURIComponent(sid),{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({approve})});
  if(!r.ok)alert('没记上去：'+(r.error||''));
  runPageKeep(sid);
}

async function loadCert(sid){
  const d=await j('/api/file/'+encodeURIComponent(sid)+'/certificate.md');
  document.getElementById('cert').textContent=d.content||'(公证书还没生成，稍等片刻)';
  window._certRaw=d;
}
async function loadChange(sid){
  const r=await j('/api/change/'+encodeURIComponent(sid));
  const el=document.getElementById('chg');
  const names=Object.keys(r.files||{});
  if(!names.length){el.innerHTML='<div class="note">（没有可展示的变更文件）</div>';return;}
  el.innerHTML=names.map((n,idx)=>{
    const cid=`chgfile-${idx}`;
    return`<div class="note mono" style="margin-top:10px;display:flex;align-items:center;gap:8px">
      <span>── ${esc(n)}</span>
      <a href="javascript:void 0" style="font-size:11px" onclick="copyFileContent('${cid}','${esc(n)}')">复制</a>
      <a href="javascript:void 0" style="font-size:11px" onclick="dlFileContent('${cid}','${esc(n)}')">下载</a>
    </div><pre id="${cid}" style="max-height:300px">${esc(r.files[n])}</pre>`;
  }).join('');
}
function copyFileContent(preId,name){
  const t=document.getElementById(preId).textContent;
  navigator.clipboard.writeText(t).then(()=>alert(`已复制 ${name} 的全部内容`));
}
function dlFileContent(preId,name){
  const t=document.getElementById(preId).textContent;
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([t],{type:'text/x-python'}));
  a.download=name;a.click();URL.revokeObjectURL(a.href);
}
async function verifyCert(sid){
  const out=document.getElementById('verifyout');
  const d=window._certRaw||await j('/api/file/'+encodeURIComponent(sid)+'/certificate.md');
  if(!d.content){out.textContent='公证书还没生成，等一下再验。';return;}
  const buf=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(d.content));
  const hex=[...new Uint8Array(buf)].map(b=>b.toString(16).padStart(2,'0')).join('');
  if(d.declared_sha256&&hex===d.declared_sha256){
    out.innerHTML=`✓ 对上了。指纹 <span class="mono">${hex.slice(0,16)}…</span> 与封存值一致——这份公证书没人动过。`;
  }else if(d.declared_sha256){
    out.innerHTML=`✗ 对不上：浏览器算出的指纹与封存值不一致。请找平台团队核查。`;
  }else{
    out.innerHTML=`本次计算的指纹：<span class="mono">${hex.slice(0,16)}…</span>（这条记录没有封存清单可比，可能还没出完证）`;
  }
}
function dlCert(sid){
  const d=window._certRaw;if(!d||!d.content)return;
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([d.content],{type:'text/markdown'}));
  a.download=`公证书-${sid}.md`;a.click();URL.revokeObjectURL(a.href);
}

async function buildReasons(sid,d,state){
  const items=[];
  if(state==='QUARANTINED'){
    const m=await j('/api/file/'+encodeURIComponent(sid)+'/evidence/quarantine/manifest.json');
    let findings=[];
    try{findings=JSON.parse(m.content||'{}').findings||[];}catch(e){}
    if(!findings.length){items.push({p:'安检发现了危险写法。',w:'具体明细读取失败，请到工程后台查看。'});}
    findings.forEach(f=>{const t=translateFinding(f.label,'');
      items.push({...t,where:`${f.file||''}${f.line?' 第 '+f.line+' 行':''}`});});
  }else{
    const v=d.verdicts||{};
    const tp=v.test_pass;
    if(tp&&tp.decision==='red')items.push({
      p:'功能测试没通过。',
      w:(tp.summary||'实现和验收标准对不上。')+'——对照测试失败的明细改。'});
    const mu=v.mutation;
    if(mu&&mu.decision==='red')items.push({
      p:`有 ${mu.survived_after_rebuttal??'?'} 处改动，测试发现不了。`,
      w:'我们把代码故意改坏几处，测试照样全绿——说明这几处测试盖不住。补测试，或者写清楚为什么盖不住也能接受。'});
    const cv=v.convention;
    if(cv&&cv.decision==='red')(cv.findings||[]).forEach(f=>{
      const t=translateFinding(f.detail,f.rule);
      items.push({...t,where:`${f.file||''}${f.line?' 第 '+f.line+' 行':''}`});});
    if(!items.length)items.push({p:'综合评审没通过。',
      w:'明细在工程后台都能查到（ verdicts 目录），或者到房间问平台团队。'});
  }
  return `<div class="panel"><h2>没通过的原因</h2>`+items.map(it=>`
    <div class="reason"><div class="p">${esc(it.p)}${it.where?` <span class="note mono">${esc(it.where)}</span>`:''}</div>
    <div class="w">${esc(it.w)}</div></div>`).join('')+`</div>`;
}

async function reapply(sid){
  let prefill=null;
  try{
    const f=await j('/api/file/'+encodeURIComponent(sid)+'/issue.json');
    const issue=JSON.parse(f.content||'{}');
    prefill={title:issue.title||'',report:issue.report||'',
      expected_behavior:issue.expected_behavior||''};
  }catch(e){}
  location.hash='#/';
  await hall(prefill);
  document.getElementById('dform').scrollIntoView({behavior:'smooth'});
}

/* ---------------- 启动 ---------------- */
async function boot(){
  try{const g=await j('/api/gw');const p=document.getElementById('gw');
    if(g.ok){p.textContent='服务正常 ●';p.className='pill on';}else{p.textContent='服务未就绪';}
  }catch(e){document.getElementById('gw').textContent='服务未就绪';}
  loadConv();
  if(!CONV)CONV=freshConv();
  try{const sk=await j('/api/skills');
    window._skillnames=(sk.skills||[]).map(s=>s.name);}catch(e){window._skillnames=[];}
  route();
}
function route(){
  const h=location.hash;
  const m=h.match(/^#\/r\/(.+)$/);
  if(m)runPage(decodeURIComponent(m[1]));else hall();
}
window.addEventListener('hashchange',route);
boot();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# 任务工作台（幕 1/幕 3 载体）：看板 + 裁决卡 + merge 确认卡，同一界面内嵌。
# 文案基调 = 公证处窗口工作人员；事实/结论分层；LLM 只在别处起草措辞，
# 这页上的每个字都来自落盘证据。
# ---------------------------------------------------------------------------
WORKBENCH_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"><title>任务工作台 · CodeNotary</title>
<style>
:root{--ink:#1a2332;--sub:#5b6b7f;--line:#d9e0e8;--bg:#f5f7fa;--card:#fff;
--red:#c0392b;--green:#1e8449;--amber:#b9770e;--blue:#2166ac}
*{box-sizing:border-box;margin:0}
body{font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);padding:16px}
header{display:flex;align-items:center;gap:16px;margin-bottom:14px}
h1{font-size:18px}
.token{margin-left:auto;font-size:12px;color:var(--sub)}
.token input{width:260px;padding:4px 8px;border:1px solid var(--line);
border-radius:6px}
.board{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.col{background:#eef1f5;border-radius:10px;padding:10px;min-height:200px}
.col h2{font-size:13px;color:var(--sub);margin-bottom:8px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px;margin-bottom:8px;cursor:pointer}
.card:hover{box-shadow:0 2px 8px rgba(26,35,50,.12)}
.card.old{opacity:.68;border-style:dashed;background:#fafbfc}
.card.rel{border-left:3px solid var(--blue)}
.topnav{display:flex;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin-bottom:16px;box-shadow:0 1px 3px rgba(26,35,50,.06)}
.topnav .brand{font-weight:700;font-size:14px;margin-right:8px}
.topnav a{padding:5px 14px;border-radius:8px;text-decoration:none;color:#1a2332;font-size:13px}
.topnav a:hover{background:#eef3fb}
.card .t{font-weight:600;font-size:13px;margin-bottom:4px}
.card .m{font-size:12px;color:var(--sub)}
.card .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
background:var(--red);margin-right:4px}
.card .wait{color:var(--red);font-size:12px}
.badge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;
background:#eef1f5;color:var(--sub)}
.overlay{position:fixed;inset:0;background:rgba(26,35,50,.45);
display:flex;align-items:center;justify-content:center;z-index:10}
.sheet{background:var(--card);border-radius:12px;width:min(720px,94vw);
max-height:88vh;overflow:auto;padding:22px}
.sheet h3{font-size:16px;margin-bottom:4px}
.sheet .principle{background:#fdf6e3;border-left:3px solid var(--amber);
padding:8px 12px;border-radius:0 6px 6px 0;margin:10px 0;font-size:13px}
.layer{border:1px solid var(--line);border-radius:8px;padding:10px 12px;
margin:8px 0;font-size:13px}
.layer>b{display:block;color:var(--sub);font-size:12px;margin-bottom:2px}
.opt{display:flex;gap:8px;align-items:flex-start;border:1px solid var(--line);
border-radius:8px;padding:8px 10px;margin:6px 0;cursor:pointer}
.opt:hover{border-color:var(--blue)}
.opt input{margin-top:4px}
.opt .hint{color:var(--sub);font-size:12px}
.evi{font-size:13px;max-height:130px;overflow:auto;border:1px solid
var(--line);border-radius:8px;padding:8px 10px}
.evi label{display:block}
textarea{width:100%;border:1px solid var(--line);border-radius:8px;
padding:8px;font:inherit;font-size:13px}
button{padding:8px 18px;border:0;border-radius:8px;background:var(--blue);
color:#fff;font-size:14px;cursor:pointer}
button:disabled{background:#aab4c0;cursor:not-allowed}
.row{display:flex;gap:10px;align-items:center;margin-top:12px}
.mut{color:var(--sub);font-size:12px}
pre{background:#0d1420;color:#d5e3f0;padding:12px;border-radius:8px;
font-size:12px;overflow:auto}
.flash{padding:8px 12px;border-radius:8px;margin-top:10px;font-size:13px}
.flash.ok{background:#e8f6ee;color:var(--green)}
.flash.err{background:#fdecea;color:var(--red)}
.chatlog{border:1px solid var(--line);border-radius:8px;padding:8px;
max-height:120px;overflow:auto;background:#fbfcfe;font-size:13px;
white-space:pre-wrap}
.chatlog .u{color:var(--blue)}
</style>
</head>
<body>
<div class="topnav"><span class="brand">⚖️ CodeNotary 公证处</span><a href="/workbench">任务看板</a><a href="/hall">办事大厅</a><a href="/skillboard">Skill 看板</a></div>
<header>
  <h1>任务工作台</h1><span class="mut">CodeNotary 公证处 · 内勤台</span>
  <span class="token">签署令牌 <input id="tok" type="password"
    placeholder="裁决人令牌"></span>
</header>
<div class="board" id="board"></div>
<div id="overlay"></div>

<script>
let BOARD = null;
const COLS = [["inflight","进行中"],["escalated","⏸ 待人工裁决"],
  ["rejected","未放行"],["notarized","已公证"],["released","已发布"]];
const esc = s => String(s??"").replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function load(){
  const r = await fetch("/api/board"); BOARD = await r.json(); render();
}
function render(){
  const el = document.getElementById("board");
  el.innerHTML = COLS.map(([key,label])=>{
    const cards = BOARD.columns[key]||[];
    return `<div class="col"><h2>${label}（${cards.length}）</h2>` +
      cards.map(c=>{
        const wait = c.waiting_s!=null ?
          `<div class="wait"><span class="dot"></span>等待人工裁决 · ` +
          `${Math.floor(c.waiting_s/60)} 分钟</div>` : "";
        const focus = c.dispute_focus ?
          `<div class="m">争议：${esc(c.dispute_focus.slice(0,40))}…</div>`:"";
        const cv = c.contract_version ?
          `<span class="badge">契约 v${c.contract_version}</span>` : "";
        const clickable = `onclick="location.href='/run?sid=${esc(c.run_id)}'"`;
        const vcls = (c.version_count>1 && !c.is_latest ? " old" : "") +
          (key==="released" ? " rel" : "");
        const ver = c.version_count>1 ?
          `<div class="m">同一工单第 ${c.version_index}/${c.version_count} 版` +
          (c.is_latest ? "（最新）" :
            ` · 最新版 ${esc(c.latest_state_label||"")} →`) + `</div>` : "";
        const quick = key==="escalated" ?
          `<button class="ghost" style="margin-top:6px;padding:3px 10px;font-size:12px"
            onclick="event.stopPropagation();openCard('${esc(c.run_id)}','escalated')">⚖️ 裁决</button>`
        : key==="notarized" ?
          `<button class="ghost" style="margin-top:6px;padding:3px 10px;font-size:12px"
            onclick="event.stopPropagation();openCard('${esc(c.run_id)}','notarized')">合并就绪</button>` : "";
        return `<div class="card${vcls}" ${clickable}>
          <div class="t">${esc(c.title)}</div>
          <div class="m">${esc(c.state_label||"")}${c.gates_progress?
            " · "+c.gates_progress:""}</div>${wait}${focus}${ver}
          <div class="m">${esc(c.run_id)} ${cv}</div>${quick}</div>`;
      }).join("") + `</div>`;
  }).join("");
}

async function openCard(sid, kind){
  const ov = document.getElementById("overlay");
  if(kind==="escalated"){
    const ctx = await (await fetch("/api/adjudication_context/"+sid)).json();
    ov.innerHTML = adjudCard(sid, ctx);
  }else{
    const mc = await (await fetch("/api/merge_card/"+sid)).json();
    ov.innerHTML = mergeCard(sid, mc);
  }
}
function closeOverlay(){ document.getElementById("overlay").innerHTML=""; }

function adjudCard(sid, ctx){
  const d = ctx.dispute || {};
  const c = ctx.contract || {};
  const v = ctx.verdicts || {};
  const failed = Object.entries(v).filter(([,x])=>x.decision==="red")
    .map(([g,x])=>`<div>· <b>${esc(x.gate||g)}</b>：${esc(x.summary||"")}</div>`)
    .join("") || "<div>· 无红色门禁（争议由分诊升级）</div>";
  const opts = ctx.options.map(o=>`
    <label class="opt"><input type="radio" name="dec" value="${o.key}"
      onchange="onDecChange()">
    <span><b>${o.label}</b><div class="hint">${o.hint}</div></span></label>`)
    .join("");
  const evi = ctx.evidence_candidates.map(e=>
    `<label><input type="checkbox" class="evi" value="${esc(e)}"> ${esc(e)}</label>`).join("");
  const assertions = (c.assertions||[]).map((a,i)=>
    `<div>· 第 ${i+1} 条：${esc(a)}</div>`).join("");
  // 假设前置透明：冻结时系统已标注的歧义与默认解读
  const assump = (c.assumptions||[]).map(a=>
    `<div>· <b>歧义点</b>：${esc(a.point)}<br>
    　<b>默认解读</b>：${esc(a.assumption)}<br>
    　<b>依据</b>：${esc(a.basis)}</div>`).join("")
    || "<div>· 无（本契约未标注假设）</div>";
  window._v1Assertions = (c.assertions||[]);
  return `<div class="overlay" onclick="if(event.target===this)closeOverlay()">
  <div class="sheet">
    <h3>裁决卡 · ${esc(sid)}</h3>
    <div class="principle">请先确认你已了解双方立场与检验证据。
      你的裁决对象是<b>规则</b>，不是代码——代码将由系统按你发布的新规则
      重新完整检验。</div>
    <div class="layer"><b>双方怎么说</b>
      <div>📋 规则 v${c.version??"—"}（哈希 ${esc((c.frozen_hash||"").slice(0,16))}…）：</div>
      ${assertions}
      <div style="margin-top:6px"><b>冻结时系统已标注的假设：</b>${assump}</div>
      <div style="margin-top:6px">🙋 送审方：${esc(d.focus||"—")}
        ${d.clause?`（涉及条款：${esc(d.clause)}）`:""}
        <span class="mut">${d.ts?new Date(d.ts*1000).toLocaleString():""}</span></div>
    </div>
    <div class="layer"><b>客观事实（检验证据，与争议点分开看）</b>${failed}</div>
    <div class="layer"><b>💬 追问（仅补充信息，不产生任何状态变更）</b>
      <div class="chatlog" id="adjChat" style="max-height:120px"></div>
      <div class="row">
        <input type="text" id="adjQ" style="flex:1"
          placeholder="例：下游渠道对'有效期'的约定原文在哪？">
        <button class="ghost" onclick="adjAsk('${esc(sid)}')">问</button>
      </div></div>
    <div class="layer"><b>四选一裁决</b>${opts}</div>
    <div class="layer" id="reviseBox" style="display:none"><b>修订文本
      （系统按当前契约起草，请亲手修改；每行一条断言）</b>
      <textarea id="revisionText" rows="5">${esc((c.assertions||[]).join("\n"))}</textarea>
      <div class="mut">v1.0 将封存保留，永不覆盖。</div>
      <button class="ghost" onclick="showDiff()">查看 v1.0 ↔ v1.1 对照</button>
      <div id="diffBox" style="margin-top:6px"></div>
    </div>
    <div class="layer"><b>引用证据（勾选将进入裁决记录）</b>
      <div class="evi">${evi}</div></div>
    <div class="layer"><b>补证附件（每行一条；修订契约时将写入新版本契约）</b>
      <textarea id="refs" rows="2" placeholder="例：《渠道对账协议 v2.3》第 4 条"></textarea></div>
    <div class="layer"><b>裁决理由（必填，将永久写入证据包；特批放行不少于 20 字）</b>
      <textarea id="rationale" rows="2"></textarea></div>
    <div class="row">
      <button id="signBtn" disabled onclick="confirmAdjudicate('${esc(sid)}')">
        ✍️ 确认并签署</button>
      <span class="mut">签署即留痕：签署人、通道、引用材料全部进证据包。</span>
    </div>
    <div id="flash"></div>
  </div></div>`;
}

function onDecChange(){
  const dec=document.querySelector("input[name=dec]:checked");
  const box=document.getElementById("reviseBox");
  if(box) box.style.display = (dec && dec.value==="revise") ? "block":"none";
  const btn=document.getElementById("signBtn");
  if(btn) btn.disabled = !(dec &&
    document.getElementById("rationale").value.trim().length>=8);
}

function showDiff(){
  const oldL = window._v1Assertions||[];
  const newL = document.getElementById("revisionText").value.split("\n")
    .map(s=>s.trim()).filter(Boolean);
  const rows=[];
  const n=Math.max(oldL.length,newL.length);
  for(let i=0;i<n;i++){
    const o=oldL[i], w=newL[i];
    if(o===undefined) rows.push(`<div style="color:var(--green)">＋ ${esc(w)}</div>`);
    else if(w===undefined) rows.push(`<div style="color:var(--red)">－ ${esc(o)}</div>`);
    else if(o!==w) rows.push(
      `<div style="color:var(--sub)">v1.0：${esc(o)}</div>
       <div style="color:var(--green)">v1.1：${esc(w)}</div>`);
    else rows.push(`<div class="mut">　${esc(o)}</div>`);
  }
  document.getElementById("diffBox").innerHTML =
    `<div style="border:1px solid var(--line);border-radius:8px;
      padding:8px;font-size:12px">${rows.join("")}</div>`;
}

async function adjAsk(sid){
  const q=document.getElementById("adjQ").value.trim(); if(!q)return;
  document.getElementById("adjQ").value="";
  const log=document.getElementById("adjChat");
  log.innerHTML+=`<div><span class="u">你：</span>${esc(q)}</div>`;
  const r=await (await fetch("/api/hall_chat",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({sid:sid,message:q,history:[]})})).json();
  log.innerHTML+=`<div><span>窗口：</span>${esc(r.reply||r.error||"")}</div>`;
  log.scrollTop=log.scrollHeight;
}
document.addEventListener("change", e=>{
  if(e.target.name==="dec" || e.target.id==="rationale"){
    const btn=document.getElementById("signBtn");
    if(btn) btn.disabled = !(
      document.querySelector("input[name=dec]:checked") &&
      document.getElementById("rationale").value.trim().length>=8);
  }});
document.addEventListener("input", e=>{
  if(e.target.id==="rationale"){
    const btn=document.getElementById("signBtn");
    if(btn) btn.disabled = !(
      document.querySelector("input[name=dec]:checked") &&
      e.target.value.trim().length>=8);
  }});

function tokenSub(){
  // 仅作回显：验签在服务端；这里只把 JWT 中段 claims 读出来给人看
  try{
    const t=document.getElementById("tok").value.split(".")[1];
    return JSON.parse(atob(t.replace(/-/g,"+").replace(/_/g,"/"))).sub||"未知";
  }catch(e){ return "未填令牌（将被拒）"; }
}

function confirmAdjudicate(sid){
  const dec=document.querySelector("input[name=dec]:checked").value;
  const rationale=document.getElementById("rationale").value.trim();
  const refs=document.getElementById("refs").value.split("\n")
    .map(s=>s.trim()).filter(Boolean);
  const evi=[...document.querySelectorAll(".evi:checked")].map(x=>x.value);
  const LABEL={uphold:"维持契约",revise:"修订契约",
    request_evidence:"要求补充证据",override:"特批放行（标黄）"}[dec];
  let revision=null;
  if(dec==="revise"){
    revision=document.getElementById("revisionText").value.split("\n")
      .map(s=>s.trim()).filter(Boolean);
    if(!revision.length){alert("修订文本不能为空");return;}
  }
  const echo=`你即将签署：${LABEL}\n\n`+
    `签署人：${tokenSub()} ｜ 通道：工作台裁决卡 ｜ 引用材料：${evi.length} 项`+
    ` ｜ 补证附件：${refs.length} 条\n`+
    (revision?`契约 v1.0 → v1.1（v1.0 全文封存可查）\n`:"")+
    `\n理由：${rationale}\n\n此操作永久写入证据包，不可撤销。`;
  if(!confirm(echo)) return;
  const body={decision:dec,rationale:rationale,
    evidence_reviewed:evi,references:refs};
  if(revision) body.revision_assertions=revision;
  fetch("/api/adjudicate/"+sid,{method:"POST",
    headers:{"Content-Type":"application/json",
      "X-Console-Token":document.getElementById("tok").value},
    body:JSON.stringify(body)})
  .then(r=>r.json()).then(b=>{
    const f=document.getElementById("flash");
    if(b.ok!==false && b.result){
      const rev=b.result.contract_revision;
      const eviList=evi.map(e=>`✓ ${e}`).join("　")||"（未勾选）";
      f.className="flash ok";
      f.innerHTML=`🟢 <b>裁决已生效，辛苦了</b><br>`+
        `系统已自动完成：① 裁决记录（#${b.result.adjudication_id}）进入证据包`+
        `② 流水线状态：${b.result.pipeline_state}`+
        (rev&&rev.frozen_hash?`③ 契约 v1.1 已发布（哈希 ${rev.frozen_hash.slice(0,12)}…，v1.0 封存可查）`:"③ 待后续动作接续")+
        `<br><span class="mut">备查——你裁决前查看过的材料：${esc(eviList)}</span>`;
      setTimeout(()=>{closeOverlay();load();},3000);
    }else{
      f.className="flash err";
      f.textContent="被拒：" + (b.error||JSON.stringify(b));
    }});
}

function mergeCard(sid, mc){
  let body;
  if(!mc.available){
    body = `<div class="layer">${esc(mc.reason||"merge 检查不可用")}</div>`;
  }else{
    const c = mc.card;
    const rows = (c.checks||[]).map((k,i)=>
      `<div class="opt" style="cursor:default">
        <span>${k.ok?"✅":"❌"}</span>
        <span><b>${esc(k.name)}</b>
        <div class="hint">${esc(k.detail)}</div></span></div>`).join("");
    const icon = c.ready ? "✅" : (c.icon||"⛔");
    body = `<div class="layer"><b>合并就绪检查 · ${c.checks.filter(k=>k.ok).length}/${c.checks.length} 通过</b>
      ${rows}</div>
      <div class="layer"><b>结论</b>${icon} ${esc(c.verdict)}
      <div class="hint mut">这张 PR 的完整过程（含争议与规则修订）都在证据链里。</div></div>`;
  }
  return `<div class="overlay" onclick="if(event.target===this)closeOverlay()">
  <div class="sheet">
    <h3>合并确认卡 · ${esc(sid)}</h3>
    <div class="principle">验收 ≠ 合并 ≠ 发布。系统只给就绪意见；
      <b>点合并是负责人的权力，不是系统的</b>。</div>
    ${body}
    <div class="row"><span class="mut">
      就绪后请到 GitHub PR 页执行合并——系统的权力止于就绪意见，合并动作只属于人。</span></div>
  </div></div>`;
}

load().then(()=>{
  const h = location.hash.match(/card=([\w-]+)/);
  if(h) openCard(h[1], (location.hash.match(/kind=(\w+)/)||[])[1]||"escalated");
});
setInterval(load, 2500);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 办事大厅（团队版提交页 + 四类信封 + 会话追问）。
# 铁律：聊天用于讨论与起草，卡片用于正式生效——聊天里说的任何话
# 永不直接驱动状态机。文案 = 公证处窗口工作人员，不是系统日志。
# ---------------------------------------------------------------------------
HALL_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"><title>办事大厅 · CodeNotary</title>
<style>
:root{--ink:#1a2332;--sub:#5b6b7f;--line:#d9e0e8;--bg:#f5f7fa;--card:#fff;
--red:#c0392b;--green:#1e8449;--amber:#b9770e;--blue:#2166ac}
*{box-sizing:border-box;margin:0}
body{font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);padding:16px}
h1{font-size:18px}
.wrap{display:grid;grid-template-columns:minmax(340px,1fr) 2fr;gap:14px;
margin-top:12px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px}
.panel h2{font-size:15px;margin-bottom:10px}
.hintbar{background:#eef3fb;border-left:3px solid var(--blue);padding:6px 10px;
border-radius:0 6px 6px 0;font-size:12px;color:var(--sub);margin:8px 0}
textarea,input[type=text]{width:100%;border:1px solid var(--line);
border-radius:8px;padding:8px;font:inherit;font-size:13px}
button{padding:7px 16px;border:0;border-radius:8px;background:var(--blue);
color:#fff;cursor:pointer}
button.ghost{background:#eef1f5;color:var(--ink)}
button:disabled{background:#aab4c0}
.task{border:1px solid var(--line);border-radius:8px;padding:8px 10px;
margin:6px 0;cursor:pointer;font-size:13px}
.task:hover{border-color:var(--blue)}
.task .mut{color:var(--sub);font-size:12px}
.env{border-radius:10px;padding:10px 12px;margin:8px 0;border:1px solid
var(--line);background:var(--card)}
.env .tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:4px;
color:#fff;margin-right:6px}
.env.todo{border-color:var(--red)} .env.todo .tag{background:var(--red)}
.env.progress .tag{background:var(--blue)}
.env.receipt{border-color:var(--green)} .env.receipt .tag{background:var(--green)}
.env.evidence .tag{background:var(--amber)}
.env .t{font-weight:600;margin-bottom:4px}
.env .five{font-size:13px;color:var(--ink)}
.env .five div{margin:2px 0}
.env .five b{color:var(--sub);font-weight:600;font-size:12px}
.chatlog{border:1px solid var(--line);border-radius:8px;padding:10px;
min-height:120px;max-height:260px;overflow:auto;background:#fbfcfe;
font-size:13px;white-space:pre-wrap}
.chatlog .u{color:var(--blue)}
.mut{color:var(--sub);font-size:12px}
#taskPanel{position:sticky;top:12px;align-self:start;display:flex;
flex-direction:column;max-height:calc(100vh - 24px)}
#taskPanel #envs{flex:1;overflow:auto;min-height:0}
#taskPanel #chatArea{flex:none}
.topnav{display:flex;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin-bottom:16px;box-shadow:0 1px 3px rgba(26,35,50,.06)}
.topnav .brand{font-weight:700;font-size:14px;margin-right:8px}
.topnav a{padding:5px 14px;border-radius:8px;text-decoration:none;color:#1a2332;font-size:13px}
.topnav a:hover{background:#eef3fb}
.row{display:flex;gap:8px;margin-top:8px;align-items:center}
.draft{border:1px dashed var(--amber);border-radius:8px;padding:10px;
margin-top:10px;font-size:13px}
.draft input,.draft textarea{margin:4px 0}
</style>
</head>
<body>
<div class="topnav"><span class="brand">⚖️ CodeNotary 公证处</span><a href="/workbench">任务看板</a><a href="/hall">办事大厅</a><a href="/skillboard">Skill 看板</a></div>
<h1>办事大厅</h1>
<div class="wrap">
  <div class="panel">
    <h2>送审 / 求修</h2>
    <div class="hintbar">一句话描述问题，可附 ZIP（有代码要审）；
      接待员帮您起草公证申请，<b>您确认后才取号</b>。</div>
    <textarea id="ask" rows="3"
      placeholder="例：优惠券有效至 11 月 10 日，但没到期就核销不了"></textarea>
    <div class="row">
      <input type="file" id="zip" accept=".zip" style="font-size:12px">
      <button onclick="draftIt()">请接待员起草</button>
    </div>
    <div id="draftBox"></div>
    <h2 style="margin-top:18px">我的任务</h2>
    <div id="tasks"></div>
  </div>
  <div class="panel" id="taskPanel">
    <h2 id="taskTitle">选择一个任务查看进展</h2>
    <div id="envs"></div>
    <div id="chatArea" style="display:none">
      <h2 style="margin-top:14px">追问</h2>
      <div class="hintbar">这里只帮您查落盘记录、摆事实；
        <b>不产生任何状态变更</b>。正式动作请走卡片。</div>
      <div class="chatlog" id="chatlog"></div>
      <div class="row">
        <input type="text" id="q" placeholder="例：失败的是哪个场景？契约哪一条？">
        <button onclick="askFollow()">问</button>
      </div>
    </div>
  </div>
</div>

<script>
const esc = s => String(s??"").replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let CUR = null, HISTORY = [];

async function zipB64(){
  const f = document.getElementById("zip").files[0];
  if(!f) return null;
  const buf = await f.arrayBuffer();
  let bin=""; new Uint8Array(buf).forEach(b=>bin+=String.fromCharCode(b));
  return btoa(bin);
}
async function draftIt(){
  const message = document.getElementById("ask").value.trim();
  if(!message){alert("先写一句话描述问题");return;}
  const body = {message, history:[]};
  const z = await zipB64(); if(z) body.zip_b64 = z;
  const r = await (await fetch("/api/assist",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}))
    .json();
  if(r.ok===false){alert(r.error);return;}
  const d = r.draft||{};
  document.getElementById("draftBox").innerHTML = `
    <div class="draft">
      <b>公证申请草稿</b>（请检查，确认才取号）
      ${r.llm===false?'<div class="mut">接待员暂不可用，以下为预填草稿</div>':""}
      <input type="text" id="dTitle" value="${esc(d.title||"")}"
        placeholder="一句话名称">
      <textarea id="dReport" rows="3"
        placeholder="问题描述（现象/背景/影响）">${esc(d.report||message)}</textarea>
      <textarea id="dExpect" rows="3"
        placeholder="验收标准：怎样算修好（可检查的判断句）">${esc(d.expected_behavior||"")}</textarea>
      <div class="row">
        <button onclick="submitIntake()">确认无误，取号送审</button>
        <span class="mut">${r.source_files?`附源码 ${r.source_files.length} 件/测试 ${r.test_files.length} 件`:""}</span>
      </div>
    </div>`;
  window._files = r.source_files && r.source_files.length ?
    {source_files:r.source_files, test_files:r.test_files} : {};
}
async function submitIntake(){
  const body = {title:document.getElementById("dTitle").value,
    report:document.getElementById("dReport").value,
    expected_behavior:document.getElementById("dExpect").value,
    target:"coupon_expiry", ...window._files};
  const r = await (await fetch("/api/intake",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}))
    .json();
  if(r.ok===false){alert("取号被拒："+r.error);return;}
  alert("已取号：" + (r.result?r.result.scenario_id:"") + "，进展会主动通知您");
  document.getElementById("draftBox").innerHTML="";
  document.getElementById("ask").value=""; loadTasks();
}

const KIND = {todo:["🔴 待办","todo"],progress:["🔵 进展","progress"],
  receipt:["🟢 回执","receipt"],evidence:["📎 证据","evidence"]};
async function loadTasks(){
  const b = await (await fetch("/api/board")).json();
  const all = Object.values(b.columns).flat()
    .sort((x,y)=>(y.last_ts||0)-(x.last_ts||0));  // 最新动态在最上
  document.getElementById("tasks").innerHTML = all.map(c=>
    `<div class="task" onclick="openTask('${esc(c.run_id)}')">
      <b>${esc(c.title)}</b>
      <div class="mut">${esc(c.state_label||"")}${c.gates_progress?
        " · "+c.gates_progress:""}${c.version_count>1?
        " · 第 "+c.version_index+"/"+c.version_count+" 版"+(c.is_latest?
        "（最新）":""):""}</div></div>`).join("") ||
      '<div class="mut">还没有任务</div>';
}
async function openTask(sid){
  CUR = sid; HISTORY = [];
  const r = await (await fetch("/api/hall/"+sid)).json();
  document.getElementById("taskTitle").textContent =
    r.title + " · " + (r.state_label||"");
  document.getElementById("envs").innerHTML = r.envelopes.slice().reverse().map(e=>{
    const [tag, cls] = KIND[e.kind]||["", "progress"];
    return `<div class="env ${cls}">
      <span class="tag">${tag}</span><span class="t">${esc(e.title)}</span>
      <div class="five">
        <div><b>发生了什么</b>　${esc(e.what)}</div>
        <div><b>意味着什么</b>　${esc(e.meaning)}</div>
        <div><b>您能做什么</b>　${esc(e.action)}</div>
        <div><b>证据在哪</b>　${esc(e.evidence)}</div>
        <div><b>下一步</b>　${esc(e.next)}</div>
      </div></div>`;}).join("");
  document.getElementById("chatArea").style.display="block";
  document.getElementById("chatlog").innerHTML="";
}
async function askFollow(){
  const q = document.getElementById("q").value.trim();
  if(!q||!CUR) return;
  document.getElementById("q").value="";
  const log = document.getElementById("chatlog");
  log.innerHTML += `\n<span class="u">您：</span>${esc(q)}\n`;
  HISTORY.push({role:"user",content:q});
  const r = await (await fetch("/api/hall_chat",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({sid:CUR,message:q,history:HISTORY})})).json();
  const reply = r.reply||("出错了："+(r.error||""));
  HISTORY.push({role:"assistant",content:reply});
  log.innerHTML += `<span>窗口：</span>${esc(reply)}\n`;
  log.scrollTop = log.scrollHeight;
}
loadTasks().then(()=>{
  const h = location.hash.match(/task=([\w-]+)/);
  if(h) openTask(h[1]);
});
setInterval(loadTasks, 4000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# 统一导航（所有视图共享）+ Run 时间线 + Skill 看板
# ---------------------------------------------------------------------------
NAV_HTML = (
    '<div class="topnav"><span class="brand">⚖️ CodeNotary 公证处</span>'
    '<a href="/workbench">任务看板</a>'
    '<a href="/hall">办事大厅</a>'
    '<a href="/skillboard">Skill 看板</a>'
    '</div>')

RUN_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"><title>任务详情 · CodeNotary</title>
<style>
:root{--ink:#1a2332;--sub:#5b6b7f;--line:#d9e0e8;--bg:#f5f7fa;--card:#fff;
--red:#c0392b;--green:#1e8449;--amber:#b9770e;--blue:#2166ac;--hitl:#6c3483}
*{box-sizing:border-box;margin:0}
body{font:13px/1.55 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);padding:14px 20px}
a{color:var(--blue);text-decoration:none}
.topbar{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 16px;display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.topbar h1{font-size:16px}
.meta{display:flex;gap:16px;font-size:12px;color:var(--sub);flex-wrap:wrap}
.meta b{color:var(--ink);font-weight:600}
.st3{display:flex;margin-left:auto;border:1px solid var(--line);
border-radius:8px;overflow:hidden}
.st3 div{padding:5px 14px;font-size:12px;border-right:1px solid var(--line)}
.st3 div:last-child{border-right:0}
.st3 .ok{color:var(--green);font-weight:600}
.st3 .bad{color:var(--red);font-weight:600}
.st3 .wait{color:var(--amber);font-weight:600}
.stepper{display:flex;margin:14px 0;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:10px 8px;
overflow-x:auto}
.step{flex:1;min-width:86px;text-align:center;position:relative;
padding:6px 4px;font-size:12px}
.step:not(:last-child)::after{content:"";position:absolute;top:16px;
right:-4px;width:8px;height:2px;background:var(--line)}
.step .dot{width:10px;height:10px;border-radius:50%;margin:0 auto 4px;
background:#cdd5de;border:2px solid #cdd5de}
.step.done .dot{background:var(--green);border-color:var(--green)}
.step.blocked .dot{background:var(--red);border-color:var(--red)}
.step.blocked.hitl .dot{background:var(--hitl);border-color:var(--hitl)}
.step.current .dot{background:#fff;border-color:var(--blue);
box-shadow:0 0 0 3px rgba(33,102,172,.25)}
.step.hitl .dot{background:var(--hitl);border-color:var(--hitl)}
.step .n{color:var(--ink)} .step .s{color:var(--sub);font-size:11px}
.step.current .n{font-weight:700;color:var(--blue)}
.smwrap{margin-top:14px;background:#f7f9fb;border:1px solid #eef1f5;
border-radius:10px;padding:12px 16px}
.smwrap .t{font-size:12px;color:var(--sub);margin-bottom:8px}
.smstrip{display:flex;gap:0;align-items:center;flex-wrap:wrap;row-gap:8px}
.smstrip .st{padding:4px 12px;font-size:12.5px;background:#eef1f5;
color:var(--sub);border:1px solid transparent}
.smstrip .st:first-child{border-radius:8px 0 0 8px}
.smstrip .st.last{border-radius:0 8px 8px 0}
.smstrip .st.done{background:#e8f6ee;color:var(--green);border-color:#bfe3cf}
.smstrip .st.current{background:var(--blue);color:#fff;font-weight:700;
box-shadow:0 0 0 3px rgba(33,102,172,.2)}
.smstrip .st.off{background:#fdf6e3;color:var(--amber);
border-color:#eedaa5}
.smstrip .arr{color:#b8c2cd;margin:0 2px;font-size:12px}
.tabs{display:flex;gap:2px;border-bottom:2px solid var(--line);margin-top:10px}
.tabs button{background:none;border:0;padding:8px 16px;font-size:13px;
cursor:pointer;color:var(--sub);border-bottom:2px solid transparent;
margin-bottom:-2px}
.tabs button.on{color:var(--blue);border-bottom-color:var(--blue);
font-weight:600}
.pane{background:var(--card);border:1px solid var(--line);border-top:0;
border-radius:0 0 10px 10px;padding:14px 16px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--sub);font-weight:600;padding:6px 8px;
border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid #eef1f5;vertical-align:top}
.chip{display:inline-block;padding:1px 8px;border-radius:4px;font-size:11px}
.chip.green{background:#e8f6ee;color:var(--green)}
.chip.red{background:#fdecea;color:var(--red)}
.chip.hitl{background:#f4ecf7;color:var(--hitl)}
.chip.gray{background:#eef1f5;color:var(--sub)}
.chip.amber{background:#fdf6e3;color:var(--amber)}
details summary{cursor:pointer;color:var(--blue);font-size:12px}
pre{background:#0d1420;color:#d5e3f0;padding:10px;border-radius:8px;
font-size:11.5px;overflow:auto;max-height:280px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:8px;margin:10px 0}
.kv .k{background:#f7f9fb;border:1px solid #eef1f5;border-radius:8px;
padding:8px 10px}
.kv .v{font-size:16px;font-weight:700}
.kv .l{font-size:11px;color:var(--sub)}
.note{font-size:12px;color:var(--sub)}
</style>
</head>
<body>
__NAV__
<div class="topbar">
  <div><h1 id="title">—</h1><div class="meta" id="meta"></div>
  <div class="chain" id="chain"></div></div>
  <div class="st3" id="st3"></div>
</div>
<div class="stepper" id="stepper"></div>
<div class="tabs" id="tabs"></div>
<div class="pane" id="pane"></div>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const sid=new URLSearchParams(location.search).get("sid")
  ||(location.hash.match(/sid=([\w-]+)/)||[])[1];
let R=null,TAB="overview";
const TABS=[["overview","概览"],["contract","验收契约"],["verify","验证结果"],
  ["agents","Agent 运行"],["evidence","证据"],["audit","审计记录"]];
const DEC={green:["通过","green"],red:["未通过","red"],
  yellow:["待复核","amber"]};

async function boot(){
  R=await (await fetch("/api/runview/"+sid)).json();
  if(R.error){document.getElementById("title").textContent="任务不存在";return;}
  renderHead(); renderTabs(); renderPane();
}

function renderHead(){
  document.getElementById("title").textContent=R.title;
  const b=R.binding;
  document.getElementById("meta").innerHTML=[
    b?`PR <b>#${b.pr}</b>`:null,
    b?`Commit <b>${(b.head_sha||"").slice(0,10)}</b>`:null,
    `状态 <b>${esc(R.state_label)}</b>`,
    `Run <b>${esc(R.run_id)}</b>`].filter(Boolean).join("<span>·</span>");
  const ch=R.chain||[];
  document.getElementById("chain").innerHTML=ch.length>1?
    "同一工单的版本链："+ch.map(c=>{
      const lab=esc(c.run_id)+(c.contract_version?` · 契约 v${esc(c.contract_version)}`:"");
      const st=esc(c.state_label||"");
      return c.current?`<span class="v cur"><b>${lab}</b>（${st}·当前）</span>`
        :`<a class="v old" href="/run?sid=${esc(c.run_id)}" style="text-decoration:none;color:inherit">${lab}（${st}）</a>`;
    }).join(" → "):"";
  const st=R.stages;
  const cell=(lab,obj)=>{
    const cls=obj.done?"ok":(obj.state==="未通过"?"bad":
      obj.state==="待人工裁决"||obj.state==="进行中"?"wait":"");
    return `<div class="${cls}">${lab}：${esc(obj.state)}</div>`;};
  document.getElementById("st3").innerHTML=
    cell("验收",st.accept)+cell("合并",st.merge)+cell("发布",st.release);
  document.getElementById("stepper").innerHTML=R.steps.map(s=>{
    const cls=s.blocked?(s.hitl?"step blocked hitl":"step blocked"):s.done?
      (s.hitl?"step done hitl":"step done"):s.current?"step current":"step";
    const sub=s.blocked?(s.hitl?"待裁决":"未通过"):s.done?"完成":
      s.current?"进行中":"待执行";
    return `<div class="${cls}"><div class="dot"></div>
      <div class="n">${esc(s.name)}</div><div class="s">${sub}</div></div>`;
  }).join("");
}

function renderTabs(){
  document.getElementById("tabs").innerHTML=TABS.map(([k,n])=>
    `<button class="${k===TAB?'on':''}" onclick="TAB='${k}';renderPane();renderTabs()">${n}</button>`
  ).join("");
}

function kvGrid(items){
  return `<div class="kv">${items.map(([v,l])=>
    `<div class="k"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`
  ).join("")}</div>`;
}
function detailBlock(obj){
  return `<details><summary>技术详情（原始数据）</summary>
    <pre>${esc(JSON.stringify(obj,null,2))}</pre></details>`;
}

function renderPane(){
  const el=document.getElementById("pane");
  if(TAB==="overview"){
    const m=R.overview.metrics||{};
    const cards=R.cards||[];
    el.innerHTML =
      kvGrid([[R.state_label,"当前状态"],[m.tool_calls??"—","网关调用"],
        [m.wall_time_s!=null?(m.wall_time_s>=3600?(m.wall_time_s/3600).toFixed(1)+" 小时":m.wall_time_s>=60?(m.wall_time_s/60).toFixed(1)+" 分钟":m.wall_time_s+" 秒"):"—","端到端历时（含等待）"],
        [m.adversarial_loop_iterations??0,"对抗环迭代"],
        [m.human_interventions??0,"人工介入"],
        [m.skill_match_calls??0,"Skill 咨询"],
        [m.security_events??0,"越权拒绝"],[R.overview.rework??"—","重修轮次"]])
      + `<div class="smwrap"><div class="t">状态流转（14 态状态机）</div>
        <div class="smstrip">` + R.sm_strip.map((s,i,arr)=>{
        const off=s.done&&!["RECEIVED","SCREENED","TRIAGED","DIAGNOSED",
          "CONTRACTED","AUTHORING","TESTING","GATING","NOTARIZED",
          "RELEASED"].includes(s.state);
        const cls=s.current?"st current":off?"st off":s.done?"st done":"st";
        return (i?'<span class="arr">→</span>':"")+
          `<span class="${cls}${i===arr.length-1?' last':''}">${s.state}</span>`;
      }).join("") + `</div></div>`
      + `<h3 style="font-size:13px;margin:14px 0 6px">处理过程</h3>
      <table><tr><th>环节</th><th>要点</th><th>状态</th><th>下一步</th><th></th></tr>`
      + cards.map(c=>{
        const tone=c.tone||"";
        const chip=tone==="hitl"?"chip hitl":tone==="red"?"chip red":
          tone==="green"?"chip green":"chip gray";
        return `<tr><td><b>${esc(c.role)}</b> · ${esc(c.title)}</td>
          <td>${c.lines.slice(0,2).map(esc).join("<br>")}</td>
          <td><span class="${chip}">${esc(c.status)}</span></td>
          <td class="note">${esc(c.next)}</td>
          <td><details><summary>详情</summary>
            ${c.lines.slice(2).map(l=>`<div>${esc(l)}</div>`).join("")}
            ${detailBlock(c.detail)}</details></td></tr>`;
      }).join("") + "</table>";
  }
  else if(TAB==="contract"){
    const c=(R.cards.find(x=>x.role==="契约")||{});
    el.innerHTML = c.lines
      ? `<table><tr><th>#</th><th>规则内容</th></tr>` +
        c.lines.filter(l=>!l.startsWith("——")).map((l,i)=>
          `<tr><td>${i+1}</td><td>${esc(l)}</td></tr>`).join("") +
        `</table>` + detailBlock(c.detail)
      : `<div class="note">本任务尚未冻结验收规则。</div>`;
  }
  else if(TAB==="verify"){
    el.innerHTML = `<table><tr><th>检验项</th><th>结论</th><th>说明</th>
      <th>未通过用例</th><th></th></tr>` +
      R.verification.map(v=>{
        const[lab,cls]=DEC[v.decision]||[v.decision,"gray"];
        return `<tr><td><b>${esc(v.gate)}</b></td>
          <td><span class="chip ${cls}">${lab}</span></td>
          <td>${esc(v.summary)}</td>
          <td>${(v.failed_tests||[]).map(t=>`<code>${esc(t)}</code>`).join("<br>")||"—"}</td>
          <td>${detailBlock(v.detail)}</td></tr>`;
      }).join("") + "</table>";
  }
  else if(TAB==="agents"){
    el.innerHTML = `<table><tr><th>Agent</th><th>调用</th>
      <th>关键产出</th><th>Skill 调用（信号 → Skill · 治理）</th></tr>` +
      R.agents.map(a=>{
        const sk=(a.skill_calls||[]).map(m=>
          `<div>${esc(m.signal)} → <b>${esc(m.skill)}</b> v${esc(m.version||"—")}
           <span class="chip ${m.governance==="probation"?"amber":"gray"}">${m.governance==="probation"?"试用":"正式"}</span></div>`
        ).join("")||"—";
        return `<tr><td><b>${esc(a.role)}</b></td><td>${a.calls}</td>
          <td class="note">${(a.artifacts||[]).map(esc).join("<br>")||"—"}</td>
          <td>${sk}</td></tr>`;
      }).join("") + "</table>"
      + (R.skill_calls.length? "":
        `<div class="note" style="margin-top:8px">本次运行未发生 Skill 咨询。</div>`);
  }
  else if(TAB==="evidence"){
    el.innerHTML = `<div class="note">证据文件均带 SHA-256 封印；
      点击文件名查看内容并现场校验哈希。</div>
      <table><tr><th>文件</th><th>说明</th></tr>` +
      (R.cards||[]).filter(c=>c.detail).map(c=>
        `<tr><td colspan="2"><b>${esc(c.role)} · ${esc(c.title)}</b>
         ${detailBlock(c.detail)}</td></tr>`).join("") + "</table>";
  }
  else if(TAB==="audit"){
    const sec=(R.security_events||[]).map(e=>
      `<tr><td class="chip red">越权拒绝</td><td>${esc(e.tool)}</td>
       <td>${esc(e.role)}</td><td class="note">${esc(e.detail)}</td></tr>`).join("");
    el.innerHTML = (sec?`<h3 style="font-size:13px;margin-bottom:6px">安全事件</h3>
      <table>${sec}</table>`:"")
      + `<h3 style="font-size:13px;margin:12px 0 6px">调用轨迹（最近 ${R.trace_tail.length} 条）</h3>
      <table><tr><th>时间</th><th>角色</th><th>工具</th><th>状态</th></tr>` +
      R.trace_tail.slice().reverse().map(e=>
        `<tr><td class="note">${new Date(e.ts*1000).toLocaleTimeString()}</td>
         <td>${esc(e.role)}</td><td>${esc(e.tool)}</td>
         <td class="note">${esc(e.state_after)}</td></tr>`).join("") + "</table>";
  }
}
boot();
</script>
</body>
</html>
""".replace("__NAV__", NAV_HTML)

SKILL_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"><title>Skill 看板 · CodeNotary</title>
<style>
:root{--ink:#1a2332;--sub:#5b6b7f;--line:#d9e0e8;--bg:#f5f7fa;--card:#fff;
--red:#c0392b;--green:#1e8449;--amber:#b9770e;--blue:#2166ac}
.topnav{display:flex;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin-bottom:16px;box-shadow:0 1px 3px rgba(26,35,50,.06)}
.topnav .brand{font-weight:700;font-size:14px;margin-right:8px}
.topnav a{padding:5px 14px;border-radius:8px;text-decoration:none;color:#1a2332;font-size:13px}
.topnav a:hover{background:#eef3fb}
*{box-sizing:border-box;margin:0}
body{font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);padding:16px}
h1{font-size:17px;margin-bottom:12px}
.mut{color:var(--sub);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px}
.card .t{font-weight:600;font-size:15px}
.card .d{color:var(--ink);font-size:13px;margin:6px 0}
.badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:4px;
margin-right:4px}
.badge.loaded{background:#e8f6ee;color:var(--green)}
.badge.retired{background:#fdecea;color:var(--red)}
.badge.rejected{background:#fdf6e3;color:var(--amber)}
.badge.probation{background:#fdf6e3;color:var(--amber);border:1px dashed var(--amber)}
.badge.ver{background:#eef1f5;color:var(--sub)}
.sig{border-top:1px dashed var(--line);margin-top:8px;padding-top:6px;
font-size:12px;color:var(--sub)}
.sig b{color:var(--ink)}
.chain{margin-top:6px;font-size:12px}
.chain .v{padding:1px 6px;border:1px solid var(--line);border-radius:4px}
.chain .v.old{opacity:.6;background:#f4f6f8}
.topnav{display:flex;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin-bottom:16px;box-shadow:0 1px 3px rgba(26,35,50,.06)}
.topnav .brand{font-weight:700;font-size:14px;margin-right:8px}
.topnav a{padding:5px 14px;border-radius:8px;text-decoration:none;color:#1a2332;font-size:13px}
.topnav a:hover{background:#eef3fb}
.chain .v.ret{opacity:.5;text-decoration:line-through}
.chain .v.cur{background:#e8f0fe;border-color:var(--blue)}
</style>
</head>
<body>
__NAV__
<div style="display:flex;align-items:center">
<h1>Skill 看板</h1>
<span class="mut" style="margin-left:auto">治理令牌
<input id="tok" type="password" placeholder="裁决人令牌（只存本页内存）"
 style="padding:4px 8px;border:1px solid var(--line);border-radius:6px"></span>
</div>
<div class="mut" id="sub"></div>
<div id="queue"></div>
<h2 id="gridLabel" style="font-size:15px;margin:14px 0 8px"></h2>
<div class="grid" id="grid"></div>
<script>
async function govern(action, name){
  const reason = prompt((action==="confirm"?"追认理由（将写入注册表）：":
    "否决理由（一票否决，将写入注册表）："));
  if(!reason) return;
  const r = await (await fetch("/api/skill_"+action,{method:"POST",
    headers:{"Content-Type":"application/json",
      "X-Console-Token":document.getElementById("tok").value},
    body:JSON.stringify({name, reason})})).json();
  if(r.ok===false){ alert("被拒："+(r.error||"")); return; }
  boot();
}
</script>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const STATUS={loaded:["在用","loaded"],retired:["已退役","retired"],
  rejected:["未通过兼容检查","rejected"],probation:["试用中","probation"]};
async function boot(){
  const r=await (await fetch("/api/skillboard")).json();
  document.getElementById("sub").textContent=
    `共 ${r.cards.length} 个 Skill ｜ 注册表只增不改（append-only）`;
  const q = r.probation_queue||[];
  document.getElementById("queue").innerHTML = q.length ?
    `<h2 style="font-size:15px;margin:14px 0 8px">追认队列（${q.length}）</h2>
     <div class="mut" style="margin-bottom:8px">试用区的 Skill 可以参与判断、
     结果标注试用；命中数据见各 run 的 skill_matches.json。
     追认转正或一票否决都须留理由、进注册表。</div>` +
    q.map(c=>`<div class="card">
      <div class="t">${esc(c.name)} <span class="badge probation">试用中</span>
        <span class="badge ver">v${esc(c.version||"—")}</span></div>
      <div class="d">${esc((c.description||"").split("；")[0].split("。")[0])}</div>
      <div class="row" style="display:flex;gap:8px;margin-top:8px">
        <button onclick="govern('confirm','${esc(c.name)}')">追认转正</button>
        <button class="ghost" onclick="govern('retire','${esc(c.name)}')">一票否决</button>
      </div></div>`).join("") : "";
  document.getElementById("gridLabel").textContent = "全部 Skill";
  document.getElementById("grid").innerHTML=r.cards.map(c=>{
    const[lab,cls]=(STATUS[c.status]||[c.status||"—","ver"]);
    const sigs=(c.signals||[]).map(s=>
      `<div>信号 <b>${esc(s.signal)}</b>：${esc(s.trigger)}<br>
       <span>适用角色：${(s.roles||[]).join("、")} ｜ 评估覆盖 ${s.coverage_n} 样本</span></div>`
    ).join("")||"<div>未被信号表引用（种子储备）</div>";
    return `<div class="card">
      <div class="t">${esc(c.name)}</div>
      <div><span class="badge ${cls}">${lab}</span>
        <span class="badge ver">v${esc(c.version||"—")}</span>
        <span class="badge ver">${esc(c.compat||"")}</span></div>
      <div class="d">${esc((c.description||"").split("；")[0].split("。")[0])}</div>
      <div class="sig">${sigs}</div>
    </div>`;
  }).join("");
}
boot();
</script>
</body>
</html>
""".replace("__NAV__", NAV_HTML)


class Handler(BaseHTTPRequestHandler):
    runs_dir = PKG_ROOT / "runs"

    def _send(self, code: int, body: str, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _merge_card(self, sid: str) -> dict:
        """Merge 确认卡：薄工具 merge_readiness 的三态意见（release 角色执行，
        非 LLM）。证据包里的 pr_binding 提供绑定的 commit SHA。"""
        run_dir = self.runs_dir / sid
        binding = read_json(run_dir / "evidence" / "pr_binding.json")
        if not binding:
            return {"available": False,
                    "reason": "该 run 没有 PR 绑定（evidence/pr_binding.json）"
                              "——inhouse 任务无合并对象"}
        if not DEMO_REPO:
            return {"available": False,
                    "reason": "console 未配置 --demo-repo，无法做 git 检查"}
        tool = Path(DEMO_REPO) / "scripts" / "merge_readiness.py"
        if not tool.exists():
            return {"available": False, "reason": f"缺 {tool}"}
        argv = [sys.executable, str(tool), "--repo-dir", DEMO_REPO,
                "--base", "main", "--head-sha", binding["head_sha"],
                "--evidence", str(run_dir.resolve()), "--json"]
        if os.environ.get("GH_TOKEN") and binding.get("repo"):
            argv += ["--repo", binding["repo"]]      # 真实 GitHub check
        else:
            argv += ["--local"]  # 未接入时第五项以证书绑定为准（见工具文案）
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=60)
        try:
            card = json.loads(proc.stdout)
        except json.JSONDecodeError:
            card = {"ready": False, "verdict": "检查异常",
                    "checks": [], "card_raw": proc.stdout + proc.stderr}
        return {"available": True, "ready": proc.returncode == 0,
                "card": card, "binding": binding}

    def _check_token(self) -> dict | None:
        """Return verified (redacted) claims for the write token, or None.

        Modes: no guard (demo), static shared token (legacy --token),
        capability JWT (--token-secret; HS256 claims = sub/role/scope/
        credential_id/exp). What reaches the gateway is the CLAIMS —
        the raw token never leaves this process.
        """
        if TOKEN is None and TOKEN_SECRET is None:
            return {"subject": "operator", "role": "leader",
                    "scope": ["resolve", "adjudicate"],
                    "credential_id": "session-unguarded"}
        header = self.headers.get("X-Console-Token", "")
        if TOKEN_SECRET is not None:
            try:
                from notary_token import verify as verify_token, \
                    redacted_claims
                return redacted_claims(verify_token(header, TOKEN_SECRET))
            except Exception:
                return None
        if hmac.compare_digest(header, TOKEN or ""):
            return {"subject": "console-operator", "role": "leader",
                    "scope": ["resolve", "adjudicate"],
                    "credential_id": "static-token"}
        return None

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self.send_response(302)
            self.send_header("Location", "/hall")
            self.end_headers()
        elif path == "/pipeline":
            self._send(200, PAGE, "text/html")
        elif path == "/desk":
            self._send(200, DESK_PAGE, "text/html")
        elif path == "/workbench":
            self._send(200, WORKBENCH_PAGE, "text/html")
        elif path == "/hall":
            self._send(200, HALL_PAGE, "text/html")
        elif path == "/run":
            self._send(200, RUN_PAGE, "text/html")
        elif path.startswith("/api/runview/"):
            sid = path.rsplit("/", 1)[-1]
            run_dir = self.runs_dir / sid
            if not run_dir.is_dir():
                self._send(404, json.dumps({"error": "unknown run"}))
            else:
                self._send(200, json.dumps(runview_data(run_dir,
                                                        self.runs_dir)))
        elif path == "/skillboard":
            self._send(200, SKILL_PAGE, "text/html")
        elif path == "/api/skillboard":
            self._send(200, json.dumps(skillboard_data()))
        elif path.startswith("/api/hall/"):
            sid = path.rsplit("/", 1)[-1]
            run_dir = self.runs_dir / sid
            if not run_dir.is_dir():
                self._send(404, json.dumps({"error": "unknown run"}))
            else:
                f = hall_facts(run_dir)
                self._send(200, json.dumps({
                    "run_id": sid,
                    "title": f["issue"].get("title", sid),
                    "state": f["state"],
                    "state_label": STATE_LABELS.get(f["state"], f["state"]),
                    "envelopes": hall_timeline(run_dir)}))
        elif path == "/api/board":
            self._send(200, json.dumps(board_data(self.runs_dir)))
        elif path.startswith("/api/adjudication_context/"):
            sid = path.rsplit("/", 1)[-1]
            run_dir = self.runs_dir / sid
            if not run_dir.is_dir():
                self._send(404, json.dumps({"error": "unknown run"}))
            else:
                self._send(200, json.dumps(adjudication_context(run_dir)))
        elif path.startswith("/api/merge_card/"):
            sid = path.rsplit("/", 1)[-1]
            self._send(200, json.dumps(self._merge_card(sid)))
        elif path == "/api/gw":
            h = gateway_get("/health")
            ver = gateway_get("/policy")
            if h and h.get("ok"):
                self._send(200, json.dumps(
                    {"ok": True,
                     "version": (ver or {}).get("result", {}).get(
                         "gateway_version", "?")}))
            else:
                self._send(200, json.dumps({"ok": False}))
        elif path == "/api/runs":
            self._send(200, json.dumps(list_runs(self.runs_dir)))
        elif path == "/api/scenarios":
            self._send(200, json.dumps(gallery_data()))
        elif path == "/api/skills":
            self._send(200, json.dumps(skills_data()))
        elif path.startswith("/api/skill_get"):
            # 按名取 Skill（runless 代理，供办事大厅 /skill 命令）
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            _code, body = gateway_post("skill-showcase", "notary_skill.get",
                                       {"name": name})
            self._send(200, json.dumps(body))
        elif path == "/api/audit":
            self._send(200, json.dumps(audit_data(self.runs_dir)))
        elif path == "/api/policy":
            p = gateway_get("/policy")
            self._send(200, json.dumps(p or {"ok": False}))
        elif path.startswith("/api/run/"):
            sid = path.rsplit("/", 1)[-1]
            run_dir = self.runs_dir / sid
            if not run_dir.is_dir():
                self._send(404, json.dumps({"error": "unknown run"}))
            else:
                self._send(200, json.dumps(run_summary(run_dir)))
        elif path.startswith("/api/change/"):
            # 变更预览（PR 视图）：inhouse=author 写的修复；external=送审补丁
            sid = path.rsplit("/", 1)[-1]
            run_dir = self.runs_dir / sid
            author_wt = run_dir / "work" / "author_wt"
            out: dict = {}
            if author_wt.is_dir() and any(author_wt.glob("*.py")):
                for f in sorted(author_wt.glob("*.py")):
                    out[f.name] = f.read_text(
                        encoding="utf-8", errors="replace")[:20000]
                src = "author"
            else:
                fx = read_json(PKG_ROOT / "scenarios" / f"{sid}.json") or {}
                ch = fx.get("submitted_change") or {}
                out = {k: str(v)[:20000]
                       for k, v in (ch.get("files") or {}).items()}
                src = "submitted"
            fx2 = read_json(PKG_ROOT / "scenarios" / f"{sid}.json") or {}
            self._send(200, json.dumps({
                "files": out, "source": src if out else None,
                "advisory": bool(fx2.get("advisory"))}))
        elif path.startswith("/api/file/"):
            rest = path[len("/api/file/"):]
            sid, _, rel = rest.partition("/")
            f = (self.runs_dir / sid / rel).resolve()
            root = (self.runs_dir / sid).resolve()
            if not str(f).startswith(str(root)) or not f.is_file():
                self._send(404, json.dumps({"error": "no such file"}))
            else:
                try:
                    raw = f.read_bytes()
                    # generous cap: truncating content would break the
                    # client-side seal verification hash
                    content = raw.decode("utf-8")[:200000]
                except Exception:
                    content = "(二进制文件)"
                manifest = read_json(self.runs_dir / sid / "manifest.json") or {}
                self._send(200, json.dumps({
                    "content": content,
                    "declared_sha256": manifest.get(rel),
                    "actual_sha256": hashlib.sha256(raw).hexdigest()}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")
                                 ) if length else {}
        except Exception:
            self._send(400, json.dumps({"ok": False, "error": "bad json"}))
            return
        # 大厅公共面（只读会话/起草/取号）免令牌；受审写通道必须 JWT
        if path not in ("/api/intake", "/api/assist", "/api/skill_match",
                        "/api/hall_chat"):
            claims = self._check_token()
            if claims is None:
                self._send(403, json.dumps({"ok": False,
                                            "error": "bad token"}))
                return
        if path == "/api/intake":
            payload["role"] = "ci"
            code, body = gateway_post("_intake", "notary_intake.submit_issue",
                                      payload)
            self._send(code, json.dumps(body))
        elif path == "/api/assist":
            # LLM 接待：ZIP + 一句话 → 表单草稿（多轮：history 由客户端携带）
            message = str(payload.get("message", "")).strip()
            history = payload.get("history") or []
            if not isinstance(history, list):
                history = []
            files_map: dict = {}
            skipped: list = []
            warnings: list = []
            if payload.get("zip_b64"):
                try:
                    unpacked = unzip_py_files(str(payload["zip_b64"]))
                    files_map = {**unpacked["source_files"],
                                 **unpacked["test_files"]}
                    skipped = unpacked["skipped"]
                    if not unpacked["source_files"]:
                        self._send(200, json.dumps({"ok": False, "error":
                            "包里没有找到 .py 源码文件"
                            + (f"（跳过了 {len(skipped)} 个非 .py 文件）"
                               if skipped else "")}))
                        return
                except Exception as exc:
                    self._send(200, json.dumps(
                        {"ok": False, "error": f"ZIP 解不开：{exc}"}))
                    return
            elif isinstance(payload.get("files"), dict):
                files_map = {str(k): str(v)
                             for k, v in payload["files"].items()}
            source_files = {k: v for k, v in files_map.items()
                            if not k.startswith("test_")}
            test_files = {k: v for k, v in files_map.items()
                          if k.startswith("test_")}
            if len(source_files) > 10 or len(test_files) > 10:
                self._send(200, json.dumps(
                    {"ok": False, "error": "文件太多了（各最多 10 个）"}))
                return
            total = sum(len(v.encode("utf-8")) for v in files_map.values())
            if total > 200_000:
                self._send(200, json.dumps(
                    {"ok": False, "error": "包太大了（超过 200KB）"}))
                return
            if not test_files:
                warnings.append("no_tests")
            history.append({"role": "user", "content": message})
            draft = llm_assist(history, files_map)
            self._send(200, json.dumps({
                "ok": True, "llm": draft is not None,
                "draft": draft or {"title": "", "report": message,
                                   "expected_behavior": "", "mode": "",
                                   "questions": ""},
                "source_files": source_files, "test_files": test_files,
                "skipped": skipped, "warnings": warnings}))
        elif path == "/api/skill_match":
            payload.setdefault("role", "ci")
            # Anchor to the run the user is viewing (exists in gateway
            # memory, no reset); fall back to any known scenario.
            sid = payload.pop("sid", None)
            if not sid:
                scenarios = sorted((PKG_ROOT / "scenarios").glob("*.json"))
                sid = scenarios[0].stem if scenarios else "_"
            _code, body = gateway_post(sid, "notary_skill.match", payload)
            self._send(200, json.dumps(body))
        elif path.startswith("/api/reset/"):
            sid = path.rsplit("/", 1)[-1]
            _code, body = gateway_post(sid, "reset", {})
            self._send(200, json.dumps(body))
        elif path.startswith("/api/resolve/"):
            sid = path.rsplit("/", 1)[-1]
            payload["role"] = "human"
            _code, body = gateway_post(sid, "notary_flow.resolve_human",
                                       payload)
            self._send(200, json.dumps(body))
        elif path.startswith("/api/adjudicate/"):
            # 人工门②：最小权限 capability——scope 必须含 adjudicate
            if "adjudicate" not in claims.get("scope", []):
                self._send(403, json.dumps({
                    "ok": False,
                    "error": "scope 'adjudicate' required; this token "
                             "cannot adjudicate"}))
                return
            sid = path.rsplit("/", 1)[-1]
            payload["role"] = claims.get("role") or "adjudicator"
            payload["claims"] = claims  # redacted claims; never the token
            payload["channel"] = "console-token"
            _code, body = gateway_post(sid, "notary_flow.adjudicate",
                                       payload)
            # 修订契约：裁决生效后系统动作全自动——用卡片上人手编辑的
            # 修订文本发布契约 v1.1（references 由网关从裁决记录带入）
            revision = payload.get("revision_assertions")
            if body.get("ok") and payload.get("decision") == "revise" \
                    and isinstance(revision, list) and revision:
                fcode, fbody = gateway_post(sid, "notary_contract.freeze", {
                    "assertions": [str(a) for a in revision],
                    "role": "contract"})
                body.setdefault("result", {})["contract_revision"] = \
                    fbody.get("result", fbody)
            self._send(200, json.dumps(body))
        elif path == "/api/skill_confirm":
            if "adjudicate" not in claims.get("scope", []):
                self._send(403, json.dumps({
                    "ok": False, "error": "scope 'adjudicate' required"}))
                return
            payload["role"] = claims.get("role") or "adjudicator"
            payload["claims"] = claims
            sid = str(payload.pop("sid", None) or "skill-governance")
            _code, body = gateway_post(sid, "notary_skill.confirm", payload)
            self._send(200, json.dumps(body))
        elif path == "/api/skill_retire":
            if "adjudicate" not in claims.get("scope", []):
                self._send(403, json.dumps({
                    "ok": False, "error": "scope 'adjudicate' required"}))
                return
            payload["role"] = claims.get("role") or "leader"
            payload["claims"] = claims
            sid = str(payload.pop("sid", None) or "skill-governance")
            _code, body = gateway_post(sid, "notary_skill.retire", payload)
            self._send(200, json.dumps(body))
        elif path == "/api/hall_chat":
            # 会话追问：只读落盘证据，永不驱动状态机（聊天起草、卡片生效）
            sid = str(payload.get("sid", ""))
            run_dir = self.runs_dir / sid
            if not run_dir.is_dir():
                self._send(404, json.dumps({"ok": False,
                                            "error": "unknown run"}))
                return
            message = str(payload.get("message", "")).strip()
            history = payload.get("history") or []
            if not message:
                self._send(400, json.dumps({"ok": False,
                                            "error": "empty message"}))
                return
            self._send(200, json.dumps(hall_chat_reply(
                run_dir, history if isinstance(history, list) else [],
                message)))
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=18091)
    ap.add_argument("--runs", default=str(PKG_ROOT / "runs"))
    ap.add_argument("--gateway", default="http://127.0.0.1:18090")
    ap.add_argument("--token", default=None,
                    help="if set, write APIs (intake/reset/resolve) require "
                         "X-Console-Token header; read APIs stay open")
    ap.add_argument("--token-secret", default=None,
                    help="path to the HS256 secret file: write APIs require "
                         "a capability JWT (see tools/notary_token.py); "
                         "takes precedence over --token")
    ap.add_argument("--demo-repo", default=None,
                    help="path to the codenotary-demo checkout; enables the "
                         "merge-readiness card on the workbench")
    args = ap.parse_args()
    global GATEWAY, TOKEN, TOKEN_SECRET, DEMO_REPO
    GATEWAY = args.gateway
    TOKEN = args.token
    DEMO_REPO = args.demo_repo
    if args.token_secret:
        TOKEN_SECRET = Path(args.token_secret).read_text(encoding="utf-8").strip()
    Handler.runs_dir = Path(args.runs)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = ("capability-JWT" if TOKEN_SECRET else
            "static" if TOKEN else "OFF (demo mode)")
    print(f"CodeNotary Console v2 on http://{args.host}:{args.port} "
          f"(runs: {Handler.runs_dir}, gateway: {GATEWAY}, "
          f"write-token: {mode})")
    srv.serve_forever()




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
