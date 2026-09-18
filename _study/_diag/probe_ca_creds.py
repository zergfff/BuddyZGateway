# -*- coding: utf-8 -*-
"""
probe_ca_creds.py — 诊断 CodeArts 各凭据来源的可用性（只读，不改状态）。

来源清单：
  ① data-state     网关自己保存的 DPoP 会话（授权流→STS 后落盘）
  ② Temp ca_state.json  早期轮转文件
  ③ 凭证库（桌面端 / VS Code 插件）的 refresh_token + dpop 密钥
  ④ 凭证库里的「免轮转」直连临时凭证（_store_ticket_creds）
安全：不打印任何令牌值。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO / "_study" / "codearts2openai"))

# ⚠ 模块靠 BUDDYZ_DATA_DIR 定位 state 文件；不设的话 _load_state() 永远返回 {}
# 且 _save_state() 静默不写（这正是历史上"会话存不下来"的隐藏原因之一）。
import os as _os
if not _os.environ.get("BUDDYZ_DATA_DIR"):
    _d = Path(_os.environ.get("LOCALAPPDATA", "")) / "BuddyZGateway" / "data"
    _os.environ["BUDDYZ_DATA_DIR"] = str(_d)
    print(f"[提示] 自动设置 BUDDYZ_DATA_DIR={_d}")

import codearts2openai as ca  # noqa: E402


def main() -> int:
    print("== CodeArts 凭据来源诊断 ==")

    print("\n[1] 网关自己的 data-state")
    st = ca._load_state()
    print(f"   顶层键: {list(st.keys())}")
    print(f"   refresh_token: {'有' if st.get('refresh_token') else '无'}"
          f" | dpop_priv: {'有' if st.get('dpop_priv') else '无'}")
    fp = ca._state_file()
    if fp and fp.is_file():
        print(f"   文件: {fp}")
        print(f"   大小 {fp.stat().st_size}B · mtime "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(fp.stat().st_mtime))}")
        print(f"   有备份: {(fp.with_suffix('.bak')).is_file()}")

    print("\n[2] Temp ca_state.json（早期轮转文件）")
    tmp = Path(ca.os.environ.get("LOCALAPPDATA", "")) / "Temp" / "ca_state.json"
    if tmp.is_file():
        try:
            tj = json.loads(tmp.read_text(encoding="utf-8"))
            print(f"   存在，键: {list(tj.keys())} · "
                  f"refresh_token: {'有' if tj.get('refresh_token') else '无'}")
        except Exception as e:  # noqa: BLE001
            print(f"   存在但解析失败: {e}")
    else:
        print("   不存在")

    print("\n[3] 凭证库（桌面端 / VS Code 插件）")
    stores = ca._bootstrap_all()
    if not stores:
        print("   没扫到任何凭证库")
    for label, sess in stores:
        rt = bool(sess.get("refresh_token"))
        priv = bool(sess.get("dpop_priv"))
        print(f"   {label:46s} refresh_token={'有' if rt else '无'} "
              f"dpop_priv={'有' if priv else '无'} "
              f"station={sess.get('station')}")

    print("\n[4] 免轮转直连临时凭证（_store_ticket_creds）")
    try:
        tkc = ca._store_ticket_creds()
        if tkc:
            print(f"   可用 ✓ 键: {list(tkc.keys())}")
            print(f"   expiration: {tkc.get('expiration')}")
        else:
            print("   不可用")
    except Exception as e:  # noqa: BLE001
        print(f"   异常: {type(e).__name__}: {e}")

    print("\n[5] 完整候选链（_dpop_candidates）")
    cands = ca._dpop_candidates()
    print(f"   {len(cands)} 个候选")
    for c in cands:
        print(f"   · {c.get('label')}")

    print("\n[6] ensure_creds 实测")
    for prefer in ("ticket", "dpop"):
        t0 = time.monotonic()
        try:
            cred = ca.ensure_creds(prefer=prefer)
            print(f"   prefer={prefer:7s} ✓ 成功（{time.monotonic()-t0:.1f}s）"
                  f" expiration={cred.get('expiration')} station={cred.get('station')}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"   prefer={prefer:7s} ✗ {str(e)[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
