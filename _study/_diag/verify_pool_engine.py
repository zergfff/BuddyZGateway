# -*- coding: utf-8 -*-
"""verify_pool_engine — buddyzpool 调度引擎单元测试。

覆盖社区成熟实现的各项语义：
  策略：round_robin / least_busy / weighted / least_latency
  熔断：连续失败阈值、窗口失败率、指数退避、Respect Retry-After
  失效：401/403 → off，按 off_recheck 自动复检恢复
  重试：order() 的排除/截断（weighted failover 语义）
  粘性：会话绑定 + 不可用时回落但保留绑定
  运维：snapshot/summary/clear_cooldowns/reset_stats

用法：python verify_pool_engine.py
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "_study"))

from buddyzpool import PoolEngine   # noqa: E402

OK = True


def chk(name, cond, extra=""):
    global OK
    if not cond:
        OK = False
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   {extra}" if extra else ""))


def mk(labels, weights=None):
    return [{"label": l, "enabled": True,
             "weight": (weights or {}).get(l, 1)} for l in labels]


def main() -> int:
    print("=== 1) round_robin 轮转 ===")
    ents = mk(["a", "b", "c"])
    eng = PoolEngine(lambda: ents, {"cooldown_base": 1, "cooldown_backoff": False})
    picks = []
    for _ in range(6):
        o = eng.order()
        picks.append(o[0][1]["label"])
        eng.ok(o[0][0], latency=0.1)
    chk("顺序轮转", picks == ["a", "b", "c", "a", "b", "c"], str(picks))

    print("\n=== 2) least_busy 选在途最少的 ===")
    eng = PoolEngine(lambda: ents, {"strategy": "least_busy"})
    eng.begin(0)
    eng.begin(0)          # a 有 2 个在途
    eng.begin(1)          # b 有 1 个
    o = eng.order()
    chk("选中在途最少的 c", o[0][1]["label"] == "c",
        f"{[x[1]['label'] for x in o]}")
    eng.end(0); eng.end(0); eng.end(1)

    print("\n=== 3) weighted 加权分布 ===")
    ents_w = mk(["w1", "w2"], {"w1": 9, "w2": 1})
    eng = PoolEngine(lambda: ents_w, {"strategy": "weighted"})
    random.seed(7)
    cnt = {"w1": 0, "w2": 0}
    for _ in range(4000):
        o = eng.order()
        cnt[o[0][1]["label"]] += 1
        eng.ok(o[0][0])
    ratio = cnt["w1"] / 4000.0
    chk("w1≈90%（容差 ±4%）", 0.86 <= ratio <= 0.94, f"实测 {ratio:.3f}")

    print("\n=== 4) least_latency 偏好低延迟 + 缓冲带 ===")
    eng = PoolEngine(lambda: ents, {"strategy": "least_latency",
                                    "latency_buffer": 0.1})
    eng.ok(0, latency=1.0)     # a=1.0s
    eng.ok(1, latency=1.05)    # b=1.05s（带内）
    eng.ok(2, latency=9.0)     # c=9.0s（带外）
    hits = set()
    for _ in range(200):
        hits.add(eng.order()[0][1]["label"])
    chk("带内 a/b 被选中，带外 c 不被优先", hits <= {"a", "b"}, str(sorted(hits)))

    print("\n=== 5) 连续失败 → 冷却 + 指数退避 + 成功后复位 ===")
    eng = PoolEngine(lambda: ents, {"allowed_fails": 1, "cooldown_base": 10,
                                    "k_5xx": 1.0, "cooldown_backoff": True,
                                    "cooldown_max": 1000, "off_recheck": 3600})
    eng.fail(0, status=500)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("a 进入 cooling", snap["a"]["state"] == "cooling", snap["a"]["state"])
    chk("首次冷却 10s", 8 <= snap["a"]["cool_remain"] <= 12,
        f"{snap['a']['cool_remain']:.1f}s")
    chk("冷却中不再被选中", all(x[1]["label"] != "a" for x in eng.order()))
    for _ in range(5):
        eng.fail(0, status=500)          # 冷却窗口内反复失败
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("冷却中反复失败**不**叠加（仍 ~10s）", 8 <= snap["a"]["cool_remain"] <= 12,
        f"{snap['a']['cool_remain']:.1f}s")
    eng._s("a").cool_until = 0           # 模拟冷却到期后又被试
    eng.fail(0, status=500)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("第二次冷却退避 ×2 → 20s", 16 <= snap["a"]["cool_remain"] <= 24,
        f"{snap['a']['cool_remain']:.1f}s")
    eng._s("a").cool_until = 0
    eng.fail(0, status=500)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("第三次退避 ×4 → 40s", 32 <= snap["a"]["cool_remain"] <= 48,
        f"{snap['a']['cool_remain']:.1f}s")
    eng.ok(0)                            # 成功 → 复位退避
    eng._s("a").cool_until = 0
    eng.fail(0, status=500)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("成功后退避复位 → 10s", 8 <= snap["a"]["cool_remain"] <= 12,
        f"{snap['a']['cool_remain']:.1f}s")

    print("\n=== 6) 窗口失败率熔断（>50%） ===")
    eng = PoolEngine(lambda: ents, {"allowed_fails": 99, "ratio_threshold": 0.5,
                                    "min_samples": 4, "cooldown_base": 5,
                                    "k_5xx": 1.0, "cooldown_backoff": False})
    for _ in range(3):
        eng.ok(0)
    for _ in range(4):
        eng.fail(0, status=500)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("连续失败未达阈值但失败率 ≥ 50% → 冷却",
        snap["a"]["state"] == "cooling", snap["a"]["state"])
    chk("该条目由比例规则触发（而非连续失败阈值）", snap["a"]["consec"] < 99,
        f"consec={snap['a']['consec']}")

    print("\n=== 7) 401/403 → off，且按 off_recheck 自动复检 ===")
    eng = PoolEngine(lambda: ents, {"off_recheck": 0.4})
    eng.fail(0, status=401)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("a 标记 off", snap["a"]["state"] == "off")
    chk("off 不在候选中", all(x[1]["label"] != "a" for x in eng.order()))
    time.sleep(0.5)
    labels = [x[1]["label"] for x in eng.order()]
    chk("到达复检间隔后重新入池", "a" in labels, str(labels))
    eng.ok(0)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("复检成功后转回 ok", snap["a"]["state"] == "ok")

    print("\n=== 8) Retry-After 优先 ===")
    eng = PoolEngine(lambda: ents, {"allowed_fails": 1, "cooldown_base": 60,
                                    "k_429": 1.0, "cooldown_backoff": False})
    eng.fail(0, status=429, retry_after=300)
    snap = {s["label"]: s for s in eng.snapshot()}
    chk("采纳 Retry-After=300s", 295 <= snap["a"]["cool_remain"] <= 305,
        f"{snap['a']['cool_remain']:.0f}s")

    print("\n=== 9) order() 截断 = max_retries+1（failover 上限） ===")
    ents5 = mk(["a", "b", "c", "d", "e"])
    eng = PoolEngine(lambda: ents5, {"max_retries": 2})
    chk("返回 3 条", len(eng.order()) == 3, str(len(eng.order())))
    eng.set_config(max_retries=0)
    chk("max_retries=0 → 只返回 1 条", len(eng.order()) == 1)
    eng.set_config(max_retries=4)
    chk("max_retries=4 → 返回 5 条", len(eng.order()) == 5)

    print("\n=== 10) 会话粘性：绑定 + 回落但保留绑定 ===")
    eng = PoolEngine(lambda: ents, {"affinity": True, "affinity_ttl": 3600,
                                    "allowed_fails": 1, "cooldown_base": 9999})
    eng.bind("sess-1", 2)
    for _ in range(10):
        chk_ok = eng.order("sess-1")[0][1]["label"] == "c"
        if not chk_ok:
            break
    chk("同会话恒定选 c", chk_ok)
    chk("不同会话不受影响", eng.order("sess-2")[0][1]["label"] in {"a", "b", "c"})
    eng.fail(2, status=500)          # c 冷却
    labels = [x[1]["label"] for x in eng.order("sess-1")]
    chk("c 不可用时回落", "c" not in labels, str(labels))
    eng.clear_cooldowns()
    chk("c 恢复后绑定仍在", eng.order("sess-1")[0][1]["label"] == "c")

    print("\n=== 11) max_inflight 限制 ===")
    eng = PoolEngine(lambda: ents, {"strategy": "least_busy", "max_inflight": 1})
    eng.begin(0); eng.begin(1)
    o = eng.order()
    chk("两个都满了 → 只剩 c", [x[1]["label"] for x in o] == ["c"],
        str([x[1]["label"] for x in o]))
    eng.end(0); eng.end(1)

    print("\n=== 12) snapshot / summary / 运维按钮 ===")
    eng = PoolEngine(lambda: ents, {})
    eng.ok(0); eng.fail(1, status=500); eng.fail(1, status=500); eng.fail(1, status=500)
    snap = eng.snapshot()
    need = {"idx", "label", "weight", "enabled", "state", "cool_remain",
            "inflight", "ok", "fail", "consec", "last_error", "latency_ms"}
    chk("snapshot 字段齐全", all(need <= set(s) for s in snap))
    su = eng.summary()
    chk("summary 计数正确", su["total"] == 3 and su["ok"] >= 1 and su["cooling"] == 1,
        str(su))
    chk("last_error 有值", any(s["last_error"] for s in snap))
    eng.reset_stats()
    snap = eng.snapshot()
    chk("reset_stats 清零统计", all(s["ok"] == 0 and s["fail"] == 0 for s in snap))

    print("\n=== 13) 空池 / 全禁用 不崩 ===")
    eng = PoolEngine(lambda: [], {})
    chk("空池 order 返回 []", eng.order() == [])
    chk("空池 snapshot 返回 []", eng.snapshot() == [])
    eng2 = PoolEngine(lambda: [{"label": "x", "enabled": False}], {})
    chk("全禁用 order 返回 []", eng2.order() == [])

    print("\n=== 14) invalidate 复位结构 ===")
    eng = PoolEngine(lambda: ents, {"allowed_fails": 1, "cooldown_base": 9999})
    eng.fail(0, status=500)
    eng.invalidate()
    chk("invalidate 清冷却", eng.snapshot()[0]["state"] == "ok")

    print("\n=== 15) 优先级分层（LiteLLM order：先按层过滤，层内再选策略） ===")
    ents_p = [{"label": "hi", "enabled": True, "priority": 0},
              {"label": "lo1", "enabled": True, "priority": 1},
              {"label": "lo2", "enabled": True, "priority": 1}]
    eng = PoolEngine(lambda: ents_p, {"strategy": "round_robin", "max_retries": 9})
    firsts = set()
    for _ in range(20):
        o = eng.order()
        firsts.add(o[0][1]["label"])
        eng.ok(o[0][0])
    chk("高优先级层始终排前", firsts == {"hi"}, str(sorted(firsts)))
    o = eng.order()
    chk("层内仍按策略排（lo1/lo2 在后）",
        [x[1]["label"] for x in o][1:] == ["lo1", "lo2"] or
        [x[1]["label"] for x in o][1:] == ["lo2", "lo1"],
        str([x[1]["label"] for x in o]))
    eng.fail(0, status=401)              # hi 失效 → 落到下一层
    o = eng.order()
    chk("高优先级层不可用时降级到低层", o[0][1]["label"] in {"lo1", "lo2"},
        str([x[1]["label"] for x in o]))

    print("\n=== 16) order(full=True)：发现类调用不被重试上限截断 ===")
    eng = PoolEngine(lambda: ents5, {"max_retries": 1})
    chk("默认截断到 2 条", len(eng.order()) == 2, str(len(eng.order())))
    chk("full=True 返回全部 5 条", len(eng.order(full=True)) == 5,
        str(len(eng.order(full=True))))
    eng.fail(0, status=500); eng.fail(0, status=500); eng.fail(0, status=500)
    chk("有冷却条目时 full 仍只给健康的", len(eng.order(full=True)) == 4,
        str(len(eng.order(full=True))))

    print("\n=== 17) manual=True：显式探测失败立刻处置（不等连续失败阈值） ===")
    eng = PoolEngine(lambda: ents, {"allowed_fails": 3, "cooldown_base": 10,
                                    "k_5xx": 1.0, "cooldown_backoff": False,
                                    "off_recheck": 3600})
    eng.fail(0, status=500)
    s = {x["label"]: x for x in eng.snapshot()}
    chk("普通失败未达阈值 → 不冷却", s["a"]["state"] == "ok", s["a"]["state"])
    eng.fail(0, status=500, manual=True)
    s = {x["label"]: x for x in eng.snapshot()}
    chk("manual 失败 → 立即冷却", s["a"]["state"] == "cooling",
        f"{s['a']['state']} {s['a']['cool_remain']:.0f}s")
    eng.clear_cooldowns()
    eng.fail(1, status=401, manual=True)
    s = {x["label"]: x for x in eng.snapshot()}
    chk("manual 401 → 立即停用", s["b"]["state"] == "off", s["b"]["state"])
    eng.clear_cooldowns()
    eng.fail(2, status=None, manual=True)          # 网络类
    s = {x["label"]: x for x in eng.snapshot()}
    chk("manual 网络错 → 按 k_net 冷却(5s)",
        s["c"]["state"] == "cooling" and s["c"]["cool_remain"] <= 6,
        f"{s['c']['state']} {s['c']['cool_remain']:.1f}s")

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
