# Vendored real-world code（D5 真实仓库样本）

本目录中 `d5a_*` / `d5b_*` / `d5c_*` 文件为 **pypa/packaging** 的真实源码逐字抽取，用于 D5 真实仓库评测样本。仅做的修改是 **import 扁平化**（`from .version import` → `from d5a_version import` 等），逻辑代码零改动。

## 出处与许可证

- 仓库：https://github.com/pypa/packaging
- 许可证：**Apache-2.0 OR BSD-2-Clause**（双许可，任选其一；本包按 Apache-2.0 使用，与本项目 LICENSE 一致）
- 版权：Copyright (c) Donald Stufft and individual contributors.

## 文件对照

| 样本 | 文件组 | 来源 commit | 说明 |
|---|---|---|---|
| D5-A（#300，local 段比较） | `d5a_specifiers.py`（修复前）、`d5a_version.py`、`d5a_utils.py`、`d5a_compat.py`、`d5a_typing.py`、`d5a_structures.py` | `db291c7^`（修复前） | 修复版 specifiers.py 内嵌于 `scenarios/d5_pkg_leq_local.json` 的 reference.implementation |
| D5-B（#673，epoch+前缀匹配） | `d5b_specifiers.py`（修复前）、`d5b_version.py`、`d5b_utils.py`（canonicalize_version 逐字子集）、`d5b_structures.py` | `a6c9bc4^` | 修复版内嵌于 `scenarios/d5_pkg_epoch_prefix.json` |
| D5-C（#100，~= 预发布段） | `d5c_specifiers.py`（修复前）、`d5c_version.py`、`d5c_utils.py`（逐字子集）、`d5c_structures.py`、`d5c_typing.py` | `7b2bb91^` | 修复版内嵌于 `scenarios/d5_pkg_compat_prerelease.json` |

基线测试 `test_d5{a,b,c}_baseline.py` 改编自对应 commit 的公开测试（仅保留跨修复稳定的公共行为用例）；盲测契约测试内嵌于各场景的 reference.tests，依据契约（PEP 440 条款 + issue 描述）独立撰写，不参考上游回归测试。
