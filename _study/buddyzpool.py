# -*- coding: utf-8 -*-
"""buddyzpool — 多凭据号池调度引擎（stdout/依赖零第三方）。

设计参考社区成熟实现（LiteLLM Router / one-api / gpt-load 在生产里的做法）：

  选择策略
    · round_robin   —— 顺序轮转（默认，最稳、零开销）
    · least_busy    —— 在途请求最少 → 近期尝试次数最少（对应 LiteLLM least-busy）
    · weighted      —— 按条目权重加权随机置换（对应 simple-shuffle 的 weight）
    · least_latency —— 观测延迟最低（EWMA，对应 latency-based-routing，带缓冲带）

  熔断与冷却
    · 连续失败达 allowed_fails → 冷却
    · 或「窗口内失败率 ≥ ratio_threshold 且样本 ≥ min_samples」→ 冷却
      （对应 LiteLLM 的 ">50% failures in current minute"）
    · 冷却时长按错误类型分级（429 / 5xx / 网络），并支持 Retry-After
    · 连续多次冷却按倍率指数退避，上限 cooldown_max
    · 401/403（凭据失效）→ off，并按 off_recheck_seconds 定期自动复检恢复
      （对应 one-api 的「自动禁用 + 定时测试自动恢复」）

  请求侧
    · max_retries   —— 一次请求最多再试几条（排除集跨跳累积，对应 weighted failover）
    · 会话粘性      —— 同一会话固定同一凭据；粘住的条目不可用时回落但保留绑定
                      （对应 LiteLLM session_affinity，deployment_affinity_ttl）

引擎不关心条目里装的是 API key 还是登录会话，只认：
  entry = {"label": str, "weight": int=1, "enabled": bool=True, ...其余原样透传}

用法：
    eng = PoolEngine(entries_provider, config)      # entries_provider() -> list[dict]
    for idx, entry in eng.order(conv_key):
        eng.begin(idx)
        try:
            cred = make_cred(entry)
        except MyErr as e:
            eng.fail(idx, status=e.status, retry_after=e.retry_after)
        else:
            eng.ok(idx, latency=elapsed)
            eng.bind(conv_key, idx)
            return cred
        finally:
            eng.end(idx)
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque


# ---------------------------------------------------------------------------
# 默认参数（括号里是社区参考值）
# ---------------------------------------------------------------------------

DEFAULTS = {
    "strategy": "round_robin",   # round_robin | least_busy | weighted | least_latency
    "allowed_fails": 3,          # LiteLLM allowed_fails 默认 3
    "fail_window": 60.0,         # 失败率统计窗口（LiteLLM 按"每分钟"）
    "ratio_threshold": 0.5,      # 窗口内失败率阈值（LiteLLM：>50%）
    "min_samples": 4,            # 样本太少不触发比例熔断
    # 冷却 = cooldown_base × 倍率。倍率让"基数"一个旋钮就能整体缩放：
    #   限流等久点（很多上游按分钟计窗） / 上游故障中等 / 网络抖动短冷却即可
    "cooldown_base": 30.0,       # 冷却基数（秒）
    "k_429": 2.0,                # 限流 → 60s
    "k_5xx": 1.0,                # 上游故障 → 30s
    "k_net": 0.5,                # 传输层异常 → 15s
    "cooldown_max": 600.0,       # 指数退避上限
    "cooldown_backoff": True,    # 连续冷却是否指数退避
    "off_recheck": 1800.0,       # 失效条目多久后自动复检（one-api 定时测试思路）
    "max_retries": 2,            # 一次请求最多再试几条
    "max_inflight": 0,           # 单条目在途上限，0=不限
    "affinity": False,           # 会话粘性（默认关，利于 prompt cache 时可开）
    "affinity_ttl": 3600.0,      # 粘性绑定空闲 TTL（LiteLLM deployment_affinity_ttl）
    "latency_buffer": 0.5,       # 延迟策略缓冲带：延迟 ≤ 最优×(1+buffer) 都算候选
}

# 视作「凭据失效」的状态码：不是慢，而是这把凭据没用了
AUTH_DEAD = (401, 403)


class _St:
    """单条目运行时状态。"""
    __slots__ = ("state", "inflight", "consec", "cool_until", "cool_count",
                 "off_since", "ok", "fail", "last_error", "last_used", "last_ok",
                 "lat_ewma", "win", "total")

    def __init__(self):
        self.state = "ok"        # ok | cooling | off
        self.inflight = 0
        self.consec = 0          # 连续失败
        self.cool_until = 0.0
        self.cool_count = 0      # 连续冷却次数（用于指数退避）
        self.off_since = 0.0
        self.ok = self.fail = 0
        self.last_error = ""
        self.last_used = 0.0
        self.last_ok = 0.0
        self.lat_ewma = 0.0
        self.win = deque()       # (ts, ok) 近窗口的尝试结果
        self.total = 0           # 本轮运行总尝试


class PoolEngine:
    def __init__(self, entries_provider, config: dict | None = None, log=None):
        self._entries = entries_provider
        self.cfg = dict(DEFAULTS)
        if config:
            self.cfg.update({k: v for k, v in config.items() if k in DEFAULTS})
        self._log = log or (lambda *_a, **_k: None)
        self._lock = threading.RLock()
        self._st: dict[str, _St] = {}
        self._rr = 0                       # round_robin 游标
        self._aff: dict[str, tuple[int, float]] = {}   # conv_key -> (idx, ts)

    # ---------------- 配置 ----------------
    def set_config(self, **kw):
        with self._lock:
            for k, v in kw.items():
                if k in DEFAULTS and v is not None:
                    self.cfg[k] = v

    def config(self) -> dict:
        with self._lock:
            return dict(self.cfg)

    # ---------------- 状态 ----------------
    def _s(self, label: str) -> _St:
        st = self._st.get(label)
        if st is None:
            st = self._st[label] = _St()
        return st

    def _label(self, idx: int, entry: dict) -> str:
        return str(entry.get("label") or f"#{idx}")

    def _maybe_revive(self, label: str, st: _St, now: float):
        """失效条目到期自动复检恢复（one-api 的「定时测试自动恢复」）。"""
        if st.state == "off" and st.off_since and \
                now - st.off_since >= float(self.cfg["off_recheck"]):
            st.state = "ok"
            st.off_since = 0.0
            st.cool_until = 0.0
            st.consec = 0
            st.cool_count = 0
            self._log(f"[pool] {label} 到达复检间隔，重新放回候选")

    def _healthy(self, idx: int, entry: dict, now: float):
        if not entry.get("enabled", True):
            return None
        st = self._s(self._label(idx, entry))
        self._maybe_revive(self._label(idx, entry), st, now)
        if st.state == "off":
            return None
        if st.cool_until > now:
            return None
        mi = int(self.cfg["max_inflight"] or 0)
        if mi and st.inflight >= mi:
            return None
        return st

    # ---------------- 选择 ----------------
    def order(self, conv_key: str | None = None, full: bool = False) -> list:
        """返回本次请求可尝试的 [(idx, entry)]，按优先级排好。

        调用方依次尝试；失败就 on_fail 并继续下一个（这就是 weighted failover）。

        full=False（默认）：最多 max_retries+1 条 —— 避免一次**计费请求**把整池试穿。
        full=True：返回全部健康条目 —— 给「列模型」这类**发现类**调用用，
                   它们不该被重试上限截断（否则前几条都冷却时会误报空列表）。
        """
        now = time.time()
        with self._lock:
            cands = []
            for idx, e in enumerate(self._entries()):
                st = self._healthy(idx, e, now)
                if st is not None:
                    cands.append((idx, e, st))
            if not cands:
                return []
            cands = self._apply_affinity(cands, conv_key, now)
            cands = self._sort(cands, now)
            cands = self._apply_affinity_order(cands, conv_key)
            if full:
                return [(i, e) for i, e, _s in cands]
            limit = int(self.cfg["max_retries"]) + 1
            return [(i, e) for i, e, _s in cands[:limit]]

    def _apply_affinity(self, cands, conv_key, now):
        """粘性绑定过期的清掉；绑定仍健康则标记优先。"""
        if self.cfg["affinity"] and conv_key:
            pin = self._aff.get(conv_key)
            if pin and now - pin[1] > float(self.cfg["affinity_ttl"]):
                self._aff.pop(conv_key, None)
        return cands

    def _apply_affinity_order(self, cands, conv_key):
        """把粘住的条目提到最前（不可用时自然回落，但绑定保留）。"""
        if self.cfg["affinity"] and conv_key:
            pin = self._aff.get(conv_key)
            if pin:
                for k, (i, _e, _s) in enumerate(cands):
                    if i == pin[0]:
                        if k:
                            cands.insert(0, cands.pop(k))
                        return cands
        return cands

    def _sort(self, cands, now):
        """先按优先级分层（对应 LiteLLM 的 order：低值优先，层内再选策略），
        再在层内应用选择策略。"""
        prios = sorted({int(e.get("priority", 0) or 0) for _i, e, _s in cands})
        if len(prios) > 1:
            out = []
            for p in prios:
                tier = [t for t in cands if int(t[1].get("priority", 0) or 0) == p]
                out.extend(self._sort_tier(tier, now))
            return out
        return self._sort_tier(cands, now)

    def _sort_tier(self, cands, now):
        strat = self.cfg["strategy"]
        if strat == "least_busy":
            # 在途最少 → 近窗口尝试最少（等量分流），对应 LiteLLM least-busy
            def key(t):
                _i, _e, s = t
                recent = sum(1 for ts, _ok in s.win if now - ts <= 60.0)
                return (s.inflight, recent, s.consec)
            cands.sort(key=key)
        elif strat == "least_latency":
            # 延迟带内随机（LiteLLM lowest_latency_buffer：避免把最快那条打满）
            known = [t for t in cands if t[2].lat_ewma > 0]
            if known:
                best = min(t[2].lat_ewma for t in known)
                buf = best * (1.0 + float(self.cfg["latency_buffer"]))
                inband = [t for t in cands if 0 < t[2].lat_ewma <= buf]
                unknown = [t for t in cands if t[2].lat_ewma <= 0]
                rest = [t for t in cands if t[2].lat_ewma > buf]
                random.shuffle(inband)
                random.shuffle(unknown)       # 未知的排第二，给它们采样机会
                rest.sort(key=lambda t: t[2].lat_ewma)
                return inband + unknown + rest
            random.shuffle(cands)
        elif strat == "weighted":
            # 加权随机置换（无放回）：Efraimidis-Spirakis，key=U^(1/w) 取**最大**
            # 对应 LiteLLM simple-shuffle 的 weight 语义（权重大的被优先抽出）
            cands.sort(key=lambda t: random.random() **
                       (1.0 / max(1.0, float(t[1].get("weight", 1) or 1))),
                       reverse=True)
        else:  # round_robin
            if cands:
                k = self._rr % len(cands)
                cands = cands[k:] + cands[:k]
        return cands

    # ---------------- 结果回馈 ----------------
    def begin(self, idx: int):
        with self._lock:
            for i, e in enumerate(self._entries()):
                if i == idx:
                    self._s(self._label(i, e)).inflight += 1
                    return

    def end(self, idx: int):
        with self._lock:
            for i, e in enumerate(self._entries()):
                if i == idx:
                    st = self._s(self._label(i, e))
                    st.inflight = max(0, st.inflight - 1)
                    return

    def ok(self, idx: int, latency: float | None = None):
        """成功：清连续失败、关熔断、退冷却，必要时自动恢复 off。"""
        with self._lock:
            e = self._entry_at(idx)
            if e is None:
                return
            label = self._label(idx, e)
            st = self._s(label)
            self._rr += 1
            st.consec = 0
            st.cool_count = 0
            st.cool_until = 0.0
            if st.state == "off":
                st.state = "ok"
                st.off_since = 0.0
                self._log(f"[pool] {label} 复检成功，已恢复")
            st.ok += 1
            st.last_ok = time.time()
            st.last_error = ""
            if latency and latency > 0:
                st.lat_ewma = (latency if st.lat_ewma <= 0
                               else st.lat_ewma * 0.7 + latency * 0.3)
            self._push_win(st, True)

    def fail(self, idx: int, status=None, retry_after=None, note: str = "",
             manual: bool = False):
        """失败：按状态码分级冷却；凭据失效转 off 待复检。

        manual=True 表示这是**显式健康探测**（号池「测试全部」按钮）：
        不等连续失败阈值，立刻按规则处置 —— 用户点了测试就是明确信号。
        """
        now = time.time()
        with self._lock:
            e = self._entry_at(idx)
            if e is None:
                return
            label = self._label(idx, e)
            st = self._s(label)
            self._rr += 1
            st.consec += 1
            st.fail += 1
            st.last_error = self._err_text(status, note)
            self._push_win(st, False)

            if status in AUTH_DEAD:
                st.state = "off"
                st.off_since = now
                st.cool_until = 0.0
                self._log(f"[pool] {label} 凭据失效({status})，停用并等待复检")
                return

            # 已在冷却窗口内：只累计统计，**不重复叠加冷却/退避**
            # （否则同一个正在冷却的条目被连续打几次失败，冷却会被指数炸到上限）
            if st.cool_until > now:
                return

            # 连续失败 或 窗口失败率过高 或 手动探测失败 → 熔断
            if manual or st.consec >= int(self.cfg["allowed_fails"]) \
                    or self._ratio_bad(st, now):
                base = float(self.cfg["cooldown_base"])
                if status == 429 or retry_after:
                    base *= float(self.cfg["k_429"])
                elif isinstance(status, int) and status >= 500:
                    base *= float(self.cfg["k_5xx"])
                elif status is None:
                    base *= float(self.cfg["k_net"])
                if retry_after:
                    try:
                        base = max(base, float(retry_after))
                    except (TypeError, ValueError):
                        pass
                st.cool_count += 1
                if self.cfg["cooldown_backoff"] and st.cool_count > 1:
                    base *= 2 ** (st.cool_count - 1)
                base = min(base, float(self.cfg["cooldown_max"]))
                st.cool_until = now + base
                st.state = "cooling"
                self._log(f"[pool] {label} 冷却 {base:.0f}s"
                          f"（连续失败 {st.consec}，{st.last_error}）")

    def _push_win(self, st: _St, good: bool):
        st.total += 1
        st.win.append((time.time(), good))
        edge = time.time() - float(self.cfg["fail_window"])
        while st.win and st.win[0][0] < edge:
            st.win.popleft()

    def _ratio_bad(self, st: _St, now: float) -> bool:
        n = len(st.win)
        if n < int(self.cfg["min_samples"]):
            return False
        bad = sum(1 for _t, ok in st.win if not ok)
        return bad / n >= float(self.cfg["ratio_threshold"])

    def _err_text(self, status, note: str) -> str:
        if note:
            return note[:80]
        return f"HTTP {status}" if status else "error"

    def _entry_at(self, idx: int):
        ents = self._entries()
        return ents[idx] if 0 <= idx < len(ents) else None

    # ---------------- 会话粘性 ----------------
    def bind(self, conv_key: str | None, idx: int):
        if self.cfg["affinity"] and conv_key:
            with self._lock:
                self._aff[conv_key] = (idx, time.time())
                # 懒清理，别让表无限涨
                ttl = float(self.cfg["affinity_ttl"])
                now = time.time()
                for k in [k for k, (_i, ts) in self._aff.items() if now - ts > ttl]:
                    self._aff.pop(k, None)

    def pinned(self, conv_key: str | None):
        if not (self.cfg["affinity"] and conv_key):
            return None
        with self._lock:
            p = self._aff.get(conv_key)
            return p[0] if p else None

    # ---------------- 运维 ----------------
    def invalidate(self):
        """池结构变了：清冷却/游标/粘性，并复位在途计数（统计保留）。

        在途计数按 idx 记账，池增删会让下标错位、旧计数再也不会被减回去
        （配了 max_inflight 就会把条目永久卡满）。结构一变就归零最稳。
        """
        with self._lock:
            self._rr = 0
            self._aff.clear()
            for st in self._st.values():
                st.inflight = 0
                if st.state != "off":
                    st.state = "ok"
                    st.cool_until = 0.0
                st.cool_count = 0
                st.consec = 0

    def clear_cooldowns(self):
        """清空冷却与失效标记（按钮：立即全部恢复可用）。"""
        with self._lock:
            for st in self._st.values():
                st.state = "ok"
                st.cool_until = 0.0
                st.off_since = 0.0
                st.cool_count = 0
                st.consec = 0

    def reset_stats(self):
        with self._lock:
            self._st.clear()
            self._rr = 0
            self._aff.clear()

    def snapshot(self, now: float | None = None) -> list:
        """给 GUI 的只读快照。"""
        now = now or time.time()
        with self._lock:
            out = []
            for idx, e in enumerate(self._entries()):
                label = self._label(idx, e)
                st = self._st.get(label)
                cooling = bool(st and st.cool_until > now)
                off = bool(st and st.state == "off")
                out.append({
                    "idx": idx,
                    "label": label,
                    "weight": e.get("weight", 1) or 1,
                    "priority": int(e.get("priority", 0) or 0),
                    "enabled": bool(e.get("enabled", True)),
                    "state": ("off" if off else "cooling" if cooling else "ok"),
                    "cool_remain": max(0.0, st.cool_until - now) if st else 0.0,
                    "inflight": st.inflight if st else 0,
                    "ok": st.ok if st else 0,
                    "fail": st.fail if st else 0,
                    "consec": st.consec if st else 0,
                    "last_error": st.last_error if st else "",
                    "latency_ms": int(st.lat_ewma * 1000) if st and st.lat_ewma else 0,
                    "extra": e.get("extra") or {},
                })
            return out

    def summary(self) -> dict:
        snap = self.snapshot()
        return {
            "total": len(snap),
            "ok": sum(1 for s in snap if s["state"] == "ok" and s["enabled"]),
            "cooling": sum(1 for s in snap if s["state"] == "cooling"),
            "off": sum(1 for s in snap if s["state"] == "off"),
            "disabled": sum(1 for s in snap if not s["enabled"]),
        }
