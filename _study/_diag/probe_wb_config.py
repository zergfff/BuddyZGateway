# -*- coding: utf-8 -*-
"""
probe_wb_config.py — 探 WorkBuddy 的远端产品配置（含模型徽章/优惠信息）。

桌面端会把后端下发的 ProductConfiguration（字段：models / availableModels /
modelGroups / modelPromotions …）用于模型列表与徽章渲染。徽章来源两处：
  · models[].tags 里的 "badge:<标签>:<hex颜色>"
  · 顶层 modelPromotions（带时间调度 + 折扣语义）

本脚本用与桌面端**同源**的凭据做单次 GET，确认端点与字段。
安全：只打印结构/字段名与徽章文案，不打印任何令牌值。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO / "_study" / "codebuddy2openai"))

import converter as conv  # noqa: E402

CANDIDATES = [
    "/v3/config",
    "/v2/config",
    "/v1/config",
    "/config",
    "/v3/config/getDataPolicy",
    "/v3/product/config",
]


def main() -> int:
    import httpx
    af = conv.find_auth_file()
    print("auth 文件:", af)
    if af is None:
        print("✗ 未找到 WorkBuddy 登录凭证")
        return 1
    cred = conv.CredentialManager(af)
    backend = cred.get_backend()
    headers = cred.get_headers()
    print("backend:", backend)
    print("headers keys:", sorted(headers.keys()))
    print()

    found = None
    with httpx.Client(timeout=25) as c:
        for path in CANDIDATES:
            url = backend.rstrip("/") + path
            try:
                r = c.get(url, headers=headers)
            except Exception as e:  # noqa: BLE001
                print(f"{path:28s} ✗ {type(e).__name__}: {e}")
                continue
            body = r.text
            print(f"{path:28s} HTTP {r.status_code}  {len(body)} 字节")
            if r.status_code != 200:
                print(f"      {body[:160]}")
                continue
            try:
                d = r.json()
            except Exception:
                print(f"      非 JSON: {body[:160]}")
                continue
            keys = list(d.keys()) if isinstance(d, dict) else f"(list len={len(d)})"
            print(f"      顶层键: {keys}")
            # 找模型相关字段
            for k in ("models", "availableModels", "modelGroups", "modelPromotions"):
                if isinstance(d, dict) and k in d:
                    v = d[k]
                    n = len(v) if isinstance(v, (list, dict)) else "?"
                    print(f"      ★ 命中 {k}（{type(v).__name__}, 数量 {n}）")
                    if not found:
                        found = (path, d)
            if found:
                break

    if not found:
        print("\n✗ 没有候选端点返回含模型字段的配置")
        return 1

    path, d = found
    print(f"\n===== 用 {path} 的结构分析 =====")
    models = d.get("models")
    if isinstance(models, list) and models:
        m0 = models[0]
        print(f"models[0] 字段: {list(m0.keys()) if isinstance(m0, dict) else type(m0)}")
        # 找带 badge 标签的模型
        tagged = [m for m in models if isinstance(m, dict) and m.get("tags")]
        print(f"带 tags 的模型: {len(tagged)}/{len(models)}")
        for m in tagged[:6]:
            tags = m.get("tags") or []
            badges = [t for t in tags if isinstance(t, str) and t.startswith("badge:")]
            print(f"   {m.get('id') or m.get('name') or m.get('model')!r}  tags={tags[:6]}")
        # 全部 badge 文案
        allb = set()
        for m in models:
            for t in (m.get("tags") or []) if isinstance(m, dict) else []:
                if isinstance(t, str) and t.startswith("badge:"):
                    rest = t[6:]
                    i = rest.rfind(":")
                    allb.add(rest[:i].strip() if i > 0 else rest)
        print(f"徽章文案集合: {sorted(allb)}")

    promo = d.get("modelPromotions")
    if promo is not None:
        print(f"\nmodelPromotions: {type(promo).__name__} 数量 "
              f"{len(promo) if isinstance(promo,(list,dict)) else '?'}")
        if isinstance(promo, list) and promo:
            print("  [0] 字段:", list(promo[0].keys()) if isinstance(promo[0], dict) else promo[0])
            print("  [0] 内容:", json.dumps(promo[0], ensure_ascii=False)[:700])
        elif isinstance(promo, dict):
            k0 = list(promo.keys())[:3]
            print("  键样本:", k0)
            for k in k0:
                print(f"   {k}: {json.dumps(promo[k], ensure_ascii=False)[:400]}")

    for k in ("modelGroups", "availableModels"):
        v = d.get(k)
        if v is not None:
            print(f"\n{k}: {type(v).__name__} "
                  f"{len(v) if isinstance(v,(list,dict)) else ''}")
            print("  ", json.dumps(v, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
