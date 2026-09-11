# -*- coding: utf-8 -*-
"""codearts2openai — 把华为云 CodeArts Agent（码道）封装成标准 OpenAI 兼容 API。

对外：
  GET  /health                探活（含余额/模型数）
  GET  /v1/models             模型目录（静态 4 个，honor exposed_models）
  POST /v1/chat/completions   对话（非流式；流式转非流式返回）
  GET  /v1/balance            10M token 池余额
  POST /v1/claim              每日福利领取

对内：DPoP 自动续期（refresh_token 单次轮转，持久化） + SDK-HMAC-SHA256
AK 签名 + x-ot-* 头，直调 snap-access / opengw。
免费模型与桌面端共用每日 10M token 池。

依赖：fastapi + uvicorn + httpx（标准库完成 DPoP/解密/AK 签名，无第三方）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CONFIG = {
    "local_api_key": "",
    "log_path": None,
    "exposed_models": [],
    "port": 9100,  # 服务端口（OAuth 回调地址用它）
}

_OAUTH_PENDING: dict = {}


# 显示名映射（复用 WorkBuddy 桌面端模型表；未知回退原名；内部仍用线上名）
CA_DISPLAY_MAP = {
    "auto": "Auto",
    "glm-5": "GLM-5",
    "glm-5.1": "GLM-5.1",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-5.2": "GLM-5.2",
    "kimi-k2.5": "Kimi-K2.5",
    "kimi-k2.6": "Kimi-K2.6",
    "kimi-k2.7-code": "Kimi K2.7 Code",
    "kimi-k2.7-code-highspeed": "Kimi K2.7 Code HighSpeed",
    "minimax-m2.5": "MiniMax-M2.5",
    "minimax-m2.7": "MiniMax-M2.7",
    "minimax-m3": "MiniMax-M3",
    "deepseek-v4-flash": "DeepSeek-V4-Flash",
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
    "deepseek-v4-flash-202605": "DeepSeek-V4-Flash 原厂直供",
    "deepseek-v4-pro-202606": "DeepSeek-V4-Pro 原厂直供",
    "tc-code-latest": "Auto",
    "hy3": "Hy3",
    "hy3-preview": "Hy3 preview",
    "hunyuan-2.0-instruct": "Tencent HY 2.0 Instruct",
    "hunyuan-2.0-thinking": "Tencent HY 2.0 Think",
    "hunyuan-t1": "Hunyuan-T1",
    "hunyuan-turbos": "Hunyuan-TurboS",
    "glm-4.7": "GLM-4.7",
    "glm-4.6v": "GLM-4.6V",
    "kimi-k3": "Kimi K3",
    "kimi-k2-0905-preview": "Kimi K2 (2024-09-05 Preview)",
    "kimi-k2-turbo-preview": "Kimi K2 Turbo Preview",
    "kimi-k2-thinking-turbo": "Kimi K2 Thinking Turbo",
    "kimi-k2-thinking": "Kimi K2 Thinking",
    "kimi-k2-0711-preview": "Kimi K2 (2024-07-11 Preview)",
    "MiniMax-M2.5": "MiniMax-M2.5",
    "MiniMax-M2.5-highspeed": "MiniMax-M2.5 High Speed",
    "MiniMax-M2.1": "MiniMax-M2.1",
    "MiniMax-M2.1-highspeed": "MiniMax-M2.1 High Speed",
    "MiniMax-M2": "MiniMax-M2",
    "deepseek-chat": "DeepSeek-V4 Flash (Chat alias)",
    "deepseek-reasoner": "DeepSeek-V4 Flash (Reasoner alias)",
    "gpt-5.4": "GPT-5.4",
    "gpt-5.3-codex": "GPT-5.3 Codex",
    "gpt-5": "GPT-5",
    "gpt-5-mini": "GPT-5 mini",
    "gpt-5-nano": "GPT-5 nano",
    "gpt-4.1": "GPT-4.1",
    "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o mini",
    "o3": "o3",
    "o3-mini": "o3-mini",
    "o1": "o1",
    "gemini-3.5-flash": "Gemini 3.5 Flash",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "gemini-3.1-flash": "Gemini 3.1 Flash",
    "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite",
    "gemini-3-flash-preview": "Gemini 3 Flash (Preview)",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite"
}

def display_of(name):
    return CA_DISPLAY_MAP.get(name, name) if name else name

def resolve_model(name):
    if not name: return name
    if name in CA_DISPLAY_MAP: return name
    return {v: k for k, v in CA_DISPLAY_MAP.items()}.get(name, name)

DEFAULT_MODELS = ["glm-5.2", "deepseek-v4-flash-0731",
                  "deepseek-v4-pro-0813", "glm-5.3-flash"]
CLIENT_ID = "codearts-agent"
STS = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
SNAP = "https://snap-access.cn-north-4.myhuaweicloud.com"
OPENGW = "https://opengw.developer.huaweicloud.com"
APP_VERSION = "26.8.300"


def _log(line: str):
    lp = CONFIG.get("log_path")
    if not lp:
        return
    try:
        with open(lp, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
    except OSError:
        pass


def _data_path() -> Path | None:
    d = os.environ.get("BUDDYZ_DATA_DIR", "")
    return Path(d) if d else None


def _state_file() -> Path | None:
    d = _data_path()
    return d / "ca_codearts.json" if d else None


def _load_state() -> dict:
    fp = _state_file()
    if fp and fp.is_file():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 兼容早期 Temp 轮转文件
    tmp = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "ca_state.json"
    try:
        if tmp.is_file():
            return json.loads(tmp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(st: dict):
    fp = _state_file()
    if not fp:
        return
    try:
        fp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _bootstrap_from_desktop() -> dict | None:
    """从桌面端 secret 解出 loginContext（DPoP 密钥/PKCE/refresh_token）。只返回，不落盘（由调用方合并保存）。"""
    try:
        import win32crypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    try:
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        ls = json.loads((Path(appdata) / "codearts-agent" / "Local State").read_text(encoding="utf-8"))
        key = win32crypt.CryptUnprotectData(
            base64.b64decode(ls["os_crypt"]["encrypted_key"])[5:], None, None, None, 0)[1]
        import sqlite3
        con = sqlite3.connect(
            "file:" + str(Path(appdata) / "codearts-agent" / "User" / "globalStorage" /
                          "state.vscdb") + "?mode=ro",
            uri=True, timeout=5)
        try:
            row = con.execute(
                "select value from ItemTable where key like 'secret://%'").fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            _log("[ca] 桌面端未找到登录会话（secret 条目为空）——需打开 CodeArts 登录一次")
            return None
        raw = bytes(json.loads(row[0])["data"])
        sess = json.loads(AESGCM(key).decrypt(raw[3:15], raw[15:], None))
        lc = sess.get("loginContext") or {}
        st = {"refresh_token": sess.get("refresh_token", ""),
              "dpop_priv": (lc.get("dpopKeyPair") or {}).get("privateKeyJwk"),
              "dpop_pub": (lc.get("dpopKeyPair") or {}).get("publicKeyJwk"),
              "verifier": (lc.get("pkcePair") or {}).get("codeVerifier", "")}
        if st["refresh_token"] and st["dpop_priv"]:
            return st
        _log("[ca] 桌面端会话缺少 refresh_token/DPoP 密钥——需重新登录桌面端")
    except Exception as e:  # noqa: BLE001
        _log(f"[ca] 桌面端会话读取失败: {e}")
    return None


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _dpop_jwt(priv_jwk: dict, pub_jwk: dict, htm: str, htu: str) -> str:
    import jwt as _pyjwt
    from cryptography.hazmat.primitives.asymmetric.ec import (
        EllipticCurvePrivateNumbers, EllipticCurvePublicNumbers, SECP256R1)
    from cryptography.hazmat.backends import default_backend
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    d = int.from_bytes(base64.urlsafe_b64decode(pad(priv_jwk["d"])), "big")
    x = int.from_bytes(base64.urlsafe_b64decode(pad(priv_jwk["x"])), "big")
    y = int.from_bytes(base64.urlsafe_b64decode(pad(priv_jwk["y"])), "big")
    nums = EllipticCurvePrivateNumbers(d, EllipticCurvePublicNumbers(x, y, SECP256R1()))
    priv = nums.private_key(default_backend())
    import cryptography.hazmat.primitives.serialization as ser
    pem = priv.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    payload = {"htm": htm, "htu": htu, "iat": int(time.time()), "jti": secrets.token_hex(16)}
    return _pyjwt.encode(payload, pem, algorithm="ES256",
                         headers={"typ": "dpop+jwt", "alg": "ES256", "jwk": pub_jwk})


def _need_refresh(cred: dict) -> bool:
    try:
        exp = datetime.fromisoformat((cred.get("expiration") or "").replace("Z", "+00:00"))
        return (exp - datetime.now(timezone.utc)).total_seconds() < 300
    except Exception:
        return True


def _locked_state_update(fn):
    """独立 lock 文件加锁（Windows 文件锁是强制性的，不能锁 state 文件本身）。"""
    try:
        import msvcrt
    except ImportError:
        return fn()
    fp = _state_file()
    if fp is None:
        return fn()
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


def _do_refresh(st: dict):
    proof = _dpop_jwt(st["dpop_priv"], st["dpop_pub"], "POST", STS)
    with httpx.Client(timeout=30) as c:
        r = c.post(STS, data={"client_id": CLIENT_ID, "code_verifier": st["verifier"],
                              "grant_type": "refresh_token", "refresh_token": st["refresh_token"]},
                   headers={"DPoP": proof, "Content-Type": "application/x-www-form-urlencoded"})
    body = r.json()
    if r.status_code != 200 or "credentials" not in body:
        raise RuntimeError(f"refresh 失败({r.status_code}): {str(body)[:200]}")
    st["last"] = body  # 含新 refresh_token（轮转）
    _save_state(st)
    _log("[ca] 会话已续期")
    return body["credentials"]


def _dpop_candidates() -> list:
    """DPoP 候选链：data state > Temp 轮转文件+桌面端密钥 > 桌面端整批。"""
    cands = []
    st = _load_state()
    if st.get("refresh_token") and st.get("dpop_priv"):
        cands.append({"refresh_token": st["refresh_token"], "dpop_priv": st["dpop_priv"],
                      "dpop_pub": st["dpop_pub"], "verifier": st.get("verifier", ""),
                      "save_to": "data", "label": "data-state"})
    # Temp 轮转文件（token） + 桌面端 loginContext（密钥）
    try:
        tmp = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "ca_state.json"
        if tmp.is_file():
            tj = json.loads(tmp.read_text(encoding="utf-8"))
            if tj.get("refresh_token"):
                desk = _bootstrap_from_desktop() or {}
                cands.append({"refresh_token": tj["refresh_token"],
                              "dpop_priv": desk.get("dpop_priv"), "dpop_pub": desk.get("dpop_pub"),
                              "verifier": desk.get("verifier", ""),
                              "save_to": "data", "label": "temp+desktop-keys"})
    except Exception:
        pass
    desk = _bootstrap_from_desktop() or {}
    if desk.get("refresh_token") and desk.get("dpop_priv"):
        cands.append({"refresh_token": desk["refresh_token"], "dpop_priv": desk["dpop_priv"],
                      "dpop_pub": desk["dpop_pub"], "verifier": desk.get("verifier", ""),
                      "save_to": "data", "label": "desktop-file"})
    # 去重（同一 token 只试一次）
    seen, out = set(), []
    for c in cands:
        if c["refresh_token"] not in seen and c.get("dpop_priv"):
            seen.add(c["refresh_token"])
            out.append(c)
    return out


def _ticket_creds() -> dict | None:
    st = _load_state()
    tk = st.get("ticket") or {}
    if not (tk.get("access") and tk.get("secret")):
        return None
    try:
        exp = datetime.fromisoformat((tk.get("expires_at") or "").replace("Z", "+00:00"))
        if (exp - datetime.now(timezone.utc)).total_seconds() < 300:
            return None
    except Exception:
        return None
    return {"access_key_id": tk["access"], "secret_access_key": tk["secret"],
            "security_token": tk.get("securitytoken", ""), "expiration": tk.get("expires_at")}


BENEFIT_MODELS = {"deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.3-flash"}


def ensure_creds(prefer: str = "dpop") -> dict:
    """prefer: dpop=桌面链（默认）| ticket=独立 OAuth。任一条走不通自动换另一条试。"""
    def _run():
        order = ["ticket", "dpop"] if prefer == "ticket" else ["dpop", "ticket"]

        def _try_dpop():
            for cand in _dpop_candidates():
                try:
                    proof = _dpop_jwt(cand["dpop_priv"], cand["dpop_pub"], "POST", STS)
                    with httpx.Client(timeout=30) as c:
                        r = c.post(STS, data={"client_id": CLIENT_ID, "code_verifier": cand["verifier"],
                                              "grant_type": "refresh_token",
                                              "refresh_token": cand["refresh_token"]},
                                   headers={"DPoP": proof, "Content-Type": "application/x-www-form-urlencoded"})
                    body = r.json()
                    if r.status_code == 200 and "credentials" in body:
                        st = _load_state()
                        st.update({"refresh_token": body.get("refresh_token", cand["refresh_token"]),
                                   "dpop_priv": cand["dpop_priv"], "dpop_pub": cand["dpop_pub"],
                                   "verifier": cand["verifier"], "last": body})
                        _save_state(st)
                        _log(f"[ca] DPoP 续期成功（{cand['label']}）")
                        return body["credentials"]
                    _log(f"[ca] DPoP 候选 {cand['label']} 失败: {str(body)[:120]}")
                except Exception as e:  # noqa: BLE001
                    _log(f"[ca] DPoP 候选 {cand['label']} 异常: {e}")
                    continue
            return None

        def _try_ticket():
            tk = _ticket_creds()
            if tk:
                _log("[ca] 使用 ticket 独立凭证")
            return tk

        for which in order:
            cred = _try_ticket() if which == "ticket" else _try_dpop()
            if cred:
                return cred
        raise RuntimeError("CodeArts 会话已失效：请打开 CodeArts 桌面端登录一次，"
                           "或点面板「授权」重新授权（余额/签到用 ticket 链，对话用 DPoP 链）")
    return _locked_state_update(_run)


def build_authorize_url(port: int) -> dict:
    """生成授权 URL（PKCE 存内存待回调配对）。"""
    verifier = _b64u(secrets.token_bytes(64))
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    ticket = secrets.token_hex(32)
    _OAUTH_PENDING[ticket] = {"verifier": verifier, "at": time.time()}
    params = {"theme": "dark", "locale": "zh-cn", "uri_scheme": CLIENT_ID,
              "client_id": CLIENT_ID, "port": str(port),
              "code_challenge": challenge, "code_challenge_method": "S256",
              "ticket_id": ticket, "plugin-name": "snap_AIIDE", "plugin-version": "5.3.0"}
    url = ("https://codearts.huaweicloud.com/portal/authorize?"
           + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items()))
    return {"url": url, "ticket_id": ticket}


def _exchange_ticket(ticket_id: str, secret: str) -> dict:
    with httpx.Client(timeout=30) as c:
        r = c.get(SNAP + "/snap-manager/v1/login/ticket",
                  params={"ticket_id": ticket_id, "secret": secret},
                  headers={"Content-Type": "application/json;charset=UTF-8",
                           "plugin-name": "snap_AIIDE", "plugin-version": "5.3.0"})
    body = r.json()
    if r.status_code != 200 or "credential" not in body:
        raise RuntimeError(f"ticket 换票失败({r.status_code}): {str(body)[:200]}")
    return body["credential"]


def _ensure_creds_dpop() -> dict:
    def _run():
        st = _load_state()
        if not st.get("dpop_priv"):
            desk = _bootstrap_from_desktop() or {}
            for k in ("dpop_priv", "dpop_pub", "verifier", "refresh_token"):
                st.setdefault(k, desk.get(k))
            _save_state(st)
        cred = (st.get("last") or {}).get("credentials") if isinstance(st.get("last"), dict) else None
        if cred and not _need_refresh(cred):
            return cred
        if not st.get("refresh_token"):
            raise RuntimeError("无 CodeArts 会话：请打开 CodeArts 桌面端登录一次（或走 OAuth 授权）")
        try:
            return _do_refresh(st)
        except RuntimeError as e:
            if "jkt" in str(e) or "used" in str(e):
                # 钥匙与 token 失配（桌面端重登换了密钥对）：整批从桌面端重取再试一次
                desk = _bootstrap_from_desktop() or {}
                if desk.get("refresh_token") and desk.get("dpop_priv"):
                    for k in ("refresh_token", "dpop_priv", "dpop_pub", "verifier"):
                        st[k] = desk.get(k)
                    st.pop("last", None)
                    _save_state(st)
                    _log("[ca] 检测到桌面端新会话，已切换")
                    return _do_refresh(st)
            if "refresh_token" not in str(e) and "refresh 失败" not in str(e):
                raise
            if "refresh_token" not in str(e) and "refresh 失败" not in str(e):
                raise
            # 可能被并发进程抢先轮转：重载后再试一次
            st2 = _load_state()
            cred2 = (st2.get("last") or {}).get("credentials") if isinstance(st2.get("last"), dict) else None
            if cred2 and not _need_refresh(cred2):
                return cred2
            if st2.get("refresh_token") and st2.get("refresh_token") != st.get("refresh_token"):
                for k in ("dpop_priv", "dpop_pub", "verifier"):
                    st2.setdefault(k, st.get(k))
                return _do_refresh(st2)
            raise

    return _locked_state_update(_run)


def _sign(ak: str, sk: str, method: str, url: str, headers: dict, body: bytes = b"") -> dict:
    """SDK-HMAC-SHA256（与官方 SDK 逐字节对齐：uri 补斜杠、下划线头不参签）。"""
    from urllib.parse import urlparse
    u = urlparse(url)
    h = {"host": u.netloc, "x-sdk-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}
    h.update({k.lower(): v for k, v in (headers or {}).items()})

    def _qe(s: str) -> str:
        return quote(s, safe="~")

    segs = [_qe(x) for x in u.path.split("/")]
    canon_uri = "/".join(segs)
    if not canon_uri.endswith("/"):
        canon_uri += "/"
    signed = sorted(k for k in h if "_" not in k)
    clines = [f"{k}:{str(h[k]).strip()}" for k in signed]
    canon = "\n".join([method.upper(), canon_uri, "", "\n".join(clines) + "\n",
                       ";".join(signed), hashlib.sha256(body).hexdigest()])
    sts = "SDK-HMAC-SHA256\n" + h["x-sdk-date"] + "\n" + hashlib.sha256(canon.encode()).hexdigest()
    sig = hmac.new(sk.encode(), sts.encode(), hashlib.sha256).hexdigest()
    out = dict(headers or {})
    out["X-Sdk-Date"] = h["x-sdk-date"]
    out["Authorization"] = f"SDK-HMAC-SHA256 Access={ak}, SignedHeaders={';'.join(signed)}, Signature={sig}"
    out["Host"] = u.netloc
    return out


def _ot_headers(model: str) -> dict:
    tid = secrets.token_hex(16)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    h = {
        "x-ot-trace-id": tid, "x-ot-span-id": secrets.token_hex(8),
        "x-snap-traceid": tid + "_" + secrets.token_hex(8),
        "x-ot-session-id": "ses_buddyz", "user-session-id": "ses_buddyz",
        "x-ot-function": "agent-general", "X-Language": "zh-cn",
        "user-msg-id": "msg_" + secrets.token_hex(8), "created-time": now,
        "model-id": model, "model-name": model,
        "x-ot-client-type": "IDE", "x-ot-client-version": APP_VERSION,
    }
    if model in BENEFIT_MODELS:
        h["maas_type"] = "benefit"  # 免费模型走 benefit 路由
    return h


def _chat_prep(model: str, messages: list, max_tokens: int, stream: bool):
    """返回 (url, headers, raw)：调用方自建 httpx.Client（避免 GC 提前关连接）"""
    # 免费模型优先 ticket 独立链（实测有 benefit 路由），glm-5.2 走桌面 DPoP 链
    cred = ensure_creds(prefer="ticket" if model in BENEFIT_MODELS else "dpop")
    url = SNAP + "/api/v2/chat/completions"
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": stream}
    raw = json.dumps(payload).encode()
    base = {"X-Security-Token": cred["security_token"], "Content-Type": "application/json"}
    base.update(_ot_headers(model))
    headers = _sign(cred["access_key_id"], cred["secret_access_key"], "POST", url, base, raw)
    return url, headers, raw


def _chat_upstream(model: str, messages: list, max_tokens: int):
    """非流式对话，返回 (status, data)。必须用 requests（httpx 的 TLS 指纹会被路由层拒）。"""
    import requests as _rq
    url, headers, raw = _chat_prep(model, messages, max_tokens, False)
    r = _rq.post(url, data=raw, headers=headers, timeout=900)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"error": {"message": r.text[:800], "type": "upstream_error"}}


def _exposed() -> list:
    raw = list(CONFIG["exposed_models"]) if CONFIG["exposed_models"] else list(DEFAULT_MODELS)
    return [display_of(m) for m in raw]


def _check_local_auth(req: Request) -> bool:
    key = CONFIG.get("local_api_key") or ""
    if not key:
        return True
    auth = req.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == key


app = FastAPI(title="CodeArts→OpenAI")


@app.get("/oauth/callback")
async def oauth_callback(req: Request):
    """OAuth 回调：?secret= → 307 回 portal 跑完 → ?code=/ticket 换票存盘"""
    from fastapi.responses import HTMLResponse, RedirectResponse
    q = dict(req.query_params)
    if "redirect" in q and "code" not in q and "ticket_id" not in q:
        return RedirectResponse(url=q["redirect"], status_code=307)
    if "secret" in q:
        # ticket 登录流：用回调里的 ticket（取自 pending 或请求参数）
        ticket_id = q.get("ticket_id", "")
        if not ticket_id:
            # 从 pending 里找唯一未过期的
            now = time.time()
            cands = [(t, v) for t, v in _OAUTH_PENDING.items() if now - v.get("at", 0) < 600]
            ticket_id = cands[-1][0] if cands else ""
        try:
            cred = _exchange_ticket(ticket_id, q["secret"])
            st = _load_state()
            st["ticket"] = {"access": cred.get("access"), "secret": cred.get("secret"),
                            "securitytoken": cred.get("securitytoken", ""),
                            "expires_at": cred.get("expires_at")}
            _save_state(st)
            _log("[ca] OAuth 授权成功，ticket 凭证已存")
            return HTMLResponse("<html><body><h2>授权成功，可以关掉此页，回网关点“测试”验证</h2></body></html>")
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(f"<html><body><h2>换票失败：{e}</h2></body></html>", status_code=400)
    if "code" in q:
        # 标准 OAuth code 流：用各 pending verifier 逐个试换
        import httpx as _hx
        port = int(CONFIG.get("port") or 9100)
        now = time.time()
        cands = [(t, v) for t, v in _OAUTH_PENDING.items() if now - v.get("at", 0) < 600]
        errs = []
        for tid, pv in cands:
            try:
                with _hx.Client(timeout=30) as c:
                    r = c.post(STS, data={"client_id": CLIENT_ID,
                                          "code_verifier": pv["verifier"],
                                          "grant_type": "authorization_code",
                                          "code": q["code"],
                                          "redirect_uri": f"http://127.0.0.1:{port}/oauth/callback"},
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
                body = r.json()
                if r.status_code == 200 and "credentials" in body:
                    st = _load_state()
                    st["ticket"] = {"access": body["credentials"].get("access_key_id"),
                                    "secret": body["credentials"].get("secret_access_key"),
                                    "securitytoken": body["credentials"].get("security_token", ""),
                                    "expires_at": body["credentials"].get("expiration")}
                    if body.get("refresh_token"):
                        st["refresh_token"] = body["refresh_token"]
                    _save_state(st)
                    _log("[ca] OAuth code 换票成功")
                    return HTMLResponse("<html><body><h2>授权成功，可以关掉此页，回网关点“测试”验证</h2></body></html>")
                errs.append(str(body)[:120])
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:120])
        return HTMLResponse(f"<html><body><h2>换票失败：{' | '.join(errs)}</h2></body></html>", status_code=400)
    return HTMLResponse("<html><body><h2>缺少参数</h2></body></html>", status_code=400)


@app.get("/v1/auth/url")
async def auth_url():
    info = build_authorize_url(int(CONFIG.get("port") or 9100))
    return {"ok": True, "data": info}


@app.get("/v1/auth/status")
async def auth_status():
    st = _load_state()
    tk = st.get("ticket") or {}
    out = {"ticket": bool(tk.get("access")), "ticket_expires": tk.get("expires_at"),
           "dpop": bool(st.get("refresh_token"))}
    try:
        cred = ensure_creds()
        out["ok"] = True
        out["expires"] = cred.get("expiration")
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:200]
    return out


@app.get("/health")
async def health():
    ok, info = True, {}
    try:
        cred = ensure_creds()
        info = {"expires": cred.get("expiration")}
    except Exception as e:  # noqa: BLE001
        ok, info = False, {"error": str(e)[:200]}
    return {"ok": ok, "service": "codearts2openai", "configured": ok,
            "models": _exposed(), "credential": info}


@app.get("/v1/models")
async def models(req: Request):
    if not _check_local_auth(req):
        return JSONResponse({"error": {"message": "invalid API key", "type": "auth_error"}}, status_code=401)
    return {"object": "list",
            "data": [{"id": m, "object": "model", "created": 0, "owned_by": "codearts"} for m in _exposed()]}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not _check_local_auth(req):
        return JSONResponse({"error": {"message": "invalid API key", "type": "auth_error"}}, status_code=401)
    try:
        body = json.loads(await req.body() or b"{}")
    except Exception:
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)
    msgs = body.get("messages") or []
    if not msgs:
        return JSONResponse({"error": {"message": "messages 为空", "type": "invalid_request"}}, status_code=400)
    model = resolve_model(body.get("model")) or resolve_model(_exposed()[0])
    max_tokens = int(body.get("max_tokens") or 1024)
    stream = bool(body.get("stream", False))
    try:
        if stream:
            from fastapi.responses import StreamingResponse

            async def gen():
                import requests as _rq
                try:
                    url, headers, raw = _chat_prep(model, msgs, max_tokens, True)
                except RuntimeError as e:
                    yield ("data: " + json.dumps(
                        {"error": {"message": str(e), "type": "config_error"}})
                        + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                    return
                try:
                    # requests 流式（TLS 指纹原因，不用 httpx）
                    import asyncio as _aio
                    q: asyncio.Queue = _aio.Queue()
                    err = {}

                    def _run():
                        try:
                            with _rq.post(url, data=raw, headers=headers,
                                          timeout=900, stream=True) as resp:
                                if resp.status_code != 200:
                                    err["body"] = resp.text[:500]
                                    err["status"] = resp.status_code
                                else:
                                    for chunk in resp.iter_content(chunk_size=4096):
                                        if chunk:
                                            q.put_nowait(chunk)
                        except Exception as e:  # noqa: BLE001
                            err["exc"] = str(e)
                        finally:
                            q.put_nowait(None)

                    import threading as _th
                    _th.Thread(target=_run, daemon=True).start()
                    while True:
                        chunk = await q.get()
                        if chunk is None:
                            break
                        yield chunk
                    if "body" in err or "exc" in err:
                        msg = err.get("body") or f"upstream error: {err.get('exc')}"
                        yield ("data: " + json.dumps(
                            {"error": {"message": msg, "type": "upstream_error"}})
                            + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                except Exception as e:  # noqa: BLE001
                    yield ("data: " + json.dumps(
                        {"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
                        + "\n\n").encode()
                    yield b"data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        status, data = _chat_upstream(model, msgs, max_tokens)
        return JSONResponse(content=data, status_code=status)
    except httpx.HTTPError as e:
        return JSONResponse({"error": {"message": f"upstream error: {e}", "type": "upstream_error"}},
                            status_code=502)
    except RuntimeError as e:
        return JSONResponse({"error": {"message": str(e), "type": "config_error"}}, status_code=503)


@app.get("/v1/balance")
async def balance():
    try:
        cred = ensure_creds(prefer="ticket")
        url = OPENGW + "/api/v1/user/tokens/balance"
        raw = b""
        base = {"X-Security-Token": cred["security_token"], "Content-Type": "application/json"}
        headers = _sign(cred["access_key_id"], cred["secret_access_key"], "GET", url, base, raw)
        with httpx.Client(timeout=20) as c:
            r = c.get(url, headers=headers)
        body = r.json()
        if body.get("error_code") != "0000":
            return JSONResponse({"ok": False, "error": body.get("error_msg")}, status_code=502)
        return {"ok": True, "data": body.get("result")}
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"upstream error: {e}"}, status_code=502)


@app.post("/v1/claim")
async def claim():
    try:
        cred = ensure_creds(prefer="ticket")
        url = OPENGW + "/api/v1/benefit/claim"
        raw = b"{}"
        base = {"X-Security-Token": cred["security_token"], "Content-Type": "application/json"}
        headers = _sign(cred["access_key_id"], cred["secret_access_key"], "POST", url, base, raw)
        with httpx.Client(timeout=20) as c:
            r = c.post(url, content=raw, headers=headers)
        body = r.json()
        if body.get("error_code") != "0000":
            return JSONResponse({"ok": False, "error": body.get("error_msg")}, status_code=502)
        return {"ok": True, "data": body.get("result")}
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"upstream error: {e}"}, status_code=502)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9100, log_level="info")
