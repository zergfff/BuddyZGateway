# -*- coding: utf-8 -*-
"""verify_ca_pool_lock — 号池「开了就慢/不回复」两个根因的回归。

背景
----
开号池后请求变慢甚至不回复，是两个 bug 叠加：

① **自死锁**：`ensure_creds()` 外层已 `_locked_state_update` 持锁，池路径里的
   `_pool_save_back()` 又调一次 `_locked_state_update`。msvcrt.LK_LOCK 是阻塞
   式的，同进程对同一 lock 文件区域二次加锁 = 自己等自己 → 永久挂死。
   → 修法：`_locked_state_update` 用线程本地计数做成**可重入**。

② **每请求都打 STS**：池路径无条件 `_refresh_candidate()`，每个请求一次 STS
   网络往返 + refresh_token 单次轮转，请求一多互相撞「已使用」。
   → 修法：`_POOL_CRED` 内存缓存，凭据没到 `_need_refresh` 就直接用。

本脚本**不碰真实 refresh_token**（monkeypatch 掉 `_refresh_candidate`），
只验证锁语义与缓存行为。

用法：
    python verify_ca_pool_lock.py
    python verify_ca_pool_lock.py --repro-old   # 复现修复前的自死锁（会超时）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CA_DIR = REPO / "_study" / "codearts2openai"


def load_ca():
    sys.path.insert(0, str(CA_DIR))
    import codearts2openai as ca
    return ca


def _run_with_timeout(fn, seconds=5.0):
    """在子线程里跑 fn，超时返回 ('timeout', None)。用于暴露死锁。"""
    box = {}

    def _t():
        try:
            box["v"] = fn()
            box["ok"] = True
        except Exception as e:  # noqa: BLE001
            box["e"] = e
            box["ok"] = False

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        return "timeout", None
    return ("ok" if box.get("ok") else "error"), box.get("v") or box.get("e")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repro-old", action="store_true",
                    help="把 _locked_state_update 还原成不可重入，复现自死锁")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="ca_lock_"))
    os.environ["BUDDYZ_DATA_DIR"] = str(tmp)
    ok = True
    try:
        ca = load_ca()
        if args.repro_old:
            print("== 还原成修复前的不可重入实现 ==")
            orig = ca._locked_state_update

            def nonreentrant(fn):
                import msvcrt
                fp = ca._state_file()
                fp.parent.mkdir(parents=True, exist_ok=True)
                lockf = fp.parent / (fp.name + ".lock")
                with open(str(lockf), "a+b") as f:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    except OSError:
                        pass
                    try:
                        return fn()
                    finally:
                        try:
                            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
            ca._locked_state_update = nonreentrant
            status, val = _run_with_timeout(
                lambda: ca._locked_state_update(
                    lambda: ca._locked_state_update(lambda: "inner-done")), 5.0)
            print(f"  嵌套加锁结果: {status}  value={val!r}")
            print("  修复前行为复现：" + ("YES（自死锁，超时未返回）"
                                      if status == "timeout" else "NO"))
            ca._locked_state_update = orig
            return 0 if status == "timeout" else 1

        print("=== 1) 锁可重入（不再自死锁） ===")
        status, val = _run_with_timeout(
            lambda: ca._locked_state_update(
                lambda: ca._locked_state_update(
                    lambda: ca._locked_state_update(lambda: "inner-done"))), 5.0)
        reentrant_ok = (status == "ok" and val == "inner-done")
        print(("  ✓ " if reentrant_ok else "  ✗ ") +
              f"三层嵌套加锁 → {status} / {val!r}")
        ok &= reentrant_ok

        print("\n=== 2) 池路径不再每请求打 STS ===")
        ca.pool_harvest()
        entries = ca.pool_list()
        if not entries:
            print("  · 本机无可用登录，跳过（用假条目测）")
            st = ca._load_state()
            st["pool"] = [{"label": "假条目·国内", "refresh_token": "RT",
                           "dpop_priv": {"kty": "EC"}, "dpop_pub": {"kty": "EC"},
                           "verifier": "V", "station": "china", "enabled": True}]
            ca._save_state(st)
            entries = ca.pool_list()
        ca._pool_invalidate()
        ca.CONFIG["pool_enabled"] = True

        calls = {"n": 0}
        real = ca._refresh_candidate

        def fake_refresh(cand):
            calls["n"] += 1
            exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            cred = {"access_key_id": "AK", "secret_access_key": "SK",
                    "security_token": "ST", "expiration": exp,
                    "station": cand.get("station") or "china"}
            return cred, "NEW-RT"

        ca._refresh_candidate = fake_refresh
        try:
            c1 = ca.ensure_creds(prefer="dpop")
            n_after_1 = calls["n"]
            c2 = ca.ensure_creds(prefer="dpop")
            n_after_2 = calls["n"]
            ca.ensure_creds(prefer="dpop")   # 只关心是否又打了 STS
            n_after_3 = calls["n"]
        finally:
            ca._refresh_candidate = real

        print(f"  第1次：STS 调用数={n_after_1}")
        print(f"  第2次：STS 调用数={n_after_2}")
        print(f"  第3次：STS 调用数={n_after_3}")
        cache_ok = (n_after_1 == 1 and n_after_2 == 1 and n_after_3 == 1)
        print(("  ✓ " if cache_ok else "  ✗ ") +
              "首次续期后命中缓存，后续请求不再打 STS")
        ok &= cache_ok
        same_ok = (c1 is c2 or c1 == c2)
        print(("  ✓ " if same_ok else "  ✗ ") + "返回的是同一份缓存凭据")
        ok &= same_ok

        print("\n=== 3) 缓存过期后会重新续期 ===")
        # 把缓存里的 expiration 改成 1 分钟内到期 → 下次应重新 STS
        for k in list(ca._POOL_CRED):
            ca._POOL_CRED[k] = dict(ca._POOL_CRED[k],
                                    expiration=(datetime.now(timezone.utc)
                                                + timedelta(seconds=60)).isoformat())
        ca._refresh_candidate = fake_refresh
        try:
            before = calls["n"]
            ca.ensure_creds(prefer="dpop")
            after = calls["n"]
        finally:
            ca._refresh_candidate = real
        stale_ok = after == before + 1
        print(("  ✓ " if stale_ok else "  ✗ ") +
              f"将过期时重新续期（STS {before} → {after}）")
        ok &= stale_ok

        print("\n=== 4) _pool_state 暴露 cached 字段 ===")
        ps = ca._pool_state()
        has_cached = all("cached" in k for k in ps.get("keys") or [])
        print(("  ✓ " if has_cached else "  ✗ ") + f"{json.dumps(ps, ensure_ascii=False)}")
        ok &= has_cached
        ca.CONFIG["pool_enabled"] = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
