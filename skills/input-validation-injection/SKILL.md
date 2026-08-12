---
name: input-validation-injection
description: 输入校验与注入防护 SOP——追踪外部输入到 SQL/shell/路径/反序列化等 sink，逐路径确认校验与转义；并排查硬编码凭据。发现可达注入路径或明文密钥一律 red，无 yellow 折中。
---

# 输入校验与注入防护 SOP

AI 生成代码默认「输入是善意的」，且训练语料里充斥着教学式的危险写法
（字符串拼 SQL、`shell=True`、明文密钥）。安全缺陷杀伤最大，本 SOP 是
sentinel（入口把关）与评审角色的标准装备，并直接支撑合规叙事。

## 识别信号

- 字符串拼接（f-string / `%` / `.format` / `+`）进入 SQL、shell 命令、文件路径、HTML。
- `subprocess`/`os.system` 使用 `shell=True`，或命令中含外部输入。
- `eval` / `exec` / `pickle.loads` / `yaml.load`（非 SafeLoader）处理外部数据。
- 路径拼接未防 `..` 与绝对路径覆盖（`os.path.join(base, user_input)` 可被绝对路径穿透）。
- **硬编码凭据**：代码中出现 `api_key = "`、`password = "`、`sk-` 前缀串、
  `BEGIN ... PRIVATE KEY`、JWT 字面量等疑似密钥。

## 定位步骤

1. 列出全部外部输入入口：HTTP 参数、文件内容、环境变量、消息队列、CLI 参数、第三方回调。
2. 从每个入口沿数据流追踪到全部 sink：DB 查询、shell、文件系统、反序列化、模板渲染。
3. 逐条「入口→sink」路径确认：有无校验（白名单优先）、有无转义/参数化。
4. 全仓扫描硬编码凭据模式；对疑似项确认是否为真实密钥（占位符/示例除外）。
5. 发现**可达**的注入路径或真实明文密钥 → 直接 red，不存在 yellow 折中。

## 修复模式

- SQL 一律参数化查询，禁止任何拼接路径（包括「我自己转义过了」）。
- shell 调用改 `shell=False` + 参数数组；输入必须进命令时用白名单校验。
- 路径：规范化（`realpath`）后断言仍在允许目录内。
- 反序列化：`pickle` 禁用于外部数据；YAML 用 `safe_load`；JSON 为首选。
- 凭据移入环境变量/密钥管理服务；**已提交进 git 的密钥视为已泄露，必须轮换**，不能只是删除。

## 验证方式

1. 为每条曾可达的注入路径构造载荷用例（`'; DROP TABLE--`、`../../../etc/passwd`、`$(id)`），确认被拦截。
2. 凭据扫描（模式匹配 + 历史 `git log -p` 抽查）无命中。
3. 全量回归测试通过。
