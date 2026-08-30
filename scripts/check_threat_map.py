#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_threat_map.py — 威胁模型映射的机器校验（独立工具，不在六脚本评测链内）。

校验 tools/openclaw_threat_map.json 中引用的每一个防线锚点真实存在：
  sentinel_pattern  → 网关 _SENTINEL_PATTERNS 中有同名 label
  convention_rule   → 网关源码中有同名规则
  role_policy       → ROLE_POLICY 非空
  tool              → TOOLS 注册表中有该端点
  skill             → match_table 决策表或种子/registry 中有该 Skill
  source_marker     → 网关源码中存在该标记字符串
  file              → 包内文件存在（expect_absent=true 则必须不存在；expect_keys 校验 JSON 键）
  roadmap           → 映射文件自身包含该扩展候选节
  evidence          → 引用的场景样本文件存在

用法：python3 scripts/check_threat_map.py   （包根或任意目录运行均可）
退出码 0 = 全部锚点通过；非 0 = 有失效锚点（fail-closed）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = PKG_ROOT / "tools" / "openclaw_threat_map.json"


def load_gateway():
    spec = importlib.util.spec_from_file_location(
        "notary_gateway", PKG_ROOT / "tools" / "notary_gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    errors: list[str] = []
    checks = 0

    if not MAP_PATH.exists():
        print(f"FAIL: {MAP_PATH} not found")
        return 2
    tmap = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    gw = load_gateway()
    gw_source = (PKG_ROOT / "tools" / "notary_gateway.py").read_text(
        encoding="utf-8")
    sentinel_labels = {label for _p, _s, label in gw._SENTINEL_PATTERNS}
    convention_rules = set((PKG_ROOT / "notary.json").exists() and
                           json.loads((PKG_ROOT / "notary.json")
                                      .read_text(encoding="utf-8"))
                           .get("convention", {}).get("rules", {}).keys())
    match_table = json.loads(
        (PKG_ROOT / "skills" / "match_table.json").read_text(encoding="utf-8"))
    skill_names = {e["skill"] for e in match_table["signals"]}
    skill_names.update(p.name for p in (PKG_ROOT / "skills").iterdir()
                       if p.is_dir())
    registry = PKG_ROOT / "skills" / "registry"
    if registry.exists():
        skill_names.update(p.name for p in registry.iterdir() if p.is_dir())
        skill_names.update(p.stem for p in registry.glob("*.md"))

    def err(msg: str) -> None:
        errors.append(msg)
        print(f"  FAIL: {msg}")

    for cat in tmap["categories"]:
        cid = cat["id"]
        for layer in cat["codenotary_layers"]:
            checks += 1
            kind, ref = layer["kind"], layer["ref"]
            if kind == "sentinel_pattern":
                if ref not in sentinel_labels:
                    err(f"[{cid}] sentinel pattern not found: {ref!r}")
            elif kind == "convention_rule":
                if ref not in convention_rules and ref not in gw_source:
                    err(f"[{cid}] convention rule not found: {ref!r}")
            elif kind == "role_policy":
                if not getattr(gw, "ROLE_POLICY", None):
                    err(f"[{cid}] ROLE_POLICY missing or empty")
            elif kind == "tool":
                if ref not in gw.TOOLS:
                    err(f"[{cid}] tool not registered: {ref!r}")
            elif kind == "skill":
                if ref not in skill_names:
                    err(f"[{cid}] skill not found: {ref!r}")
            elif kind == "source_marker":
                if ref not in gw_source:
                    err(f"[{cid}] source marker not in gateway: {ref!r}")
            elif kind == "file":
                p = PKG_ROOT / ref
                if layer.get("expect_absent"):
                    if p.exists():
                        err(f"[{cid}] file must be absent but exists: {ref}")
                elif not p.exists():
                    err(f"[{cid}] file not found: {ref}")
                elif layer.get("expect_keys"):
                    data = json.loads(p.read_text(encoding="utf-8"))
                    for k in layer["expect_keys"]:
                        if k not in data:
                            err(f"[{cid}] {ref} missing key {k!r}")
            elif kind == "roadmap":
                if ref not in tmap:
                    err(f"[{cid}] roadmap section missing: {ref!r}")
            else:
                err(f"[{cid}] unknown layer kind: {kind!r}")
            for sid in layer.get("evidence", []):
                checks += 1
                if not (PKG_ROOT / "scenarios" / f"{sid}.json").exists():
                    err(f"[{cid}] evidence scenario not found: {sid}")

    cand = tmap.get("sentinel_extension_candidates", {})
    if cand.get("status") == "validated-frozen":
        checks += 1
        existing = {label for _p, _s, label in gw._SENTINEL_PATTERNS}
        overlap = existing & {r["label"] for r in cand.get("rules", [])}
        if overlap:
            err(f"frozen candidate rules already active (freeze violated): "
                f"{sorted(overlap)}")

    print(f"threat-map check: {checks} anchors, {len(errors)} failures")
    if errors:
        return 1
    print("THREAT MAP VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
