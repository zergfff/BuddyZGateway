# -*- coding: utf-8 -*-
"""
verify_welfare_live.py — 真机验证「自动领福利」链路。

对每个有福利接口的通道各跑一次真实领取：
  cb (WorkBuddy)  → 查签到状态 + 每日签到
  mc (MonkeyCode) → 查签到状态 + 签到（含验证码）
  ca (CodeArts)   → 每日福利（上游幂等，重复调用仍返回 0000）

安全：这些都是**各自的官方客户端每天本来就在做**的调用；
接口幂等，重复调用不会重复发放，也不会触发风控。

用法：
    python verify_welfare_live.py                 # 全部
    python verify_welfare_live.py --only cb
    python verify_welfare_live.py --status        # 只查状态，不领取
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
for sub in ("codebuddy2openai", "monkeycode2openai", "codearts2openai"):
    sys.path.insert(0, str(REPO / "_study" / sub))

PASS = 0
FAIL = 0
SKIP = 0


# 凭据/会话类问题算 SKIP（是账号状态，不是代码链路问题）
# 归为 SKIP 的情况：账号状态 / 上游活动状态问题，不是代码链路问题
_AUTH_HINTS = ("会话已失效", "未登录", "无凭证", "请重新登录", "refresh_token",
               "401", "403", "授权",
               # 上游明确没有该活动（如 WorkBuddy 国际版未开启签到）
               "活动未开启", "活动已过期", "活动已结束", "10001", "10002")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL, SKIP
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    elif any(k in detail for k in _AUTH_HINTS):
        SKIP += 1
        print(f"  ○ {name} (SKIP: 凭据/会话问题，需重新登录)  {detail[:110]}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["cb", "mc", "ca"], default=None)
    ap.add_argument("--status", action="store_true", help="只查状态，不领取")
    a = ap.parse_args()
    want = [a.only] if a.only else ["cb", "mc", "ca"]

    print("== 每日福利真机验证 ==")

    if "cb" in want:
        print("\n===== WorkBuddy (cb) =====")
        import converter as conv
        for st in ("intl", "cn"):
            af = conv.find_auth_file(st)
            if af is None:
                print(f"  [{st}] 未登录，跳过")
                continue
            t0 = time.monotonic()
            s = conv.checkin_status_for_station(st)
            print(f"  [{st}] 状态: ok={s.get('ok')} "
                  f"今日已签={s.get('today_checked_in')} "
                  f"连续={s.get('streak_days')} "
                  f"今日积分={s.get('today_credit')} "
                  f"({time.monotonic()-t0:.1f}s)")
            if not s.get("ok"):
                check(f"[{st}] 状态查询", False, str(s.get("error"))[:160])
                continue
            check(f"[{st}] 状态查询成功", True)
            if a.status:
                continue
            t0 = time.monotonic()
            r = conv.claim_for_station(st)
            el = time.monotonic() - t0
            print(f"  [{st}] 领取: ok={r.get('ok')} already={r.get('already')} "
                  f"credit={r.get('credit')} err={r.get('error') or '-'} ({el:.1f}s)")
            check(f"[{st}] 领取成功或已领", bool(r.get("ok")), str(r.get("error"))[:160])

    if "mc" in want:
        print("\n===== MonkeyCode (mc) =====")
        import mc_saas
        t0 = time.monotonic()
        s = mc_saas.checkin_state(None)
        print(f"  状态: ok={s.get('ok')} data={s.get('data')} "
              f"err={s.get('error') or '-'} ({time.monotonic()-t0:.1f}s)")
        if not s.get("ok"):
            check("mc 状态查询", False, str(s.get("error"))[:160])
        else:
            check("mc 状态查询成功", True)
            if not a.status:
                t0 = time.monotonic()
                r = mc_saas.claim_for_station(None)
                el = time.monotonic() - t0
                print(f"  领取: ok={r.get('ok')} already={r.get('already')} "
                      f"data={r.get('data')} err={r.get('error') or '-'} ({el:.1f}s)")
                check("mc 领取成功或已签", bool(r.get("ok")), str(r.get("error"))[:160])

    if "ca" in want:
        print("\n===== 华为云 CodeArts (ca) =====")
        import codearts2openai as ca
        if a.status:
            print("  （--status：跳过领取）")
        else:
            t0 = time.monotonic()
            r = ca.claim_daily()
            el = time.monotonic() - t0
            print(f"  领取: ok={r.get('ok')} data={r.get('data')} "
                  f"err={r.get('error') or '-'} ({el:.1f}s)")
            check("ca 领取成功", bool(r.get("ok")), str(r.get("error"))[:200])

    print(f"\n=====  PASS {PASS}  FAIL {FAIL}  SKIP {SKIP}  =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
