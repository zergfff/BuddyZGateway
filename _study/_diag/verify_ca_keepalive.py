# -*- coding: utf-8 -*-
"""
verify_ca_keepalive.py — CodeArts 登录体验优化的离线回归。

背景（用户反馈）：每次登录都要点「授权」、还得开桌面端，很麻烦。
本次优化：
  ① 状态落盘原子化 + 备份（refresh_token 单次有效，写丢就得重登）
  ② _state_patch 只合并指定键（避免整体覆盖把主会话冲掉）
  ③ prewarm() 主动续期 —— 运行期间保活，不再闲置到过期
  ④ auto_login() 凭据失效时自动拉起授权页（免点按钮）
  ⑤ 落盘失败不再静默吞掉

全部离线（不发真实授权请求），用临时数据目录隔离。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO / "_study" / "codearts2openai"))

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


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="ca-keepalive-"))
    os.environ["BUDDYZ_DATA_DIR"] = str(sandbox)
    import codearts2openai as ca

    print("== CodeArts 会话保活 / 自动授权 ==")

    # ---------- ① 原子写 + 备份 ----------
    print("\n[1] _save_state 原子写 + 备份")
    fp = ca._state_file()
    check("state 文件路径可用", fp is not None, str(fp))
    check("初始无文件", not fp.is_file())

    ok = ca._save_state({"refresh_token": "RT1", "dpop_priv": {"k": 1}})
    check("落盘成功返回 True", ok is True)
    check("主文件已生成", fp.is_file())
    check("内容正确",
          json.loads(fp.read_text(encoding="utf-8"))["refresh_token"] == "RT1")

    # 第二次保存 → 应产生 .bak，且 .bak 是**上一版**
    time.sleep(0.02)
    ca._save_state({"refresh_token": "RT2", "dpop_priv": {"k": 1}})
    bak = fp.with_suffix(".bak")
    check("生成备份 .bak", bak.is_file(), str(bak))
    check("备份是上一版（RT1）",
          json.loads(bak.read_text(encoding="utf-8"))["refresh_token"] == "RT1")
    check("主文件是当前版（RT2）",
          json.loads(fp.read_text(encoding="utf-8"))["refresh_token"] == "RT2")

    # 主文件损坏 → 自动回退到备份
    fp.write_text("{ 这不是合法 JSON", encoding="utf-8")
    st = ca._load_state()
    check("主文件损坏时回退到备份", st.get("refresh_token") == "RT1", str(st))

    # 恢复
    ca._save_state({"refresh_token": "RT2", "dpop_priv": {"k": 1}})

    # ---------- ② _state_patch 不覆盖其它键 ----------
    print("\n[2] _state_patch 只合并指定键")
    ca._save_state({"refresh_token": "MAIN", "pool": [{"label": "p1"}]})
    ca._state_patch(ticket={"access": "AK"})
    st = json.loads(fp.read_text(encoding="utf-8"))
    check("新键写入", st.get("ticket", {}).get("access") == "AK")
    check("主会话未被冲掉", st.get("refresh_token") == "MAIN", str(st)[:160])
    check("号池未被冲掉", st.get("pool") == [{"label": "p1"}], str(st)[:160])

    # ---------- ③ BUDDYZ_DATA_DIR 未设 → 明确失败，不静默 ----------
    print("\n[3] 未配置数据目录时不静默")
    saved = os.environ.pop("BUDDYZ_DATA_DIR")
    try:
        check("_state_file() 返回 None", ca._state_file() is None)
        check("_save_state() 返回 False（明确失败）", ca._save_state({"a": 1}) is False)
    finally:
        os.environ["BUDDYZ_DATA_DIR"] = saved

    # ---------- ④ _expires_in ----------
    print("\n[4] _expires_in")
    check("None → -1", ca._expires_in(None) == -1)
    check("空对象 → -1", ca._expires_in({}) == -1)
    check("非法字符串 → -1", ca._expires_in({"expiration": "nonsense"}) == -1)
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    left = ca._expires_in({"expiration": future})
    check("未来 2 小时 → 约 7200 秒", 7000 < left <= 7200, str(left))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    check("过去 1 小时 → 负数", ca._expires_in({"expiration": past}) < 0)

    # ---------- ⑤ prewarm 三条分支 ----------
    print("\n[5] prewarm（主动续期）")
    import types
    real_ensure = ca.ensure_creds

    # 5a 凭据还很久 → skip
    ca.remember_cred({"expiration": future})
    ca._PREWARM_AT[0] = 0.0
    r = ca.prewarm(lead_seconds=1200)
    check("剩余充足 → skip", r["action"] == "skip", str(r))
    check("返回剩余秒数", r["expires_in"] > 0, str(r))

    # 5b 快过期 → 调 ensure_creds 续期
    soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
    ca.remember_cred({"expiration": soon})
    ca._PREWARM_AT[0] = 0.0
    calls = {"n": 0}

    def fake_ensure(prefer="dpop", conv_key=None, _c=calls):
        _c["n"] += 1
        return {"expiration": future, "access_key_id": "AK", "secret_access_key": "SK"}
    ca.ensure_creds = fake_ensure
    try:
        r = ca.prewarm(lead_seconds=1200)
        check("快过期 → refreshed", r["action"] == "refreshed", str(r))
        check("确实调了 ensure_creds", calls["n"] == 1, str(calls))
        check("续期后剩余为正", r["expires_in"] > 0, str(r))
    finally:
        ca.ensure_creds = real_ensure

    # 5c 节流：短时间内再调 → skip（不再打 STS）
    ca._PREWARM_AT[0] = time.time()
    r = ca.prewarm(lead_seconds=1200)
    check("节流内 → skip", r["action"] == "skip", str(r))

    # 5d 失败分支
    ca._PREWARM_AT[0] = 0.0
    # 显式清空缓存凭据（remember_cred(None) 按设计只忽略、不清除已有值）
    with ca._CRED_LOCK:
        ca._LAST_CRED["cred"] = None

    def boom(prefer="dpop", conv_key=None):
        raise RuntimeError("no creds")
    ca.ensure_creds = boom
    try:
        r = ca.prewarm(lead_seconds=1200)
        check("失败 → failed 且带错误", r["action"] == "failed" and "no creds" in r["error"], str(r))
        check("失败不抛异常", isinstance(r, dict))
    finally:
        ca.ensure_creds = real_ensure
    ca._PREWARM_AT[0] = 0.0

    # ---------- ⑥ needs_login ----------
    print("\n[6] needs_login")
    ca.ensure_creds = lambda *a, **k: {"expiration": future}
    try:
        check("有凭据 → False", ca.needs_login() is False)
    finally:
        ca.ensure_creds = real_ensure
    ca.ensure_creds = boom
    try:
        check("无凭据 → True", ca.needs_login() is True)
    finally:
        ca.ensure_creds = real_ensure

    # ---------- ⑦ auto_login（不真开浏览器）----------
    print("\n[7] auto_login（免点按钮）")
    real_build = ca.build_authorize_url
    try:
        res = ca.auto_login(19333, station="china", open_browser=False, log=lambda m: None)
        check("返回 ok", res.get("ok") is True, str(res))
        check("有授权 URL", "/portal/authorize" in (res.get("url") or ""), str(res)[:160])
        check("URL 带 ticket_id", "ticket_id=" in res["url"])
        check("URL 带回调地址", "auth_callback_url=" in res["url"])
        check("URL 带 PKCE", "code_challenge=" in res["url"])
        check("返回站点", res.get("station") == "china", str(res.get("station")))
        check("open_browser=False 时不打开", res.get("opened") is False)
        check("返回值带身份标签", bool(res.get("identity")), str(res)[:140])
        check("返回值带身份 key",
              res.get("identity_key") in ("snap_AIIDE", "snap_vscode"), str(res.get("identity_key")))
        # 国际站要多一个 quickcompfalg=1
        res2 = ca.auto_login(19333, station="international", open_browser=False)
        check("国际站带 quickcompfalg=1", "quickcompfalg=1" in res2["url"], res2["url"][-80:])
        # build_authorize_url 抛错时要被兜住
        ca.build_authorize_url = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        res3 = ca.auto_login(19333, open_browser=False)
        check("build 抛错时 ok=False", res3.get("ok") is False and res3.get("error"), str(res3))
    finally:
        ca.build_authorize_url = real_build

    # ---------- ⑦a 授权身份（client_id / uri_scheme / plugin-name）----------
    print("\n[7a] 授权身份 —— 「授权成功但换不到票」的根因")
    print(f"      本机凭据库: {[l for l, _ in ca._bootstrap_all()]}")
    print(f"      插件目录  : {ca._vscode_ext_dir()}")
    print(f"      插件版本  : {ca._vscode_ext_version()}")

    # 两套身份常量
    check("desktop 身份", ca.IDENTITIES["desktop"]["plugin_name"] == "snap_AIIDE"
          and ca.IDENTITIES["desktop"]["client_id"] == "codearts-agent",
          str(ca.IDENTITIES["desktop"]))
    check("vscode 身份", ca.IDENTITIES["vscode"]["plugin_name"] == "snap_vscode"
          and ca.IDENTITIES["vscode"]["client_id"] == "vscode-codebot",
          str(ca.IDENTITIES["vscode"]))

    # 自动跟随凭据来源
    ca.CONFIG["identity"] = ""
    ca._STORE_CACHE["ts"] = 0.0            # 清缓存，确保按真实来源判定
    auto_ident = ca.current_identity()
    check("auto 会返回合法身份",
          auto_ident.get("plugin_name") in ("snap_AIIDE", "snap_vscode"),
          str(auto_ident))
    _stores = [l for l, _ in ca._bootstrap_all()]
    if any("VS Code" in l or "codebot" in l.lower() for l in _stores):
        check("有 VS Code 凭据库 → 自动选 vscode 身份",
              auto_ident["plugin_name"] == "snap_vscode", str(auto_ident))
        check("vscode 身份带真实插件版本",
              bool(auto_ident.get("plugin_version")), str(auto_ident))
    else:
        check("无 VS Code 凭据库 → 回退桌面端身份",
              auto_ident["plugin_name"] == "snap_AIIDE", str(auto_ident))

    # 显式覆盖
    ca.CONFIG["identity"] = "desktop"
    d = ca.current_identity()
    check("显式 desktop 生效",
          d["plugin_name"] == "snap_AIIDE" and d["client_id"] == "codearts-agent", str(d))
    ca.CONFIG["identity"] = "vscode"
    v = ca.current_identity()
    check("显式 vscode 生效",
          v["plugin_name"] == "snap_vscode" and v["client_id"] == "vscode-codebot", str(v))
    ca.CONFIG["identity"] = ""

    # 授权 URL 必须用身份里的 client_id / uri_scheme / plugin-name
    import urllib.parse as _up
    for key, expect in (("desktop", ("codearts-agent", "codearts-agent", "snap_AIIDE")),
                        ("vscode", ("vscode-codebot", "vscode-codebot", "snap_vscode"))):
        ca.CONFIG["identity"] = key
        info = ca.build_authorize_url(19500, station="china")
        q = dict(_up.parse_qsl(_up.urlparse(info["url"]).query))
        check(f"[{key}] client_id 正确", q.get("client_id") == expect[0], str(q.get("client_id")))
        check(f"[{key}] uri_scheme 正确", q.get("uri_scheme") == expect[1], str(q.get("uri_scheme")))
        check(f"[{key}] plugin-name 正确", q.get("plugin-name") == expect[2], str(q.get("plugin-name")))
        check(f"[{key}] 有 plugin-version", bool(q.get("plugin-version")), str(q.get("plugin-version")))
        check(f"[{key}] 有 ticket_id（UUID v4）",
              len(str(q.get("ticket_id"))) == 36 and str(q.get("ticket_id")).count("-") == 4,
              str(q.get("ticket_id")))
        check(f"[{key}] 有回调地址", "auth_callback_url" in q)
        # 身份必须记进 pending，换票时才能用同一套
        pend = ca._OAUTH_PENDING.get(info["ticket_id"]) or {}
        check(f"[{key}] 身份记入 pending",
              (pend.get("identity") or {}).get("plugin_name") == expect[2], str(pend)[:120])
        check(f"[{key}] URL 回传身份标签", bool(info.get("identity")), str(info)[:120])
    ca.CONFIG["identity"] = ""

    # 换票头必须用同一身份（用假 httpx 抓 headers）
    print("      换票头 plugin-name 校验：")
    import httpx as _hx
    _real_client = _hx.Client
    _seen = {}

    class _FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"credential": {"access": "A", "secret": "S", "securitytoken": "T"}}

    class _FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            _seen["headers"] = dict(headers or {})
            return _FakeResp()
    _hx.Client = lambda **kw: _FakeClient()
    try:
        for key, expect in (("desktop", "snap_AIIDE"), ("vscode", "snap_vscode")):
            ca.CONFIG["identity"] = key
            cred, code, msg = ca._ticket_get("tid", "sec", "china")
            check(f"[{key}] 换票头 plugin-name={expect}",
                  _seen.get("headers", {}).get("plugin-name") == expect,
                  str(_seen.get("headers")))
            check(f"[{key}] 换票头带 plugin-version",
                  bool(_seen.get("headers", {}).get("plugin-version")),
                  str(_seen.get("headers")))
            check(f"[{key}] 假响应能解析出 credential", cred is not None)
        # 显式传 identity 时优先用它
        ca.CONFIG["identity"] = "desktop"
        ca._ticket_get("tid", "sec", "china", ca.IDENTITIES["vscode"])
        check("显式 identity 参数优先",
              _seen["headers"].get("plugin-name") == "snap_vscode",
              str(_seen["headers"]))
    finally:
        _hx.Client = _real_client
        ca.CONFIG["identity"] = ""

    # 插件目录/版本探测
    if ca._vscode_ext_dir():
        check("插件目录存在", ca._vscode_ext_dir().is_dir())
        check("插件版本可读", bool(ca._vscode_ext_version()), ca._vscode_ext_version())
    else:
        check("未装插件时不崩", ca._vscode_ext_version() == "")

    # ---------- ⑦b cred_status（只读真实可用性）----------
    print("\n[7b] cred_status（GUI 用它显示真实可用性）")
    ca._PREWARM_AT[0] = 0.0
    with ca._CRED_LOCK:
        ca._LAST_CRED["cred"] = {"expiration": future}
    ca._PREWARM_LAST.update({"ok": True, "action": "refreshed", "at": time.time(), "error": ""})
    cs = ca.cred_status()
    for k in ("live", "expires_in", "has_session", "has_ticket",
              "last_ok", "last_action", "last_error", "writable", "has_backup"):
        check(f"cred_status.{k} 存在", k in cs, str(list(cs.keys())))
    check("有可用凭据 → live=True", cs["live"] is True, str(cs))
    check("剩余秒数为正", cs["expires_in"] > 0, str(cs))
    check("反映最近一次成功", cs["last_ok"] is True, str(cs))
    # 掉了之后
    with ca._CRED_LOCK:
        ca._LAST_CRED["cred"] = None
    ca._PREWARM_LAST.update({"ok": False, "action": "failed",
                             "at": time.time(), "error": "会话已失效"})
    cs2 = ca.cred_status()
    check("无凭据 → live=False", cs2["live"] is False, str(cs2))
    check("反映最近一次失败", cs2["last_ok"] is False, str(cs2))
    check("带错误信息", "失效" in (cs2["last_error"] or ""), str(cs2))
    ca._PREWARM_LAST.update({"ok": None, "action": "failed", "at": 0.0, "error": ""})

    # ---------- ⑧ 路由存在 ----------
    print("\n[8] HTTP 路由")
    paths = {getattr(r, "path", "") for r in ca.app.routes}
    check("/v1/keepalive 存在", "/v1/keepalive" in paths, str(sorted(paths))[:200])
    check("/v1/auth/status 存在", "/v1/auth/status" in paths)
    check("/oauth/callback 存在", "/oauth/callback" in paths)

    # 收尾
    shutil.rmtree(sandbox, ignore_errors=True)
    os.environ.pop("BUDDYZ_DATA_DIR", None)
    print(f"\n=====  PASS {PASS}  FAIL {FAIL}  =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
