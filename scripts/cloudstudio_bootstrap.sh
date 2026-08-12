#!/usr/bin/env bash
# CodeNotary AgentTeams 代码包 — Cloud Studio 一键引导脚本
# 在 Cloud Studio 工作区终端中、代码包解压后的目录里执行：
#   bash scripts/cloudstudio_bootstrap.sh
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PKG_DIR"

echo "== 1/5 启动公证工具网关（端口 18090）"
if curl -s -m 2 http://127.0.0.1:18090/health >/dev/null 2>&1; then
  echo "    网关已在运行，跳过启动"
else
  mkdir -p runs
  nohup python3 tools/notary_gateway.py --host 0.0.0.0 --port 18090 \
    > runs/gateway.log 2>&1 &
  echo "    网关已启动，PID $!，日志 runs/gateway.log"
  sleep 1
fi
curl -s http://127.0.0.1:18090/health && echo

echo "== 2/5 定位 manager 容器与 docker 网关地址"
MANAGER="$(docker ps --format '{{.Names}}' | grep -E 'manager' | head -1)"
if [ -z "$MANAGER" ]; then
  echo "    未找到 manager 容器，请确认 AgentTeams 已启动" >&2
  exit 1
fi
echo "    manager 容器: $MANAGER"
GW_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{println .Gateway}}{{end}}' "$MANAGER" | head -1)"
BASE_URL="http://${GW_IP}:18090"
echo "    NOTARY_TOOL_BASE_URL = $BASE_URL"

echo "== 3/5 从容器内验证网关可达"
if docker exec "$MANAGER" curl -s -m 5 "$BASE_URL/health" | grep -q '"ok": true'; then
  echo "    容器内访问网关 OK"
else
  echo "    容器内访问网关失败，请检查网关监听地址与 docker 网络" >&2
  exit 1
fi

echo "== 4/5 替换创建消息中的网关地址占位符"
sed -i "s|<NOTARY_TOOL_BASE_URL>|${BASE_URL}|g" at/create_agents_messages.md
echo "    at/create_agents_messages.md 已就绪"

echo "== 5/5 网关自检（无 LLM 全流程 dryrun）"
python3 scripts/local_dryrun.py > runs/dryrun.log 2>&1 && \
  echo "    dryrun 通过（日志 runs/dryrun.log）" || \
  { echo "    dryrun 失败，见 runs/dryrun.log" >&2; exit 1; }

cat <<EOF

完成。接下来在 Element Web（18088 预览地址）中手动执行两步：

  1. 进入 manager 房间，整段发送 at/create_agents_messages.md 中
     "复制到 Manager 的完整创建请求"（占位符已替换为 $BASE_URL）。
     等待 10 个 Worker + codenotary Team 串行创建完成。
  2. 进入 Team: codenotary 房间，按 at/run_demo_task_message.md
     依次 @codenotary-leader 发送两个公证任务。
EOF
