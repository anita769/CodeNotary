# 使用 AgentTeams 运行 CodeNotary Demo

这份手册面向第一次运行本代码包的评审与使用者。运行机器可以是本地 Mac、Linux 服务器或云主机；公证工具网关和 AgentTeams 部署在同一台机器上。

核心流程：

1. 启动 HTTP 公证工具网关。
2. 安装 AgentTeams（已安装则跳过），并按安装器引导完成 LLM 配置。
3. 找到 Docker Worker 可访问的网关地址。
4. 在 `manager` 房间创建 10 个业务 Worker，并在创建 Team 时生成独立 TeamLeader Worker。
5. 在名称以 `Team` 开头的 Team 房间，通过 `@<team_leader_name>` 依次发送两个公证任务。

## 1. 准备运行机器

需要：

- Docker 或兼容运行时。
- Python 3（网关仅用标准库，无第三方依赖）。
- 一个 AgentTeams 可使用的 LLM API Key（配置在 AgentTeams 安装器中，本代码包不包含也不需要任何模型密钥）。

检查：

```bash
python3 --version
docker --version
```

## 2. 启动公证工具网关

在一个终端中启动服务，并保持它运行：

```bash
cd <PACKAGE_DIR>
python3 tools/notary_gateway.py --host 0.0.0.0 --port 18090
```

另开一个终端验证：

```bash
curl http://127.0.0.1:18090/health
curl http://127.0.0.1:18090/scenarios
curl -X POST http://127.0.0.1:18090/tools/qb_inhouse_fix/notary_change.get_issue \
  -H 'Content-Type: application/json' -d '{}'
```

这一步只验证宿主机本机访问。后面还需要验证 Docker 容器访问。

### 无 LLM 自检（可选，约 1 分钟）

```bash
python3 scripts/local_dryrun.py
```

该脚本用脚本化 Worker 输出驱动网关完整跑通两个场景：测试门禁、变异门禁（含等价变异体反驳复算）、惯例门禁均为现场真实执行。产物写入 `runs/` 并收集到 `evidence/sample_run/`。

## 3. 安装 AgentTeams

已安装可跳过。否则执行官方安装脚本并按引导完成语言、版本、LLM、API 联通性测试、Manager/Worker 运行时、端口等配置；Worker 运行时选择 `qwenpow`（`copow`/`QwenPaw`）。安装完成后检查：

```bash
docker ps | grep -E 'manager|controller'
```

打开 Element Web：`http://<AGENTTEAMS_HOST>:18088`（本机访问通常是 `http://127.0.0.1:18088`）。

## 4. 确定工具网关地址

Worker 在 Docker 容器中运行，不能直接用 `http://127.0.0.1:18090` 访问宿主机网关。单机 Docker 部署优先使用 manager 所在网络的 gateway 地址：

```bash
docker ps --format '{{.Names}}' | grep manager
docker inspect -f '{{range .NetworkSettings.Networks}}{{println .Gateway}}{{end}}' <manager 容器名>
```

假设输出是 `172.18.0.1`，则 `<NOTARY_TOOL_BASE_URL>` 使用：

```text
http://172.18.0.1:18090
```

从容器内验证：

```bash
docker exec -it <manager 容器名> curl http://172.18.0.1:18090/health
```

返回 `{"ok": true, ...}` 即后续 Worker 可访问网关。

## 5. 创建 Agent 和 Team

进入 Element Web 的 `manager` 房间。

打开 [create_agents_messages.md](create_agents_messages.md)，先把文件中的 `<NOTARY_TOOL_BASE_URL>` 全部替换为第 4 步确认的地址，然后将"复制到 Manager 的完整创建请求"整段发送给 `manager`。这段请求包含 10 个业务 Worker 和 1 个 Team 的完整定义，并明确要求：

1. 所有 Worker 使用 `qwenpow`（`copow`/`QwenPaw`）运行时。
2. `manager` 必须逐个串行创建 Worker，确认前一个正常运行后再创建下一个。
3. 创建 Team 时必须生成新的独立 Worker `codenotary-leader` 作为 TeamLeader，不能把任何业务 Worker 直接指定为 leader。

Worker 初始化会拉起运行时并写入依赖，低规格机器上并发创建可能造成高 I/O 阻塞。如果创建中途 Matrix 服务无响应，重启 controller 容器后从中断的 Step 继续即可，已创建的 Worker 不受影响。

注意：

- `manager` 只负责创建和管理；公证任务发给 Team 房间并 `@<team_leader_name>`。
- 10 个业务 Worker 的 AgentSpec、Skill 和工具契约已内联在创建消息中，Worker 不需要读取宿主机上的 `agents/...` 或 `skills/*/SKILL.md` 文件。
- `skills/*/SKILL.md` 主要用于评审追溯和后续 Registry 替换。

## 6. 发送公证任务

打开 [run_demo_task_message.md](run_demo_task_message.md)。进入名称以 `Team` 开头、对应 `codenotary` 的 Team 房间，先 `@<team_leader_name>` 再粘贴任务。必须逐个发送：等 `qb_inhouse_fix` 公证报告完整输出后再发 `qb_external_sloppy`。

任务消息只包含 issue 来源与 scenario_id。源码、复现、门禁执行、证据收集应由 Agent 通过公证工具网关主动获取。

## 7. 判断是否跑通

`qb_inhouse_fix`（green 路径）应包含：

| 检查项 | 期望信号 |
| --- | --- |
| 检疫 | 目标源码无命中，SCREENED |
| 契约 | frozen_hash（sha256）输出，状态 CONTRACTED |
| 盲测隔离 | author 上下文无盲测内容；tester 上下文无实现内容 |
| 测试门禁 | 基线 2 + 盲测 4 用例全绿 |
| 变异门禁 | 幸存者 M03（`> → !=`）经 author 举证等价后豁免，复算 1.00 |
| 终审与发布 | NOTARIZED → deploy 冒烟通过 → RELEASED |
| 沉淀与封印 | 新 Skill 注册成功；manifest.json 写入，全部产物 sha256 在册 |

`qb_external_sloppy`（red 路径）应包含：

| 检查项 | 期望信号 |
| --- | --- |
| 检疫 | 命中硬编码凭据（演示用假密钥）与 os.system，标 yellow 继续 |
| 测试门禁 | red：空 pop 返回 None，违背契约"抛出 IndexError('pop from empty mailbox')" |
| 惯例门禁 | red：bare except、作用域外文件 notes.md/clear()、安全模式，共 5 条 veto |
| 终态 | REJECTED；证据包完整封印 |

如果 Team 要求你人工提供源码、测试或门禁结果，可以提醒：

```text
请通过已配置的 HTTP 公证工具网关主动获取，不要让我人工收集证据。
```

## 后续替换点

| 当前内容 | 后续替换方向 |
| --- | --- |
| HTTP 公证工具网关 | 真实 MCP Server 或 Higress MCP 代理（工具契约已按 MCP 迁移对齐，见 `tools/tool_catalog.json` 的 future_mcp_mapping） |
| `scenarios/*.json` | 真实 issue / PR / 变更数据源 |
| `tools/notary_target/` | 真实目标仓库（门禁与变异执行逻辑不变） |
| `at/create_agents_messages.md` 中的内联 AgentSpec/Skill | Nacos AI Registry 中的 Prompt、Skill、AgentSpec、AgentTeam Spec |
| `skills/registry/` 本地注册表 | AgentTeams Skill Registry 或 Nacos AI Registry，按版本/标签动态加载 |
