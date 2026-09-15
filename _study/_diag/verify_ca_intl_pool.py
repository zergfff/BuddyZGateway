# -*- coding: utf-8 -*-
"""verify_ca_intl_pool — CodeArts 国际站 + 号池回归。

背景
----
1. 国际站：插件按 LOGIN_STATION_KEY（china | international）切站点。
   对话域：国内 snap-access.cn-north-4 vs 国际 snap-access.ap-southeast-1；
   STS（续期）与 OPENGW（余额/福利）国内外一致。证据见插件
   product.json commercialVersionDomain.HKFramework vs newFramework。
2. 号池：每条 = 一套独立 DPoP 会话，轮转 + 401/403 停用 + 429/5xx 冷却，
   对标 MonkeyCode 语义。

用法：
    python verify_ca_intl_pool.py           # 离线：站点判定 + 号池机制
    python verify_ca_intl_pool.py --live    # 另做只读 live（ticket 链对话+余额，
                                            # 不消耗单次 refresh_token）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CA_DIR = REPO / "_study" / "codearts2openai"


def load_ca():
    sys.path.insert(0, str(CA_DIR))
    import codearts2openai as ca
    return ca


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="只读 live 验证（不消耗 refresh_token）")
    args = ap.parse_args()

    tmp_state = Path(tempfile.mkdtemp(prefix="ca_verify_"))
    os.environ["BUDDYZ_DATA_DIR"] = str(tmp_state)
    ok = True
    try:
        ca = load_ca()

        print("=== 1) 站点常量 ===")
        checks = {
            "国内对话域 cn-north-4": ca.SNAP_CN == "https://snap-access.cn-north-4.myhuaweicloud.com",
            "国际对话域 ap-southeast-1": ca.SNAP_INTL == "https://snap-access.ap-southeast-1.myhuaweicloud.com",
            "STS 国内外一致": "sts.cn-north-4" in ca.STS,
            "OPENGW 生产一致": ca.OPENGW == "https://opengw.developer.huaweicloud.com",
            "国际 portal": ca.PORTAL_INTL == "https://codearts.ap-southeast-1.huaweicloud.com",
        }
        for k, v in checks.items():
            print(("  ✓ " if v else "  ✗ ") + k)
            ok &= v

        print("\n=== 2) 本机站点判定 ===")
        st = ca._current_station()
        print(f"  当前判定: {st}")
        stores = [(s["label"], ca._store_station(s)) for s in ca._session_stores()]
        for label, sst in stores:
            print(f"  · {label} → {sst}")
        print(f"  _snap() → {ca._snap()}")
        print(f"  _portal() → {ca._portal()}")
        snap_ok = (st == "international" and "ap-southeast-1" in ca._snap()) or (
            st == "china" and "cn-north-4" in ca._snap())
        print(("  ✓ " if snap_ok else "  ✗ ") + "对话域跟随站点判定")
        ok &= snap_ok

        print("\n=== 3) 授权 URL 跟随站点 ===")
        info = ca.build_authorize_url(9100)
        url_ok = ca._portal().replace("https://", "") in info["url"]
        print(f"  station={info.get('station')} host_ok={url_ok}")
        print(("  ✓ " if url_ok else "  ✗ ") + "授权 URL 站点正确")
        ok &= url_ok

        print("\n=== 4) 号池收录/开关/删除 ===")
        res = ca.pool_harvest()
        print(f"  harvest: added={res['added']} skipped={res['skipped']}")
        entries = ca.pool_list()
        print(f"  条目数: {len(entries)}")
        harvest_ok = len(entries) >= 1
        print(("  ✓ " if harvest_ok else "  ✗ ") + "本机登录已收录进池")
        ok &= harvest_ok
        # 再收一次必须去重
        res2 = ca.pool_harvest()
        dedup_ok = not res2["added"] and len(ca.pool_list()) == len(entries)
        print(("  ✓ " if dedup_ok else "  ✗ ") + f"重复收录去重（added={res2['added']}）")
        ok &= dedup_ok
        t1 = ca.pool_toggle(0)
        t2 = ca.pool_toggle(0)
        toggle_ok = t1 is False and t2 is True
        print(("  ✓ " if toggle_ok else "  ✗ ") + "启用/停用切换")
        ok &= toggle_ok
        print(f"  pool_remove(99) → {ca.pool_remove(99)}（应 False）")
        ok &= ca.pool_remove(99) is False

        print("\n=== 5) 轮转/冷却语义（纯本地，不联网） ===")
        ca.CONFIG["pool_enabled"] = True
        eng = ca._pool_engine()
        eng.reset_stats()
        # 阈值设为 1，便于用一次失败就观察熔断；重试上限放大，看完整候选
        ca.pool_set_config(allowed_fails=1, cooldown_base=30, k_429=1.0,
                           k_5xx=1.0, off_recheck=3600, max_retries=9)
        n_enabled = len([e for e in entries if e.get("enabled", True)])
        order = eng.order()
        order_ok = len(order) == n_enabled
        print(("  ✓ " if order_ok else "  ✗ ") + f"健康候选 {len(order)}/{n_enabled} 条")
        ok &= order_ok

        ca._pool_fail(0, 429)
        cool_ok = not any(i == 0 for i, _e in eng.order())
        snap0 = {s["idx"]: s for s in eng.snapshot()}
        print(("  ✓ " if cool_ok else "  ✗ ") +
              f"达阈值失败后该条被冷却跳过（{snap0[0]['state']} "
              f"{snap0[0]['cool_remain']:.0f}s）")
        ok &= cool_ok

        ca.pool_clear_cooldowns()
        uncool_ok = any(i == 0 for i, _e in eng.order())
        print(("  ✓ " if uncool_ok else "  ✗ ") + "清空冷却后恢复入池")
        ok &= uncool_ok

        ca._pool_fail(0, 401)
        off_ok = not any(i == 0 for i, _e in eng.order())
        snap1 = {s["idx"]: s for s in eng.snapshot()}
        print(("  ✓ " if off_ok else "  ✗ ") +
              f"401 后该条停用（{snap1[0]['state']}）")
        ok &= off_ok

        ca.pool_clear_cooldowns()
        ps = ca._pool_state()
        ps_ok = (ps.get("enabled") is True and len(ps.get("keys") or []) >= 1
                 and isinstance(ps.get("summary"), dict)
                 and ps["keys"][0].get("state") == "ok")
        print(("  ✓ " if ps_ok else "  ✗ ") +
              f"_pool_state: summary={ps.get('summary')} 策略={ps.get('strategy')}")
        ok &= ps_ok
        ca.CONFIG["pool_enabled"] = False

        print("\n=== 6) refresh 状态码解析 ===")
        for msg, want in [("refresh 失败(401): xxx", 401), ("refresh 失败(500): y", 500),
                          ("session 无效", None)]:
            got = ca._refresh_status(RuntimeError(msg))
            good = got == want
            print(("  ✓ " if good else "  ✗ ") + f"{msg!r} → {got}")
            ok &= good

        if args.live:
            print("\n=== 7) live 只读验证（ticket 链，不消耗 refresh_token） ===")
            try:
                cred = ca.ensure_creds(prefer="ticket")
                print(f"  cred station={cred.get('station')} expires={cred.get('expiration')}")
                url, headers, raw = ca._chat_prep(
                    "GLM-5.2", [{"role": "user", "content": "hi"}], 60, False)
                print(f"  chat url={url}")
                import requests as _rq
                r = _rq.post(url, data=raw, headers=headers, timeout=120)
                chat_ok = r.status_code == 200
                print(("  ✓ " if chat_ok else "  ✗ ") + f"chat HTTP {r.status_code}")
                ok &= chat_ok
                import httpx
                burl = ca.OPENGW + "/api/v1/user/tokens/balance"
                base = {"X-Security-Token": cred["security_token"],
                        "Content-Type": "application/json"}
                hdrs = ca._sign(cred["access_key_id"], cred["secret_access_key"],
                                "GET", burl, base, b"")
                with httpx.Client(timeout=20) as c:
                    rb = c.get(burl, headers=hdrs)
                body = rb.json()
                bal_ok = str(body.get("error_code")) == "0000"
                print(("  ✓ " if bal_ok else "  ✗ ") +
                      f"balance error_code={body.get('error_code')}")
                ok &= bal_ok
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ live 失败：{e}")
                ok = False
    finally:
        shutil.rmtree(tmp_state, ignore_errors=True)

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
