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
import io
import json
import os
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
            for line in trace.read_text().splitlines():
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
        for line in alog.read_text().splitlines()[-50:]:
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
  <a class="btn ghost" href="/">工程后台 →</a>
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


class Handler(BaseHTTPRequestHandler):
    runs_dir = PKG_ROOT / "runs"

    def _send(self, code: int, body: str, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _check_token(self) -> bool:
        if TOKEN is None:
            return True
        return self.headers.get("X-Console-Token") == TOKEN

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._send(200, PAGE, "text/html")
        elif path == "/desk":
            self._send(200, DESK_PAGE, "text/html")
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
        if not self._check_token():
            self._send(403, json.dumps({"ok": False, "error": "bad token"}))
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")
                                 ) if length else {}
        except Exception:
            self._send(400, json.dumps({"ok": False, "error": "bad json"}))
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
    args = ap.parse_args()
    global GATEWAY, TOKEN
    GATEWAY = args.gateway
    TOKEN = args.token
    Handler.runs_dir = Path(args.runs)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CodeNotary Console v2 on http://{args.host}:{args.port} "
          f"(runs: {Handler.runs_dir}, gateway: {GATEWAY}, "
          f"write-token: {'set' if TOKEN else 'OFF (demo mode)'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
