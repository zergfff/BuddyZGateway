# -*- coding: utf-8 -*-
"""
verify_welfare_and_badges.py — 两个新功能的离线回归。

① 每日福利自动领取（无按钮 / 每通道每日一次 / 分站点各一次）
   · 各通道 claim 函数的返回结构（全用 mock，不打真实上游）
   · 幂等：已领过 → 不重复请求
   · 失败路径也要给出可读错误
② 模型活动标签（仅界面展示，内部模型名不变）
   · badge_labels 结构
   · promotions_for 的 schedule 过滤（每日时段 / 跨零点 / 绝对区间）
   · display_of / resolve_model 不受标签影响（内部逻辑仍用原始名）
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO / "_study" / "codebuddy2openai"))
sys.path.insert(0, str(REPO / "_study" / "monkeycode2openai"))
sys.path.insert(0, str(REPO / "_study" / "codearts2openai"))

PASS = 0
FAIL = 0
SKIP = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  ○ {name} (跳过: {why})")


CST = timezone(timedelta(hours=8))


def main() -> int:
    import converter as conv

    # ================= ② 模型活动标签 =================
    print("\n[1] badge_labels 结构")
    raw = conv.badge_labels()
    check("返回 dict", isinstance(raw, dict))
    if raw:
        for k, v in list(raw.items())[:6]:
            print(f"      {k:26s} -> {v}")
        check("值都是非空 list",
              all(isinstance(v, list) and v for v in raw.values()))
        check("标签都是字符串",
              all(isinstance(x, str) and x for v in raw.values() for x in v))
    else:
        skip("badge_labels", "本机缓存里没有活动/标签")

    print("\n[2] promotions_for 的 schedule 过滤")
    # 构造合成配置，避免依赖线上活动
    now = datetime.now(CST)
    cur = f"{now.hour}:{now.minute:02d}"
    # 造一个「当前命中」的窗口：前后各 30 分钟（可能跨零点）
    lo = (now - timedelta(minutes=30)).strftime("%H:%M")
    hi = (now + timedelta(minutes=30)).strftime("%H:%M")
    cfg_hit = {"modelPromotions": [{
        "id": "t-hit", "enabled": True, "modelIds": ["m1"], "priority": 1,
        "badge": {"label": "测试标签", "color": "#FF0000"},
        "discount": {"discountedCredits": "0.25x", "factor": 0.25},
        "schedule": {"timezone": "Asia/Shanghai", "daily": [{"start": lo, "end": hi}]},
    }]}
    got = conv.promotions_for("m1", cfg_hit)
    check("当前时段命中", len(got) == 1, f"窗口 {lo}-{hi}, 现在 {cur}")

    # 造一个「肯定不命中」的窗口：当前时间前移 5 小时，取 1 分钟宽
    away1 = (now - timedelta(hours=5)).strftime("%H:%M")
    away2 = (now - timedelta(hours=5) + timedelta(minutes=1)).strftime("%H:%M")
    cfg_miss = {"modelPromotions": [{
        "id": "t-miss", "enabled": True, "modelIds": ["m1"], "priority": 1,
        "badge": {"label": "不该出现"}, "schedule": {"daily": [{"start": away1, "end": away2}]},
    }]}
    check("非当前时段不命中", len(conv.promotions_for("m1", cfg_miss)) == 0,
          f"窗口 {away1}-{away2}")

    # 绝对区间
    past = {"modelPromotions": [{
        "id": "t-past", "enabled": True, "modelIds": ["m1"],
        "badge": {"label": "过期"}, "schedule": {
            "validFrom": "2020-01-01T00:00:00+08:00",
            "validUntil": "2020-02-01T00:00:00+08:00"}}]}
    check("已过期区间不命中", len(conv.promotions_for("m1", past)) == 0)
    future = {"modelPromotions": [{
        "id": "t-future", "enabled": True, "modelIds": ["m1"],
        "badge": {"label": "未开始"}, "schedule": {
            "validFrom": "2099-01-01T00:00:00+08:00"}}]}
    check("未开始区间不命中", len(conv.promotions_for("m1", future)) == 0)
    check("enabled=False 不命中",
          len(conv.promotions_for("m1", {"modelPromotions": [
              {"id": "x", "enabled": False, "modelIds": ["m1"],
               "badge": {"label": "关"}}]})) == 0)
    check("modelIds 不含则不命中",
          len(conv.promotions_for("other", cfg_hit)) == 0)

    print("\n[3] badge_labels 合成用例（活动优先 + tags 去重）")
    synth = {"models": [
        {"id": "m1", "tags": ["craft", "badge:独家优惠:#00FF00"]},
        {"id": "m2", "tags": ["badge:限时免费:#FF0000"]},
    ], "modelPromotions": [{
        "id": "p1", "enabled": True, "modelIds": ["m1"],
        "badge": {"label": "限时免费", "color": "#FF0000"},
        "discount": {"discountedCredits": "0x", "factor": 0},
    }]}
    bl = conv.badge_labels(synth)
    check("m1 有标签", "m1" in bl, str(bl))
    check("m1 活动标签带折扣倍率",
          any("限时免费" in x and "0x" in x for x in bl.get("m1", [])), str(bl.get("m1")))
    check("m1 tag 标签保留（不同名）",
          any("独家优惠" in x for x in bl.get("m1", [])), str(bl.get("m1")))
    check("m2 tag 标签解析", "m2" in bl and "限时免费" in bl["m2"], str(bl.get("m2")))
    check("m2 无活动 → 纯 tag 文案", bl.get("m2") == ["限时免费"], str(bl.get("m2")))

    print("\n[4] 标签**不影响**内部模型名")
    check("display_of 未变", conv.display_of("hy3") == conv.display_of("hy3"))
    check("resolve_model 未变", conv.resolve_model("hy3") == "hy3")
    check("未知名原样透传", conv.resolve_model("no-such-model") == "no-such-model")

    # ================= ① 每日福利 =================
    print("\n[5] converter.claim_for_station 结构（mock httpx）")
    import httpx

    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    class _Client:
        def __init__(self, seq):
            self.seq = list(seq)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, **kw):
            return _Resp(self.seq.pop(0) if self.seq else {"code": 0, "data": {}})

    real_af, real_cred, real_client = (
        conv.find_auth_file, conv.CredentialManager, httpx.Client)
    try:
        # 未登录
        conv.find_auth_file = lambda st=None: None
        r = conv.claim_for_station("cn")
        check("未登录 → ok=False 且带原因", r["ok"] is False and r["error"], str(r))

        # 已领过
        class _Cred:
            def get_backend(self):
                return "https://example.invalid"
            def get_headers(self):
                return {}
        conv.find_auth_file = lambda st=None: Path("x.info")
        conv.CredentialManager = lambda p: _Cred()
        httpx.Client = lambda **kw: _Client([{"code": 0, "data": {"today_checked_in": True,
                                                                 "today_credit": 100,
                                                                 "streak_days": 3}}])
        r = conv.claim_for_station("cn")
        check("已领过 → ok=True, already=True",
              r["ok"] and r.get("already"), str(r))
        check("已领过带今日积分", r.get("credit") == 100, str(r))

        # 未领 → 领取成功
        httpx.Client = lambda **kw: _Client([
            {"code": 0, "data": {"today_checked_in": False}},
            {"code": 0, "data": {"credit": 120, "streak_days": 4}},
        ])
        r = conv.claim_for_station("intl")
        check("未领 → 领取成功", r["ok"] and not r.get("already"), str(r))
        check("返回积分", r.get("credit") == 120, str(r))
        check("返回站点", r.get("station") == "intl", str(r))

        # 上游报错
        httpx.Client = lambda **kw: _Client([
            {"code": 0, "data": {"today_checked_in": False}},
            {"code": 5001, "msg": "服务器繁忙"},
        ])
        r = conv.claim_for_station("cn")
        check("上游报错 → ok=False 且带 msg",
              not r["ok"] and "5001" in (r.get("error") or ""), str(r))

        # 幂等措辞 → 当已领
        httpx.Client = lambda **kw: _Client([
            {"code": 0, "data": {"today_checked_in": False}},
            {"code": 5002, "msg": "今天已签到"},
        ])
        r = conv.claim_for_station("cn")
        check("『今天已签到』→ already=True", r["ok"] and r.get("already"), str(r))

        # 真错误里也含「已」→ 不能被误判成已领取
        httpx.Client = lambda **kw: _Client([
            {"code": 0, "data": {"today_checked_in": False}},
            {"code": 5003, "msg": "活动已结束"},
        ])
        r = conv.claim_for_station("cn")
        check("『活动已结束』→ ok=False（不误判）", not r["ok"], str(r))

        # 网络异常
        def _boom(**kw):
            raise RuntimeError("boom")
        httpx.Client = _boom
        r = conv.claim_for_station("cn")
        check("网络异常被兜住", not r["ok"] and "boom" in (r.get("error") or ""), str(r))
    finally:
        conv.find_auth_file, conv.CredentialManager, httpx.Client = (
            real_af, real_cred, real_client)

    print("\n[6] mc_saas.claim_for_station（mock _req）")
    import mc_saas
    real_status, real_do, real_base = (
        mc_saas.get_checkin_status, mc_saas.do_checkin, mc_saas.current_base_api)
    try:
        mc_saas.get_checkin_status = lambda: {"checked_in": True}
        r = mc_saas.claim_for_station("cn")
        check("已签 → already", r["ok"] and r.get("already"), str(r))
        check("站点回填", r.get("station") == "cn", str(r))

        mc_saas.get_checkin_status = lambda: {"checked_in": False}
        mc_saas.do_checkin = lambda: {"credits": 100}
        r = mc_saas.claim_for_station("intl")
        check("未签 → 签到成功", r["ok"] and not r.get("already"), str(r))

        def _raise():
            raise RuntimeError("no session")
        _prev_station = mc_saas.STATION
        mc_saas.get_checkin_status = _raise
        r = mc_saas.claim_for_station(None)
        check("异常被兜住", not r["ok"] and "no session" in (r.get("error") or ""), str(r))
        # STATION 必须被还原（不能被 claim 改动遗留）
        check("STATION 已还原", mc_saas.STATION == _prev_station,
              f"{mc_saas.STATION!r} != {_prev_station!r}")
    finally:
        mc_saas.get_checkin_status, mc_saas.do_checkin = real_status, real_do

    print("\n[7] codearts2openai.claim_daily（mock）")
    import codearts2openai as ca
    real_ensure = ca.ensure_creds
    try:
        ca.ensure_creds = lambda prefer=None: (_ for _ in ()).throw(RuntimeError("无凭证"))
        r = ca.claim_daily()
        check("无凭证 → ok=False 且带原因", not r["ok"] and r.get("error"), str(r))
        check("claim_daily 是同步函数", not hasattr(r, "__await__"))
    finally:
        ca.ensure_creds = real_ensure

    print("\n[8] 上游模块都暴露了 claim 入口")
    check("converter.claim_for_station", hasattr(conv, "claim_for_station"))
    check("converter.checkin_status_for_station",
          hasattr(conv, "checkin_status_for_station"))
    check("mc_saas.claim_for_station", hasattr(mc_saas, "claim_for_station"))
    check("mc_saas.checkin_state", hasattr(mc_saas, "checkin_state"))
    check("codearts2openai.claim_daily", hasattr(ca, "claim_daily"))
    check("converter /v1/checkin 路由存在",
          any(getattr(r, "path", "") == "/v1/checkin" for r in conv.app.routes))
    check("converter /v1/model_badges 路由存在",
          any(getattr(r, "path", "") == "/v1/model_badges" for r in conv.app.routes))

    print(f"\n=====  PASS {PASS}  FAIL {FAIL}  SKIP {SKIP}  =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
