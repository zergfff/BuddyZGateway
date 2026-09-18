# -*- coding: utf-8 -*-
"""
verify_qoder_live.py — Qoder 通道真机验证。

分两段：
  A. **零额度**：`--list-models` 验全自动鉴权链路（自动读桌面端登录态 → 铸 jobToken）
  B. **一次极小对话**：真正跑通 `stream-json` → 拿回模型回复（每站 1 次，约 5~15s）

用法：
    python verify_qoder_live.py                 # 两站，A+B
    python verify_qoder_live.py --no-chat       # 只做零额度那段
    python verify_qoder_live.py --station intl
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
sys.path.insert(0, str(STUDY / "qoder2openai"))

import qoder2openai as q  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


async def verify_station(station: str, do_chat: bool) -> None:
    meta = q.STATIONS[station]
    print(f"\n===== {meta['label']} ({station}) =====")

    # --- 凭据（自动读桌面端）---
    cs = q.credential_status(station)
    check("读到桌面端登录态", cs.get("ide_found") is True, str(cs)[:160])
    if not cs.get("ide_found"):
        print(f"      ({meta['label']} 桌面端未登录，跳过)")
        return
    who = cs.get("email") or cs.get("phone") or cs.get("account")
    print(f"      账号 = {who} · 到期 {cs.get('ide_expires', '')[:10]}")

    # --- CLI ---
    h = q.cli_health_for(station)
    check("定位到 CLI", h.get("cli_found") is True, h.get("error", "")[:160])
    if not h.get("cli_found"):
        return
    print(f"      CLI = {h['cli_source']} · node {h.get('node_version', '')}")

    # --- 全自动铸 jobToken ---
    q.apply_config({"station": station, "cli_path": "", "pat": ""})
    try:
        cred = await asyncio.to_thread(q.get_job_credential, station, True)
    except Exception as e:  # noqa: BLE001
        check("铸 jobToken", False, f"{type(e).__name__}: {e}")
        return
    check("铸出 JobTokenCredential", bool(cred and cred.get("token")), str(cred)[:120])
    if not cred:
        return
    print(f"      jobToken len={len(cred['token'])} · expires_at={cred.get('expires_at')}")

    # --- A. 零额度：--list-models ---
    t0 = time.monotonic()
    res = await q.probe()
    el = time.monotonic() - t0
    check("probe 通过（零额度）", res["ok"] is True, res.get("error", "")[:220])
    if res["ok"]:
        print(f"      耗时 {el:.1f}s · 拿到 {len(res['models'])} 个模型")
        print(f"      {', '.join(res['models'][:8])} …")
        check("模型数与内置表接近",
              abs(len(res["models"]) - len(meta["models"])) <= 3,
              f"{len(res['models'])} vs {len(meta['models'])}")

    # --- B. 真实对话 ---
    if not do_chat:
        return
    print("      --- 一次极小真实对话 ---")
    t0 = time.monotonic()
    try:
        text, usage, _ = await q._run_cli_once("Auto", '{"type":"user","message":'
                                               '{"role":"user","content":'
                                               '[{"type":"text","text":"回答 OK 两个字"}]},'
                                               '"parent_tool_use_id":null}')
        el = time.monotonic() - t0
        check("对话返回非空", bool(text.strip()), repr(text[:120]))
        check("没挂死（<90s）", el < 90, f"{el:.1f}s")
        print(f"      耗时 {el:.1f}s · usage={usage or '(无)'}")
        print(f"      回复 = {text.strip()[:160]!r}")
    except Exception as e:  # noqa: BLE001
        check("真实对话", False, f"{type(e).__name__}: {e}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", choices=["intl", "cn", "both"], default="both")
    ap.add_argument("--no-chat", action="store_true", help="只做零额度那段")
    a = ap.parse_args()

    print("== Qoder 通道真机验证 ==")
    import shutil
    check("本机有 node", bool(shutil.which("node")), "需要 Node 运行 Qoder CLI")

    sts = ["intl", "cn"] if a.station == "both" else [a.station]
    for st in sts:
        await verify_station(st, not a.no_chat)

    q.apply_config({"station": "intl", "cli_path": "", "pat": ""})
    print(f"\n=====  PASS {PASS}  FAIL {FAIL}  =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
