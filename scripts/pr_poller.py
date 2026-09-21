#!/usr/bin/env python3
"""pr_poller: PR 重线——GitHub PR 自动唤起 10-agent 房间公证并回写。

链路：盯 PR（新 head）→ 网关取号（external 送审，run_tag 幂等）→
Matrix 房间派发给 leader → 轮询终态 → 回写 PR 评论 + commit status。

与轻线（ci_audit in Action）互不干扰：本脚本不读 spec、不在 runner 上跑、
判断棒全部由房间里的 10 个 worker（真 LLM）现场生成；网关校验/判决/
留痕两线完全相同。

安全默认：--apply 不给时，GitHub 回写（评论/status）一律 dry-run 只打印；
取号与轮询是对本地网关的真实调用（网关只听 127.0.0.1，本脚本同机跑）。

用法：
  # 盯仓循环（dry-run 回写）
  python3 scripts/pr_poller.py --repo anita769/codenotary-demo \
      --room '!xxx:server' --token-file ~/.config/codenotary/matrix_token
  # 单轮 + 夹具（本地 E2E，不碰 GitHub/Matrix）
  python3 scripts/pr_poller.py --once --fixture /tmp/pr.json --no-dispatch
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent

TERMINAL = {"NOTARIZED", "REJECTED", "QUARANTINED", "RELEASED",
            "ROLLED_BACK"}
STATE_CN = {
    "NOTARIZED": "已公证（验收通过）", "REJECTED": "未放行",
    "QUARANTINED": "检疫隔离", "ESCALATED": "等待人工裁决",
    "RELEASED": "已发布", "ROLLED_BACK": "已回滚",
}
ANCHOR = "<!-- codenotary-heavy:pr{n} -->"


# ---------------------------------------------------------------- http ----
def gw_call(base: str, sid: str, tool: str, payload: dict | None = None,
            tolerate: tuple[str, ...] = ()) -> dict:
    req = urllib.request.Request(
        f"{base}/tools/{sid}/{tool}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
    if not body.get("ok") and not any(t in body.get("error", "")
                                      for t in tolerate):
        raise RuntimeError(f"gateway {tool}: {body.get('error')}")
    return body


def gh_api(repo: str, method: str, path: str,
           payload: dict | None = None) -> dict | list:
    token = os.environ.get("GH_TOKEN") or subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        data=json.dumps(payload).encode("utf-8") if payload else None,
        method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    for attempt in range(5):  # 指数退避（ci_audit 同款）
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429, 502, 503) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
    return {}


def matrix_post(matrix: str, token: str, room: str, text: str,
                mention: str | None = None) -> bool:
    txn = str(time.time_ns())
    url = (f"{matrix}/_matrix/client/v3/rooms/"
           f"{urllib.parse.quote(room, safe='')}/send/m.room.message/{txn}")
    req = urllib.request.Request(url, method="PUT")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    content: dict = {"msgtype": "m.text", "body": text}
    if mention:
        # qwenpaw 频道只认 m.mentions 结构化字段，裸 @昵称 不算提到
        content["m.mentions"] = {"user_ids": [mention]}
    try:
        urllib.request.urlopen(
            req, data=json.dumps(content).encode(), timeout=15)
        return True
    except Exception as exc:
        print(f"[poller] matrix post failed: {exc}", flush=True)
        return False


# ------------------------------------------------------------ 取号载荷 ----
def build_intake(pr: dict, target: str) -> dict:
    """PR 元数据 → external 送审载荷。契约/盲测不在此预写——那是房间里
    判断棒的活；这里只给工单与补丁，剩下的交给 10 个 worker。"""
    n, sha = pr["number"], pr["head"]["sha"]
    files = {}
    for f in pr.get("_files", []):
        # 1:1 按文件名映射到靶场（spec 的 file_map 是轻线的拐杖，重线不用）
        files[Path(f["filename"]).name] = f["_content"]
    return {
        "title": f"PR#{n} {pr['title']}",
        "report": (pr.get("body") or "").strip()
                  or "（PR 未填描述，按补丁内容审计）",
        "expected_behavior": "PR 声明的意图真实达成，且不破坏既有行为；"
                             "边界与歧义按契约流程形式化。",
        "target": target,
        "files": files,
        "source": f"github-pr:{n}",
        "run_tag": f"pr{n}-{sha[:7]}",
    }


def fetch_pr(repo: str, number: int) -> dict:
    pr = gh_api(repo, "GET", f"/pulls/{number}")
    changed = gh_api(repo, "GET", f"/pulls/{number}/files?per_page=50")
    sha = pr["head"]["sha"]
    out = []
    for f in changed:
        if f.get("status") == "removed":
            continue
        content = gh_api(repo, "GET",
                         f"/contents/{f['filename']}?ref={sha}")
        import base64
        out.append({"filename": f["filename"],
                    "_content": base64.b64decode(
                        content["content"]).decode("utf-8")})
    pr["_files"] = out
    return pr


# ------------------------------------------------------------ 派发/回写 ----
def dispatch_text(sid: str, repo: str, pr_number: int) -> str:
    return (f"@codenotary-leader 请让你的 Team 公证一条新的变更任务。\n\n"
            f"scenario_id: {sid}\n"
            f"模式：外部 AI 变更送审（external）\n"
            f"来源：{repo} PR#{pr_number}\n\n"
            f"请按完整公证流程处理，并输出本次公证报告。")


def compose_comment(anchor: str, sid: str, state: str,
                    verdicts: dict, console_url: str | None) -> str:
    lines = [anchor, f"### ⚖️ CodeNotary 公证（10-agent 协作线）",
             f"",
             f"**结论：{STATE_CN.get(state, state)}** ｜ 任务 `{sid}`"]
    for gate, v in sorted(verdicts.items()):
        icon = {"green": "✅", "red": "❌", "yellow": "🟡"}.get(
            v.get("decision"), "·")
        lines.append(f"- {icon} **{gate}**：{v.get('summary', '')}")
    if state == "ESCALATED":
        lines += ["", "> ⏸ 流水线停在争议点，等待人工裁决——裁决落地后"
                  "重新触发即可继续，check 保持 pending。"]
    if console_url:
        lines += ["", f"全过程证据（契约/盲测/门禁/封印）："
                      f"{console_url}/run?sid={sid}"]
    return "\n".join(lines)


def write_back(repo: str, pr_number: int, sha: str, body: str,
               state: str, apply: bool) -> None:
    gh_state = {"NOTARIZED": "success", "RELEASED": "success",
                "REJECTED": "failure", "QUARANTINED": "failure",
                "ESCALATED": "pending"}.get(state, "pending")
    if not apply:
        print(f"[poller] DRY-RUN 回写 PR#{pr_number} "
              f"(status={gh_state}):\n{body}\n", flush=True)
        return
    # 评论：锚点幂等——找到旧评论原地更新，不刷屏
    comments = gh_api(repo, "GET",
                      f"/issues/{pr_number}/comments?per_page=100")
    anchor = body.split("\n", 1)[0]
    hit = next((c for c in comments if anchor in c.get("body", "")), None)
    if hit:
        gh_api(repo, "PATCH", f"/issues/comments/{hit['id']}",
               {"body": body})
    else:
        gh_api(repo, "POST", f"/issues/{pr_number}/comments",
               {"body": body})
    gh_api(repo, "POST", f"/statuses/{sha}",
           {"state": gh_state, "context": "codenotary-heavy",
            "description": STATE_CN.get(state, state)[:140]})


# ------------------------------------------------------------- 状态文件 ----
def load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(p: Path, st: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                 encoding="utf-8")


# ----------------------------------------------------------------- 主流程 ----
def process_pr(args, pr: dict, st: dict) -> None:
    n, sha = pr["number"], pr["head"]["sha"]
    key = str(n)
    if st.get(key, {}).get("sha") == sha and not args.fixture:
        return  # 已处理过这个 head
    print(f"[poller] PR#{n} new head {sha[:7]}，送审…", flush=True)
    intake = build_intake(pr, args.target)
    body = gw_call(args.gateway, "intake", "notary_intake.submit_issue",
                   intake, tolerate=("already intaken",))
    sid = (body.get("result") or {}).get("scenario_id")
    if not sid and "already intaken" in body.get("error", ""):
        # 幂等命中（同内容同 run_tag）：按内容哈希复算 scenario_id
        import hashlib
        sid = "intake_" + hashlib.sha256(
            (intake["title"] + intake["report"]
             + intake["expected_behavior"]
             + str(intake.get("run_tag", ""))).encode()).hexdigest()[:10]
    if not sid:
        print(f"[poller] intake failed: {body}", flush=True)
        return
    print(f"[poller] 取号 {sid}（run_tag={intake['run_tag']}）", flush=True)

    if args.no_dispatch:
        print(f"[poller] --no-dispatch，派发消息仅打印：\n"
              f"{dispatch_text(sid, args.repo, n)}\n", flush=True)
    else:
        token = Path(args.token_file).read_text().strip()
        matrix_post(args.matrix, token, args.room,
                    dispatch_text(sid, args.repo, n),
                    mention=args.mention)

    st[key] = {"sha": sha, "sid": sid, "state": "RUNNING",
               "ts": time.time()}

    if args.no_wait:
        return
    # 轮询终态；ESCALATED 回写 pending 后退出不等人（裁决后重跑即可）
    seen_pending = False
    while True:
        cur = gw_call(args.gateway, sid, "notary_state.get")["result"]
        state = cur["state"]
        if state == "ESCALATED" and not seen_pending:
            seen_pending = True
            v = gw_call(args.gateway, sid, "notary_verdicts.list")[
                "result"]["verdicts"]
            write_back(args.repo, n, sha,
                       compose_comment(ANCHOR.format(n=n), sid, state, v,
                                       args.console_url), state, args.apply)
        if state in TERMINAL:
            v = gw_call(args.gateway, sid, "notary_verdicts.list")[
                "result"]["verdicts"]
            write_back(args.repo, n, sha,
                       compose_comment(ANCHOR.format(n=n), sid, state, v,
                                       args.console_url), state, args.apply)
            st[key].update({"state": state, "ts": time.time()})
            return
        time.sleep(args.interval)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="anita769/codenotary-demo")
    ap.add_argument("--gateway", default="http://127.0.0.1:18090")
    ap.add_argument("--target", default="coupon_expiry")
    ap.add_argument("--state-file",
                    default=str(PKG_ROOT / "runs" / "_pr_poller_state.json"))
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--console-url", default=None)
    ap.add_argument("--fixture", help="本地 PR 夹具 JSON（不碰 GitHub）")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-wait", action="store_true",
                    help="取号+派发后即退出（终态回写下轮再补）")
    ap.add_argument("--no-dispatch", action="store_true")
    ap.add_argument("--matrix", default="http://127.0.0.1:18080")
    ap.add_argument("--room")
    ap.add_argument("--token-file")
    ap.add_argument("--mention",
                    default="@codenotary-leader:matrix-local.agentteams.io:18080",
                    help="派发的 m.mentions 目标（leader 的完整 mxid）")
    ap.add_argument("--apply", action="store_true",
                    help="真实回写 GitHub（默认 dry-run 只打印）")
    args = ap.parse_args()
    if not args.no_dispatch and not (args.room and args.token_file):
        ap.error("--room 与 --token-file 必须同给，或用 --no-dispatch")

    state_p = Path(args.state_file)
    st = load_state(state_p)

    def one_round():
        if args.fixture:
            prs = [json.loads(Path(args.fixture).read_text(
                encoding="utf-8"))]
        else:
            prs = gh_api(args.repo, "GET",
                         "/pulls?state=open&per_page=20")
        for pr in prs:
            if not args.fixture:
                pr = fetch_pr(args.repo, pr["number"])
            process_pr(args, pr, st)
        save_state(state_p, st)

    if args.once:
        one_round()
    else:
        while True:
            try:
                one_round()
            except Exception as exc:
                print(f"[poller] round failed: {exc}", flush=True)
            time.sleep(max(args.interval, 30))


if __name__ == "__main__":
    main()
