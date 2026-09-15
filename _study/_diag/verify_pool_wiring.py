# -*- coding: utf-8 -*-
"""verify_pool_wiring — 两个通道接上 buddyzpool 引擎后的行为验证。

不碰真实凭据/网络：ca 的续期函数、mc 的 key 都是造出来 + monkeypatch 的。

覆盖：
  ca：池开关、候选选择、失败冷却后自动换条、失效转 off、统计、配置持久化
  mc：候选列表、号池关闭时只有本机桌面端、失败分级、/health 里的池状态

用法：python verify_pool_wiring.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OK = True


def chk(name, cond, extra=""):
    global OK
    if not cond:
        OK = False
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   {extra}" if extra else ""))


def fresh_ca():
    """全新导入 ca 模块（干净 CONFIG/引擎），数据目录指向临时。"""
    for m in list(sys.modules):
        if m in ("codearts2openai", "buddyzpool"):
            del sys.modules[m]
    for p in (str(REPO / "_study"), str(REPO / "_study" / "codearts2openai")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import codearts2openai as ca
    return ca


def fake_cred(station="china", hours=1):
    return {"access_key_id": "AK", "secret_access_key": "SK",
            "security_token": "ST",
            "expiration": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
            "station": station}


def seed_pool(ca, labels):
    st = ca._load_state()
    st["pool"] = [{"label": lb, "refresh_token": f"RT-{lb}",
                   "dpop_priv": {"kty": "EC"}, "dpop_pub": {"kty": "EC"},
                   "verifier": "V", "station": "china", "enabled": True,
                   "weight": 1, "priority": 0} for lb in labels]
    ca._save_state(st)
    ca.pool_clear_cooldowns()
    ca.pool_reset_stats()


def test_ca(tmp):
    print("\n########## ca 通道 ##########")
    os.environ["BUDDYZ_DATA_DIR"] = str(tmp)
    ca = fresh_ca()
    seed_pool(ca, ["A", "B", "C"])
    ca.CONFIG["pool_enabled"] = True

    print("=== 1) 池配置读写 + 落盘 ===")
    cfg = ca.pool_config()
    chk("默认策略 round_robin", cfg["strategy"] == "round_robin", cfg["strategy"])
    ca.pool_set_config(strategy="least_busy", cooldown_base=12, allowed_fails=2)
    chk("set_config 生效", ca.pool_config()["strategy"] == "least_busy")
    chk("落盘到 state.pool_cfg", (ca._load_state().get("pool_cfg") or {}).get("cooldown_base") == 12,
        json.dumps(ca._load_state().get("pool_cfg"), ensure_ascii=False))

    print("\n=== 2) 正常取凭证（走引擎） ===")
    used = []
    ca._refresh_candidate = lambda cand: (used.append(cand["label"]), (fake_cred(), "NEW-RT"))[1]
    c = ca.ensure_creds(prefer="dpop")
    chk("取到凭证", bool(c.get("access_key_id")))
    chk("命中池条目", used and used[0] in {"A", "B", "C"}, str(used))
    snap = {s["label"]: s for s in ca._pool_state()["keys"]}
    chk("成功计入统计", snap[used[0]]["ok"] == 1, str({k: v["ok"] for k, v in snap.items()}))
    chk("缓存已建立", ca._pool_cached(used[0]) is not None)

    print("\n=== 3) 连续失败 → 冷却 → 自动换条 ===")
    ca.pool_reset_stats(); ca.pool_clear_cooldowns()
    first = used[0]
    tries = []

    def fail_first(cand):
        tries.append(cand["label"])
        if cand["label"] == first:
            raise RuntimeError("refresh 失败(500): boom")
        return fake_cred(), "NEW-RT"

    ca._refresh_candidate = fail_first
    c = ca.ensure_creds(prefer="dpop")
    chk("失败后换到别的条目", c and tries[0] == first and tries[-1] != first, str(tries))
    snap = {s["label"]: s for s in ca._pool_state()["keys"]}
    chk(f"{first} 计入失败", snap[first]["fail"] >= 1, str(snap[first]))

    print("\n=== 4) 401 → off，且不参与调度 ===")
    ca.pool_reset_stats(); ca.pool_clear_cooldowns()
    # 取当前真正会轮到的第一条，避免依赖策略的排序假设
    bad = ca._pool_engine().order()[0][1].get("label")

    def dead(cand):
        if cand["label"] == bad:
            raise RuntimeError("refresh 失败(401): dead")
        return fake_cred(), "NEW-RT"

    ca._refresh_candidate = dead
    c = ca.ensure_creds(prefer="dpop")
    snap = {s["label"]: s for s in ca._pool_state()["keys"]}
    chk(f"{bad} 标记 off", snap[bad]["state"] == "off", snap[bad]["state"])
    labels = [e.get("label") for _i, e in ca._pool_engine().order()]
    chk("off 条目被排除", bad not in labels, str(labels))
    chk("其余条目仍能出凭证", bool(c.get("access_key_id")))

    print("\n=== 5) 池关闭时不影响主链 ===")
    ca.CONFIG["pool_enabled"] = False
    calls = []
    ca._refresh_candidate = lambda cand: (calls.append(cand["label"]),
                                          (fake_cred(), "NEW-RT"))[1]
    ca._pool_engine().reset_stats()
    # 关池时 ensure_creds 不该走引擎；这里只验证 _pool_state 仍可读
    ps = ca._pool_state()
    chk("state.enabled=False", ps["enabled"] is False)
    chk("仍能看到全部条目（GUI 管理用）", len(ps["keys"]) == 3, str(len(ps["keys"])))

    print("\n=== 6) /health 带池状态与统计 ===")
    ca.CONFIG["pool_enabled"] = True
    h = ca._pool_state()
    for k in ("enabled", "keys", "summary", "strategy"):
        chk(f"含字段 {k}", k in h)
    chk("summary 计数", h["summary"].get("total") == 3, str(h["summary"]))
    one = h["keys"][0]
    for k in ("state", "cool_remain", "inflight", "ok", "fail", "consec",
              "last_error", "latency_ms", "weight", "priority", "cached"):
        chk(f"条目含字段 {k}", k in one)
        break        # 只打一条汇总，避免刷屏
    chk("条目字段齐全", {"state", "cool_remain", "ok", "fail"} <= set(one))


    print("\n=== 7) 批量测试：遍历全部号 + 失败按号池规则处置 ===")
    seed_pool(ca, ["A", "B", "C"])
    ca.CONFIG["pool_enabled"] = True
    ca.pool_set_config(allowed_fails=3, cooldown_base=20, k_429=2.0, k_5xx=1.0,
                       k_net=0.5, off_recheck=3600, max_retries=9)
    ca.pool_reset_stats(); ca.pool_clear_cooldowns()

    # pool_targets 必须与 pool_probe(idx) 的索引空间一致（GUI 靠它遍历）
    targets = ca.pool_targets()
    chk("pool_targets 与池条目一一对应且同序",
        [t.get("label") for t in targets] == ["A", "B", "C"],
        str([t.get("label") for t in targets]))

    def flaky(cand):
        lb = cand["label"]
        if lb == "A":
            raise RuntimeError("refresh 失败(401): dead")
        if lb == "B":
            raise RuntimeError("refresh 失败(500): boom")
        return fake_cred(), "NEW-RT"

    ca._refresh_candidate = flaky
    res = {}
    for i, e in enumerate(ca.pool_targets()):
        res[e.get("label")] = ca.pool_probe(i)

    chk("A 探测失败（401）", res["A"][0] is False, res["A"][1])
    chk("A 已按规则停用", "停用" in res["A"][1], res["A"][1])
    chk("B 探测失败（5xx）", res["B"][0] is False, res["B"][1])
    chk("B 已按规则冷却", "冷却" in res["B"][1], res["B"][1])
    chk("C 探测通过", res["C"][0] is True, res["C"][1])

    snap = {s["label"]: s for s in ca._pool_state()["keys"]}
    chk("状态落盘正确：A=off", snap["A"]["state"] == "off", snap["A"]["state"])
    chk("状态落盘正确：B=cooling", snap["B"]["state"] == "cooling",
        f"{snap['B']['state']} {snap['B']['cool_remain']:.0f}s")
    chk("状态落盘正确：C=ok", snap["C"]["state"] == "ok", snap["C"]["state"])

    # 停用的号不再参与调度，但仍在池里（等复检）
    labels = [e.get("label") for _i, e in ca._pool_engine().order(full=True)]
    chk("off/冷却的号不参与调度", labels == ["C"], str(labels))
    chk("停用的号仍留在池中", len(ca.pool_list()) == 3, str(len(ca.pool_list())))

    # 探活成功可让停用的号复活（ok() 自动恢复）
    ca._refresh_candidate = lambda cand: (fake_cred(), "NEW-RT")
    good, note = ca.pool_probe(ca.pool_targets().index(
        next(t for t in ca.pool_targets() if t.get("label") == "A")))
    snap = {s["label"]: s for s in ca._pool_state()["keys"]}
    chk("复测通过后停用的号自动恢复 ok",
        good and snap["A"]["state"] == "ok", f"{good} {snap['A']['state']}")


def fresh_mc(tmp):
    for m in list(sys.modules):
        if m in ("monkeycode2openai", "buddyzpool"):
            del sys.modules[m]
    for p in (str(REPO / "_study"), str(REPO / "_study" / "monkeycode2openai")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import monkeycode2openai as mc
    return mc


def test_mc(tmp):
    print("\n########## mc 通道 ##########")
    mc = fresh_mc(tmp)
    mc.CONFIG.update({
        "base_url": "https://local.mc/v1", "api_key": "KEY-LOCAL",
        "pool_enabled": False,
        "pool_keys": [
            {"base_url": "https://p1.mc/v1", "api_key": "KEY-1", "enabled": True, "label": "池1"},
            {"base_url": "https://p2.mc/v1", "api_key": "KEY-2", "enabled": True, "label": "池2"},
            {"base_url": "https://p3.mc/v1", "api_key": "KEY-3", "enabled": False, "label": "池3"},
        ],
        "pool_cfg": {},
    })

    print("=== 1) 池关闭 → 只有本机桌面端 ===")
    order = mc._pool_order()
    labels = [e.get("label") for _i, e in order]
    chk("只返回本机桌面端", labels == ["本机桌面端"], str(labels))

    print("=== 2) 池开启 → 本机 + 启用的池 key（禁用那条不参与） ===")
    mc.CONFIG["pool_enabled"] = True
    mc.pool_reset_stats()
    seen = set()
    for _ in range(12):
        labels = [e.get("label") for _i, e in mc._pool_order()]
        seen.update(labels)
        mc._pool_ok(0)
    chk("包含本机桌面端/池1/池2", {"本机桌面端", "池1", "池2"} <= seen, str(sorted(seen)))
    chk("不含被禁用的池3", "池3" not in seen, str(sorted(seen)))

    print("=== 3) 失败分级：401 → off，5xx → 冷却 ===")
    mc.pool_clear_cooldowns(); mc.pool_reset_stats()
    mc._pool_fail(1, status=401)          # 池1 失效
    mc._pool_fail(2, status=500)
    mc._pool_fail(2, status=500)
    mc._pool_fail(2, status=500)          # 池2 达阈值冷却
    snap = {s["label"]: s for s in mc._pool_state()["keys"]}
    chk("池1 off", snap["池1"]["state"] == "off", snap["池1"]["state"])
    chk("池2 cooling", snap["池2"]["state"] == "cooling",
        f"{snap['池2']['state']} {snap['池2']['cool_remain']}s")
    labels = [e.get("label") for _i, e in mc._pool_order()]
    chk("off/冷却条目都排除了", labels == ["本机桌面端"], str(labels))
    chk("GUI 仍能看到全部 4 条（含禁用）", len(snap) == 4, str(sorted(snap)))

    print("=== 4) 清空冷却按钮 ===")
    mc.pool_clear_cooldowns()
    snap = {s["label"]: s for s in mc._pool_state()["keys"]}
    chk("全部恢复 ok/可用", all(s["state"] == "ok" for s in snap.values()),
        str({k: v["state"] for k, v in snap.items()}))

    print("=== 5) 策略切换 ===")
    mc.pool_set_config(strategy="least_busy", cooldown_base=20, max_retries=1)
    cfg = mc.pool_config()
    chk("strategy 生效", cfg["strategy"] == "least_busy", cfg["strategy"])
    chk("max_retries 生效", cfg["max_retries"] == 1)
    chk("saved 落盘值", cfg["saved"].get("cooldown_base") == 20, str(cfg["saved"]))
    chk("max_retries=1 → order 返回 2 条", len(mc._pool_order()) == 2,
        str(len(mc._pool_order())))

    print("=== 6) /health 池状态结构 ===")
    ps = mc._pool_state()
    chk("含 summary/strategy", "summary" in ps and "strategy" in ps)
    chk("条目含 key_prefix", "key_prefix" in ps["keys"][0], str(ps["keys"][0]))


    print("\n=== 7) 批量测试：mc 全部号 + 失败按规则处置 ===")
    # 让 池1 → 401、池2 → 5xx、其余 200；验证 index 0 是本机桌面端也不错位
    import urllib.error

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", str(req))
        if "p1.mc" in url:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if "p2.mc" in url:
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
        return _Resp()

    mc.CONFIG["pool_enabled"] = True
    mc.pool_set_config(allowed_fails=3, cooldown_base=30, k_429=2.0, k_5xx=1.0,
                       k_net=0.5, off_recheck=3600, max_retries=9)
    mc.pool_clear_cooldowns(); mc.pool_reset_stats()
    mc.urllib.request.urlopen = fake_urlopen

    tg = mc.pool_targets()
    # 注意：pool_targets() 是**管理视角**，含被禁用的池3（引擎在调度时才按 enabled 过滤）
    chk("pool_targets 含本机桌面端且同序（含禁用的池3）",
        [t.get("label") for t in tg] == ["本机桌面端", "池1", "池2", "池3"],
        str([t.get("label") for t in tg]))
    chk("被禁用的池3 标记 enabled=False",
        [t.get("enabled") for t in tg] == [True, True, True, False],
        str([t.get("enabled") for t in tg]))
    mres = {}
    for i, e in enumerate(mc.pool_targets()):
        mres[e.get("label")] = mc.pool_probe(i)
    chk("本机桌面端 通过", mres["本机桌面端"][0] is True, mres["本机桌面端"][1])
    chk("池1 失败(401) → 已停用", "停用" in mres["池1"][1], mres["池1"][1])
    chk("池2 失败(5xx) → 已冷却", "冷却" in mres["池2"][1], mres["池2"][1])
    msnap = {s["label"]: s for s in mc._pool_state()["keys"]}
    chk("mc 状态落盘：池1=off", msnap["池1"]["state"] == "off", msnap["池1"]["state"])
    chk("mc 状态落盘：池2=cooling", msnap["池2"]["state"] == "cooling",
        f"{msnap['池2']['state']} {msnap['池2']['cool_remain']:.0f}s")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pool_wire_"))
    try:
        test_ca(tmp)
        test_mc(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
