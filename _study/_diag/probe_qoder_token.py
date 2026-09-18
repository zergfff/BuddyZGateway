# -*- coding: utf-8 -*-
"""
实验 3：IDE 解出来的 token 能不能直接喂给 CLI？

    QODER_PERSONAL_ACCESS_TOKEN=<IDE token>  →  Qoder CLI

只发一条极短指令（"OK"），单次请求，用于判定可行性。
安全：不打印任何令牌值。

用法：
    python probe_qoder_token.py            # 两版都试
    python probe_qoder_token.py --station intl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO / "_study" / "qoder2openai"))
sys.path.insert(0, str(REPO / "_study" / "_diag"))

import qoder2openai as q                    # noqa: E402
from probe_qoder_auth2 import get_master_key, aes_gcm_decrypt   # noqa: E402


def read_ide_auth(user_data_dir: Path) -> dict | None:
    try:
        key = get_master_key(user_data_dir)
        plain = aes_gcm_decrypt(key, (user_data_dir / "auth.v1.dat").read_bytes())
        return json.loads(plain.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ 读取失败: {type(e).__name__}: {e}")
        return None


STATION_UDD = {
    "intl": "com.qoder.app.stable",
    "cn": "com.qodercn.app.stable",
}


async def try_station(station: str) -> None:
    meta = q.STATIONS[station]
    print(f"\n===== {meta['label']} ({station}) =====")
    appdata = Path(os.environ["APPDATA"])
    udd = appdata / STATION_UDD[station]

    d = read_ide_auth(udd)
    if not d:
        return
    tok = d.get("token") or ""
    print(f"  IDE 里: token len={len(tok)} · expiresAt={d.get('expiresAt')}"
          f" · user={d.get('user', {}).get('name', '')}")

    # CLI 定位
    info = q.detect_cli(station)
    print(f"  CLI: found={info['found']} ({info.get('hint','')})")
    if not info["found"]:
        return

    for label, pat in (("IDE token", tok),
                       ("IDE refreshToken", d.get("refreshToken") or "")):
        if not pat:
            continue
        q.apply_config({"station": station, "cli_path": info["path"],
                        "pat": pat, "timeout_s": 90})
        print(f"\n  --- 用 {label} 当访问令牌试一次 ---")
        res = await q.probe(log_fn=lambda m: None)
        print(f"      ok={res['ok']} 耗时={res['elapsed']:.1f}s")
        if res["ok"]:
            print(f"      ✓✓ 成功！返回: {res['text'][:80]!r}")
            print(f"      usage={res['usage']}")
            return
        print(f"      失败: {res['error'][:220]}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", choices=["intl", "cn", "both"], default="both")
    a = ap.parse_args()
    print("== 实验：IDE token 能否直接喂给 CLI ==")
    print("（每站最多 2 次极短请求，用于判定可行性）")
    for st in (["intl", "cn"] if a.station == "both" else [a.station]):
        await try_station(st)
    q.apply_config({"pat": "", "cli_path": ""})
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
