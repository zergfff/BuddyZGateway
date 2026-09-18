# -*- coding: utf-8 -*-
"""mc_saas — MonkeyCode SaaS 侧接口（钱包/签到），自包含。

会话取自桌面端 Roaming/com.chaitin.baizhi.monkeycode/monkeycode-cookies.json
（单 session cookie，无锁、无 DPAPI，可靠）。
移植自 reverse-proxy/upstream.py + session.py 相关部分。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

# 默认站点；实际以桌面端当前登录的版本为准（国内 monkeycode-ai.com /
# 国际 monkeycode-ai.net），由 current_base_api() 每次请求前解析。
DEFAULT_BASE_API = "https://monkeycode-ai.com"
BASE_API = DEFAULT_BASE_API

# 站点 → 服务端根域名。桌面端两版共用同一份配置文件，只换域名。
MC_STATION_HOSTS = {"cn": "https://monkeycode-ai.com",
                    "intl": "https://monkeycode-ai.net"}
# 由主模块按 GUI 的「使用版本」同步过来：None=自动(读文件) / "cn" / "intl"
STATION: str | None = None

EP_WALLET = "/api/v1/users/wallet"
EP_CHECKIN = "/api/v1/users/wallet/checkin"
EP_CAPTCHA_CHALLENGE = "/api/v1/public/captcha/challenge"
EP_CAPTCHA_REDEEM = "/api/v1/public/captcha/redeem"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

FNV_OFFSET32 = 2166136261
FNV_MASK = 0xFFFFFFFF


def station_of(value: str | None) -> str | None:
    """由 server/base_url 判断站点：含 .net → intl，含 .com → cn。"""
    s = str(value or "")
    if "monkeycode-ai.net" in s:
        return "intl"
    if "monkeycode-ai.com" in s:
        return "cn"
    return None


def current_base_api() -> str:
    """当前活动站点：国际版 https://monkeycode-ai.net / 国内版 https://monkeycode-ai.com。

    优先级：
      1. `STATION` 显式指定（GUI 的「使用版本」选 国内/国际）→ 强制用该站点；
      2. 否则跟随桌面端 monkeycode-ohmyagent-key.json 的 server 字段
         （切换版本时桌面端会重写该文件）——写死 .com 会让国际版账号查钱包/签到全挂；
      3. 都读不到时回退 DEFAULT_BASE_API。
    """
    if STATION in MC_STATION_HOSTS:
        return MC_STATION_HOSTS[STATION]
    home = Path.home()
    for base in (os.environ.get("APPDATA"), home / "AppData" / "Roaming"):
        if not base:
            continue
        p = Path(base) / "com.chaitin.baizhi.monkeycode" / "monkeycode-ohmyagent-key.json"
        if not p.is_file():
            continue
        try:
            s = (json.loads(p.read_text(encoding="utf-8")) or {}).get("server")
        except Exception:
            continue
        if s:
            return str(s).rstrip("/")
    return DEFAULT_BASE_API


def _fnv1a(s):
    h = FNV_OFFSET32
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h + (h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24)) & FNV_MASK
    return h


def _prng(seed, length):
    state = _fnv1a(seed)
    out = []
    while len(out) * 8 < length:
        state ^= (state << 13) & FNV_MASK
        state ^= state >> 17
        state ^= (state << 5) & FNV_MASK
        state &= FNV_MASK
        out.append(f"{state:08x}")
    return "".join(out)[:length]


def _solve_sub(token, idx, s_len, d_len):
    salt = _prng(token + str(idx), s_len)
    target = _prng(token + str(idx) + "d", d_len)
    for n in range(1 << 26):
        digest = hashlib.sha256((salt + str(n)).encode()).hexdigest()
        if digest[:d_len] == target:
            return n
    raise RuntimeError(f"sub-challenge {idx} unsolved (d={d_len})")


def _cookie_file() -> Path | None:
    home = Path.home()
    for base in (os.environ.get("APPDATA"), home / "AppData" / "Roaming"):
        if not base:
            continue
        p = Path(base) / "com.chaitin.baizhi.monkeycode" / "monkeycode-cookies.json"
        if p.is_file():
            return p
    return None


def load_session_cookies() -> dict:
    p = _cookie_file()
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data if isinstance(data, list) else data.get("cookies", [])
    out = {}
    for c in items:
        if isinstance(c, dict) and c.get("name") and c.get("value"):
            out[c["name"]] = c["value"]
    return out


def _req(method: str, path: str, body=None):
    cookies = load_session_cookies()
    if not cookies:
        raise RuntimeError("no monkeycode session (monkeycode-cookies.json 缺失或为空)")
    base = current_base_api()
    headers = {"User-Agent": _UA, "Referer": base + "/",
               "Accept": "application/json",
               "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v)}
    if body is not None:
        headers["Content-Type"] = "application/json"
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0),
                      follow_redirects=True) as c:
        r = c.request(method, base + path, json=body, headers=headers)
    if r.status_code in (401, 403):
        raise RuntimeError(f"session 无效（{r.status_code}，请重登 MonkeyCode 桌面端）")
    try:
        obj = r.json()
    except Exception:
        raise RuntimeError(f"上游返回非 JSON（{r.status_code}）") from None
    if isinstance(obj, dict) and "code" in obj:
        if obj.get("code") != 0:
            raise RuntimeError(f"upstream code={obj.get('code')} msg={obj.get('message', '')}")
        return obj.get("data")
    return obj


def get_wallet() -> dict:
    """{'balance': 64079, 'daily_token_balance': N, 'daily_token_limit': N, ...}"""
    return _req("GET", EP_WALLET) or {}


def get_checkin_status() -> dict:
    """{'checked_in': bool}"""
    return _req("GET", EP_CHECKIN) or {}


def _solve_captcha() -> str:
    ch = _req("POST", EP_CAPTCHA_CHALLENGE)
    if not isinstance(ch, dict):
        raise RuntimeError("captcha challenge: bad response")
    cfg = ch.get("challenge", {})
    c, s, d = int(cfg.get("c", 0) or 0), int(cfg.get("s", 0) or 0), int(cfg.get("d", 0) or 0)
    token = ch.get("token", "")
    if c <= 0 or s <= 0 or d <= 0 or not token:
        raise RuntimeError(f"captcha invalid config: {cfg}")
    solutions = [_solve_sub(token, i, s, d) for i in range(1, c + 1)]
    rd = _req("POST", EP_CAPTCHA_REDEEM, {"token": token, "solutions": solutions})
    if not isinstance(rd, dict) or not rd.get("success") or not rd.get("token"):
        raise RuntimeError("captcha redeem failed")
    return rd["token"]


def do_checkin() -> dict:
    cap_token = _solve_captcha()
    return _req("POST", EP_CHECKIN, {"captcha_token": cap_token}) or {}


def claim_for_station(station: str | None = None) -> dict:
    """**按站点**签到，返回 {ok, station, already?, data?, error?}。

    MonkeyCode 两版共用同一份 cookie/key 文件（只有 `server` 字段不同），
    所以同一时刻只有**当前登录那一版**能签到。station 用于强制把请求发到
    该站点的域名；cookie 不匹配时上游会报错，这里如实返回。
    """
    global STATION
    prev = STATION
    out = {"ok": False, "station": station or "", "already": False, "error": ""}
    try:
        if station in MC_STATION_HOSTS:
            STATION = station
        out["station"] = station or (STATION or "")
        st = get_checkin_status() or {}
        if st.get("checked_in"):
            out.update({"ok": True, "already": True, "data": st})
            return out
        res = do_checkin() or {}
        out.update({"ok": True, "data": res})
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        STATION = prev


def checkin_state(station: str | None = None) -> dict:
    """只查签到状态（不领取）。"""
    global STATION
    prev = STATION
    try:
        if station in MC_STATION_HOSTS:
            STATION = station
        return {"ok": True, "data": get_checkin_status() or {}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        STATION = prev
