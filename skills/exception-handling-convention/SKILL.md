---
name: exception-handling-convention
description: 异常处理规范 SOP——禁止 bare except、强制 raise from 异常链、按具体异常类型捕获，保证错误可诊断可追溯。
---

# 异常处理规范 SOP

糟糕的异常处理会吞掉错误、切断堆栈、掩盖根因。本 SOP 给出统一规范。

## 识别信号

- 代码中存在 `except:` 或 `except Exception:` 且块内仅 `pass` 或只打日志。
- 抛出异常时没有保留原始异常（丢失 `__cause__`）。
- 捕获范围远大于实际可能抛出的异常类型。
- 异常被捕获后又原样重抛，捕获块没有任何增量价值。

## 定位步骤

1. 全局搜索 `except:`、`except Exception`、`except BaseException`，逐个审查。
2. 检查每个 `raise` 是否发生在 `except` 块内——若是，确认是否用了 `raise ... from e`。
3. 检查日志中是否只有「出错了」而没有堆栈（应使用 `log.exception` 或带 `exc_info`）。
4. 确认被吞异常的场景是否有明确的、写在注释里的理由。

## 修复模式

- 禁止 bare except：永远捕获具体异常类型（如 `except (OSError, ValueError):`）。
- 转换异常时必须链接：`raise ConfigError("invalid config") from e`，保留根因链。
- 需要重抛时用裸 `raise`，不要 `raise e`（会重置堆栈起点）。
- 吞异常必须同时满足：有注释说明理由 + 记录日志 + 不影响上层正确性判断。
- 资源清理由 `finally` 或上下文管理器承担，不依赖异常是否发生。

## 验证方式

1. 构造会触发异常的用例，确认上层能看到完整异常链（`__cause__` 非空）。
2. 日志中出现完整堆栈而非单行消息。
3. 全量回归测试通过；静态检查（如 ruff 的 BLE001/E722 规则）无新增告警。
