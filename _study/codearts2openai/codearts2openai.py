# -*- coding: utf-8 -*-
"""codearts2openai — 把华为云 CodeArts Agent（码道）封装成标准 OpenAI 兼容 API。

对外：
  GET  /health                探活（含余额/模型数）
  GET  /v1/models             模型目录（静态 4 个，honor exposed_models）
  POST /v1/chat/completions   对话（非流式；流式转非流式返回；支持 tools/tool_calls）
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
import re
import secrets
import shutil
import sys
import threading as _th
import time
import uuid
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
    "pool_enabled": False,  # 号池轮转（默认关；每个池条目是一套独立 DPoP 会话）
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
# STS 国内外同站（插件 product.json 里 iamStsOpenDomain 两版一致），只有对话域按站点切。
# 余额/福利走 OPENGW，生产环境国内外也一致（插件里按 isGammaVersion 分流，
# 生产分支两站都是 opengw.developer.huaweicloud.com）。
SNAP_CN = "https://snap-access.cn-north-4.myhuaweicloud.com"
SNAP_INTL = "https://snap-access.ap-southeast-1.myhuaweicloud.com"
SNAP = SNAP_CN  # 兼容别名：历史代码直接引用的兜底（实际请走 _snap()）
OPENGW = "https://opengw.developer.huaweicloud.com"
PORTAL_CN = "https://codearts.huaweicloud.com"
PORTAL_INTL = "https://codearts.ap-southeast-1.huaweicloud.com"
APP_VERSION = "26.8.300"

# ---------------------------------------------------------------------------
# 授权身份（client_id / uri_scheme / plugin-name）
#
# ⚠ **这是「浏览器里授权成功了，但网关一直换不到票、必须开着桌面端才行」的根因。**
#   授权与换票必须用**同一套身份**，portal 会把票绑到该身份的会话上：
#     桌面端      client_id=codearts-agent，plugin-name=snap_AIIDE
#     VS Code 插件 client_id=vscode-codebot，  plugin-name=snap_vscode
#   本机有效的凭据库是 VS Code 插件，却拿桌面端身份去授权 → 票绑在桌面端会话上，
#   网关怎么轮询都换不到（表现为"必须打开桌面端"）。
#
# 取证（插件 out/extension.js 的 WebLoginStrategy.openAuthorizeUrl）：
#   `&uri_scheme=${encodeURIComponent(r9())}&client_id=${encodeURIComponent(r9())}
#    &port=${w}&code_challenge=…&ticket_id=${A}&auth_callback_url=${x}
#    &plugin-name=${qf()}_vscode&plugin-version=${nn(!1)}`
#   r9() = i5s() = package.json 的 name（缺省 "vscode-codebot"）
#   qf() = isInnerVersion ? "codemate" : "snap"   → plugin-name = snap_vscode
#   nn(false) = 插件版本号（不带 "Vscode_" 前缀）
# ---------------------------------------------------------------------------
IDENTITIES = {
    "desktop": {"label": "CodeArts 桌面端",
                "client_id": "codearts-agent", "uri_scheme": "codearts-agent",
                "plugin_name": "snap_AIIDE", "plugin_version": "5.3.0"},
    "vscode": {"label": "VS Code 插件",
               "client_id": "vscode-codebot", "uri_scheme": "vscode-codebot",
               "plugin_name": "snap_vscode", "plugin_version": ""},
}
CONFIG_IDENTITY = ""        # 显式指定："" = 自动（跟随凭据来源）| desktop | vscode

# VS Code 插件安装目录（读真实版本号用；授权 URL 要带对版本）
_VSCODE_EXT_GLOB = "huaweicloud.vscode-codebot-*"


def _vscode_ext_dir() -> Path | None:
    """已安装的 vscode-codebot 扩展目录（取版本最高的那个）。"""
    roots = [Path.home() / ".vscode" / "extensions",
             Path.home() / ".vscode-insiders" / "extensions",
             Path.home() / ".vscode-oss" / "extensions"]
    best_dir: Path | None = None
    best_key: tuple = (-1,)
    for r in roots:
        try:
            for d in r.glob(_VSCODE_EXT_GLOB):
                if not d.is_dir():
                    continue
                k = _ver_key(d.name)
                if k > best_key:
                    best_key, best_dir = k, d
        except Exception:  # noqa: BLE001
            continue
    return best_dir


def _ver_key(name: str) -> tuple:
    """从目录名尾部取版本，转成可比较的元组。"""
    try:
        tail = name.rsplit("-", 1)[-1]
        return tuple(int(x) for x in re.findall(r"\d+", tail))
    except Exception:  # noqa: BLE001
        return (0,)


def _vscode_ext_version() -> str:
    """插件版本号（授权 URL / 换票头里要带真实值；拿不到就留空）。"""
    d = _vscode_ext_dir()
    if not d:
        return ""
    try:
        import json as _j
        pkg = _j.loads((d / "package.json").read_text(encoding="utf-8"))
        return str(pkg.get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def current_identity() -> dict:
    """当前该用哪套授权身份。

    优先级：显式配置（CONFIG["identity"] / CONFIG_IDENTITY）> 有 VS Code 凭据库
    就用 vscode > 桌面端。**必须与凭据来源一致**，否则授权换不到票。
    """
    explicit = (CONFIG.get("identity") or CONFIG_IDENTITY or "").strip()
    if explicit in IDENTITIES:
        ident = dict(IDENTITIES[explicit])
    else:
        ident = dict(IDENTITIES["desktop"])
        try:
            for label, _sess in _bootstrap_all_cached():
                if "VS Code" in label or "codebot" in label.lower():
                    ident = dict(IDENTITIES["vscode"])
                    break
        except Exception:  # noqa: BLE001
            pass
    ident = ident or {}
    if ident.get("plugin_name") == "snap_vscode":
        v = _vscode_ext_version()
        if v:
            ident["plugin_version"] = v
    return ident


def _snap(station: str | None = None) -> str:
    """本站点的对话域。station: china | international，缺省读当前判定。"""
    return SNAP_INTL if (station or _current_station()) == "international" else SNAP_CN


def _portal(station: str | None = None) -> str:
    """本站点的授权 portal。"""
    return PORTAL_INTL if (station or _current_station()) == "international" else PORTAL_CN


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
    # 主文件 → 备份：主文件损坏/被截断时用备份兜底（轮转 token 丢了就得重登）
    for cand in (fp, (fp.with_suffix(".bak") if fp else None)):
        if cand and cand.is_file():
            try:
                d = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(d, dict) and d:
                    return d
            except Exception:
                continue
    # 兼容早期 Temp 轮转文件
    tmp = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "ca_state.json"
    try:
        if tmp.is_file():
            return json.loads(tmp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(st: dict) -> bool:
    """原子写 + 备份。

    为什么必须这样做：**refresh_token 是单次有效的**，用掉即轮转，
    新值只存在这一份文件里。原来的 `write_text` 一但写到一半被中断
    （进程被杀、杀软占用、磁盘满），旧 token 已失效、新 token 又没落地
    → 只能重新登录。备份能在主文件损坏时兜回来。

    返回是否落盘成功（失败会打日志，不再静默吞掉）。
    """
    fp = _state_file()
    if not fp:
        _log("[ca] 状态未落盘：BUDDYZ_DATA_DIR 未设置")
        return False
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        # ① 先把当前文件备份一份（只备份能解析的，避免把损坏内容也备份进去）
        if fp.is_file() and fp.stat().st_size > 0:
            try:
                json.loads(fp.read_text(encoding="utf-8"))
                shutil.copy2(fp, fp.with_suffix(".bak"))
            except Exception:  # noqa: BLE001
                pass
        # ② 写临时文件 → 原子替换（Windows 上 os.replace 是原子的）
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, fp)
        return True
    except OSError as e:
        _log(f"[ca] 状态落盘失败（旧值仍保留在文件里）：{e}")
        return False


def _state_patch(**updates) -> bool:
    """只合并指定键（在 state 锁内调用）。

    原来各处都是「整体读 → 改 → 整体写」，主会话与号池共用一份文件时
    容易互相覆盖：A 线程刷新完主会话写回，B 线程手里还是只含 pool 的旧快照，
    再写一次就把主会话**冲掉了**。用 patch 语义 + 原子写规避。
    """
    st = _load_state()
    st.update(updates)
    return _save_state(st)


# ---------------------------------------------------------------------------
# 登录会话来源
# ---------------------------------------------------------------------------
# 两处登录留下的东西是同一套（Chromium v10：Local State 里的 DPAPI 密钥 +
# SQLite ItemTable 里被保护的 secret），解密方式完全一样：
#   · CodeArts Agent 桌面端   %APPDATA%\codearts-agent\
#   · VS Code 插件            %APPDATA%\<Code|...>\User\globalStorage\
# 两者登录的是同一个华为云账号，凭证可互换，所以都要扫。

# VS Code 系 userData 目录名（装的是插件 huaweicloud.vscode-codebot）
_VSCODE_USERDATA_DIRS = ("Code", "Code - Insiders", "VSCodium", "Code - OSS")
_VSCODE_EXT_ID = "huaweicloud.vscode-codebot"
_VSCODE_SECRET_KEY = "SYSTEM_HC_USER_INFO"


def _session_stores() -> list[dict]:
    """候选凭证库 [{label, local_state, vscdb, match}]，按库文件新→旧排序。

    match:
      "any"     桌面端 —— 该库里只有 CodeArts 自己的 secret，取第一条即可
      "codebot" VS Code —— state.vscdb 被**所有扩展共用**，必须按插件 ID 精确
                匹配，否则会拿到别的扩展的 secret
    """
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    cands: list[dict] = []

    d = appdata / "codearts-agent"
    if (d / "User" / "globalStorage" / "state.vscdb").is_file():
        cands.append({"label": "CodeArts 桌面端",
                      "local_state": d / "Local State",
                      "vscdb": d / "User" / "globalStorage" / "state.vscdb",
                      "match": "any"})

    for name in _VSCODE_USERDATA_DIRS:
        u = appdata / name
        db = u / "User" / "globalStorage" / "state.vscdb"
        if db.is_file():
            cands.append({"label": f"VS Code 插件（{name}）",
                          "local_state": u / "Local State",
                          "vscdb": db, "match": "codebot"})

    def _mtime(c: dict) -> float:
        try:
            return c["vscdb"].stat().st_mtime
        except OSError:
            return 0.0

    cands.sort(key=_mtime, reverse=True)
    return cands


def _store_station(store: dict) -> str:
    """该凭证库当前登录的站点：china | international。

    VS Code 插件把站点存在同一库 globalState 的 LOGIN_STATION_KEY 里
    （用户在插件内切换国内外，插件重写这个值）；桌面端是国内客户端，
    直接判 china。读不到一律回退 china。
    """
    if store.get("match") != "codebot":
        return "china"
    import sqlite3
    try:
        con = sqlite3.connect("file:" + str(store["vscdb"]) + "?mode=ro",
                              uri=True, timeout=5)
        try:
            row = con.execute(
                "select value from ItemTable where key='HuaweiCloud.vscode-codebot'"
            ).fetchone()
        finally:
            con.close()
        if row and row[0]:
            j = json.loads(row[0])
            if isinstance(j, dict) and j.get("LOGIN_STATION_KEY") == "international":
                return "international"
    except Exception:
        pass
    return "china"


def _current_station() -> str:
    """当前站点：china | international。

    优先级：
      1. CONFIG["station"] 显式指定（GUI 的「使用版本」选 国内/国际）—— **强制**用它，
         让国内/国际账号各走各的端点（portal / snap / STS 全套跟着切），互不干扰。
      2. 否则自动：取 mtime 最新的那个凭证库的判定。
         用户在插件内切站 → 插件重写 LOGIN_STATION_KEY + vscdb mtime 更新 →
         这里下次调用即跟随，不用重启网关。桌面端是国内客户端，判 china。
      3. 无任何凭证库时回退 china。
    """
    forced = CONFIG.get("station")
    if forced in ("cn", "china"):
        return "china"
    if forced in ("intl", "international"):
        return "international"
    best, best_mtime = "china", -1.0
    for s in _session_stores():
        try:
            mt = Path(s["vscdb"]).stat().st_mtime
        except OSError:
            continue
        if mt >= best_mtime:
            best_mtime = mt
            best = _store_station(s)
    return best


def _secret_value(vscdb: Path, match: str) -> str | None:
    """从 state.vscdb 里取登录 secret 原文。

    VS Code 的 secret key 形如
      secret://{"extensionId":"huaweicloud.vscode-codebot","key":"SYSTEM_HC_USER_INFO"}
    必须精确匹配插件 ID —— 这个库是所有扩展共用的。
    """
    import sqlite3
    exact = "secret://" + json.dumps(
        {"extensionId": _VSCODE_EXT_ID, "key": _VSCODE_SECRET_KEY},
        separators=(",", ":"))
    con = sqlite3.connect("file:" + str(vscdb) + "?mode=ro", uri=True, timeout=5)
    try:
        if match == "codebot":
            row = con.execute("select value from ItemTable where key=?", (exact,)).fetchone()
            if not row or not row[0]:
                # key 名可能随插件版本改动，退化为按插件 ID 模糊匹配
                row = con.execute(
                    "select value from ItemTable where key like 'secret://%' "
                    "and key like ? order by rowid limit 1",
                    (f"%{_VSCODE_EXT_ID}%",)).fetchone()
        else:
            row = con.execute(
                "select value from ItemTable where key like 'secret://%' "
                "order by rowid limit 1").fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def _read_store_session(store: dict) -> dict | None:
    """解出某个凭证库里的登录会话（DPoP 密钥 + refresh_token + 直连临时凭证）。"""
    try:
        import win32crypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    try:
        ls = json.loads(Path(store["local_state"]).read_text(encoding="utf-8"))
        key = win32crypt.CryptUnprotectData(
            base64.b64decode(ls["os_crypt"]["encrypted_key"])[5:], None, None, None, 0)[1]
        raw_s = _secret_value(Path(store["vscdb"]), store["match"])
        if not raw_s:
            _log(f"[ca] {store['label']} 未找到登录会话（secret 条目为空）——需登录一次")
            return None
        raw = bytes(json.loads(raw_s)["data"])
        if raw[:3] != b"v10":
            _log(f"[ca] {store['label']} secret 非 v10 格式，跳过")
            return None
        sess = json.loads(AESGCM(key).decrypt(raw[3:15], raw[15:], None))
        lc = sess.get("loginContext") or {}
        st = {"refresh_token": sess.get("refresh_token", ""),
              "dpop_priv": (lc.get("dpopKeyPair") or {}).get("privateKeyJwk"),
              "dpop_pub": (lc.get("dpopKeyPair") or {}).get("publicKeyJwk"),
              "verifier": (lc.get("pkcePair") or {}).get("codeVerifier", ""),
              # 登录时签发的直连临时凭证：AK/SK + 安全令牌，**无需轮转**即可
              # 签名直调 opengw（桌面端那张表里没有，插件登录才有）
              "access_key_id": sess.get("accessKeyId", ""),
              "secret_access_key": sess.get("secretAccessKey", ""),
              "security_token": sess.get("accessToken", ""),
              "expires_at": sess.get("expiresAt", ""),
              "login_name": sess.get("name", ""),
              "source": store["label"],
              # ⚠ 每条会话**必须记住自己是用哪个 client_id 换来的**：
              #   refresh_token 是绑身份的，拿桌面端 client_id 去续 VS Code 插件
              #   那条 token，STS 会回 `STS5.1806 invalid refresh token:
              #   'invalid client id'`（日志里真实刷过一大片）。
              "identity": "vscode" if store.get("match") == "codebot" else "desktop",
              "client_id": IDENTITIES["vscode" if store.get("match") == "codebot"
                                      else "desktop"]["client_id"],
              "station": _store_station(store)}
        if st["refresh_token"] and st["dpop_priv"]:
            return st
        _log(f"[ca] {store['label']} 会话缺少 refresh_token/DPoP 密钥——需重新登录")
    except Exception as e:  # noqa: BLE001
        _log(f"[ca] {store['label']} 会话读取失败: {e}")
    return None


def _bootstrap_all() -> list:
    """所有可用凭证库解出的会话 [(label, session)]，按库文件新→旧。"""
    out = []
    for store in _session_stores():
        sess = _read_store_session(store)
        if sess:
            out.append((store["label"], sess))
    return out


# 凭证库读取有成本：DPAPI 解密 + 两次 sqlite 打开（secret + station），
# 而免费模型走 ticket 链，**每个请求**都会读一次。加个短 TTL 缓存：
# 突发流量只读一遍，用户刚登录也能在几秒内被发现。
_STORE_CACHE = {"ts": 0.0, "items": []}
_STORE_TTL = float(os.environ.get("BUDDYZ_STORE_TTL") or 5.0)


def _bootstrap_all_cached() -> list:
    now = time.time()
    if _STORE_CACHE["items"] and now - _STORE_CACHE["ts"] < _STORE_TTL:
        return _STORE_CACHE["items"]
    items = _bootstrap_all()
    _STORE_CACHE["ts"] = now
    _STORE_CACHE["items"] = items
    return items


def store_fingerprint() -> float:
    """凭证库「最近被写过」的时间（候选库里 vscdb mtime 的最大值）。

    为什么要它：桌面端/VS Code 插件登录时都会**重写** state.vscdb，mtime 变大。
    而 cred_status() 只看内存凭据与自家的 ca_codearts.json —— 光靠它永远发现不了
    「用户刚刚登录完成」这件事（实测踩过：桌面端登录成功了，网关还在原地等，
    最后超时把窗口关掉，用户以为没生效）。
    """
    best = 0.0
    try:
        for st in _session_stores():
            try:
                best = max(best, Path(st["vscdb"]).stat().st_mtime)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return best


def has_store_session() -> bool:
    """凭证库里现在能不能解出登录态（DPAPI 解密 + sqlite 读，实时不做缓存）。"""
    try:
        return bool(_bootstrap_all())
    except Exception:  # noqa: BLE001
        return False


def _bootstrap_from_desktop() -> dict | None:
    """兼容入口：返回最新那个凭证库的会话。

    历史上只认 CodeArts 桌面端；现在同时认桌面端与 VS Code 插件
    （huaweicloud.vscode-codebot）——同一华为云账号，凭证可互换。
    """
    all_sess = _bootstrap_all_cached()
    return all_sess[0][1] if all_sess else None


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


_LOCK_LOCAL = _th.local()   # 线程本地：标记本线程是否已持 state 锁（可重入用）


def _locked_state_update(fn):
    """独立 lock 文件加锁（Windows 文件锁是强制性的，不能锁 state 文件本身）。

    **必须可重入**：`ensure_creds()` 已经持锁，池路径里的 `_pool_save_back()`
    还会再调一次本函数。msvcrt.LK_LOCK 是阻塞式的，同一进程对同一文件区域
    二次加锁会自己等自己 → 永久挂死（现象：开了号池就不回复）。
    这里用线程本地计数标记「本线程是否已持锁」，已持锁就直接执行。
    """
    try:
        import msvcrt
    except ImportError:
        return fn()
    fp = _state_file()
    if fp is None:
        return fn()
    if getattr(_LOCK_LOCAL, "depth", 0) > 0:
        return fn()               # 同线程重入：直接跑，避免自死锁
    _LOCK_LOCAL.depth = 1
    try:
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
    finally:
        _LOCK_LOCAL.depth = 0


def _do_refresh(st: dict):
    proof = _dpop_jwt(st["dpop_priv"], st["dpop_pub"], "POST", STS)
    _cid = st.get("client_id") or CLIENT_ID      # 必须与签发该 token 的身份一致
    with httpx.Client(timeout=30) as c:
        r = c.post(STS, data={"client_id": _cid, "code_verifier": st["verifier"],
                              "grant_type": "refresh_token", "refresh_token": st["refresh_token"]},
                   headers={"DPoP": proof, "Content-Type": "application/x-www-form-urlencoded"})
    body = r.json()
    if r.status_code != 200 or "credentials" not in body:
        raise RuntimeError(f"refresh 失败({r.status_code}): {str(body)[:200]}")
    st["last"] = body  # 含新 refresh_token（轮转）
    st["client_id"] = _cid          # 记住身份，下次续期仍用它
    _save_state(st)
    _log("[ca] 会话已续期")
    return body["credentials"]


def _dpop_candidates() -> list:
    """DPoP 候选链：data state > Temp 轮转文件+凭证库密钥 > 各凭证库整批。

    「各凭证库」= CodeArts 桌面端 / VS Code 插件，每个库各作一个候选：
    某个库的 refresh_token 被轮转过（单次有效）时自动换下一个试。
    """
    cands = []
    st = _load_state()
    if st.get("refresh_token") and st.get("dpop_priv"):
        cands.append({"refresh_token": st["refresh_token"], "dpop_priv": st["dpop_priv"],
                      "dpop_pub": st["dpop_pub"], "verifier": st.get("verifier", ""),
                      "client_id": st.get("client_id") or CLIENT_ID,
                      "save_to": "data", "label": "data-state"})
    stores = _bootstrap_all_cached()
    # Temp 轮转文件（token） + 最新凭证库 loginContext（密钥）
    try:
        tmp = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "ca_state.json"
        if tmp.is_file():
            tj = json.loads(tmp.read_text(encoding="utf-8"))
            if tj.get("refresh_token") and stores:
                label0, s0 = stores[0]
                cands.append({"refresh_token": tj["refresh_token"],
                              "dpop_priv": s0.get("dpop_priv"), "dpop_pub": s0.get("dpop_pub"),
                              "verifier": s0.get("verifier", ""),
                              "client_id": s0.get("client_id") or CLIENT_ID,
                              "save_to": "data", "label": "temp+" + label0})
    except Exception:
        pass
    for label, sess in stores:
        if sess.get("refresh_token") and sess.get("dpop_priv"):
            cands.append({"refresh_token": sess["refresh_token"],
                          "dpop_priv": sess["dpop_priv"], "dpop_pub": sess["dpop_pub"],
                          "verifier": sess.get("verifier", ""),
                          "client_id": sess.get("client_id") or CLIENT_ID,
                          "save_to": "data", "label": label})
    # 去重（同一 token 只试一次）
    seen, out = set(), []
    for c in cands:
        if c["refresh_token"] not in seen and c.get("dpop_priv"):
            seen.add(c["refresh_token"])
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 号池（多套独立 DPoP 会话调度）
# ---------------------------------------------------------------------------
# 每条 = 一套独立登录会话 {label, refresh_token, dpop_priv, dpop_pub,
# verifier, station, enabled, weight, priority}，存在 state 文件的 "pool" 里，
# 与主会话分离。refresh_token 单次有效：用掉即轮转，新 token 回写到该条目。
# 「抓取本机」从各凭证库（桌面端 / VS Code 插件）各收一条进来。
#
# 调度、熔断、冷却、重试、粘性全部交给共用引擎 buddyzpool（与 mc 通道同源），
# 策略/阈值可在 GUI「号池设置」里改，存在 state 的 "pool_cfg"。
_POOL_CRED: dict = {}          # {label: cred} 凭据缓存（避免每请求都打 STS）
_ENGINE = None
_ENGINE_LOCK = _th.RLock()


def _pool_entries() -> list:
    """池条目（含禁用；是否可用交给引擎判断 enabled）。"""
    st = _load_state()
    out = []
    for e in (st.get("pool") or []):
        if isinstance(e, dict) and e.get("refresh_token") and e.get("dpop_priv"):
            out.append(e)
    return out


def _pool_engine():
    """懒建共用调度引擎；配置从 state 的 pool_cfg 读。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            import importlib
            from pathlib import Path as _P
            root = str(_P(__file__).resolve().parent.parent)   # runtime/ 或 _study/
            if root not in sys.path:
                sys.path.insert(0, root)
            try:
                bzp = importlib.import_module("buddyzpool")
            except Exception as e:  # noqa: BLE001
                _log(f"[ca-pool] 调度引擎不可用，退化为顺序轮转: {e}")
                bzp = None
            if bzp is None:
                _ENGINE = False
            else:
                _ENGINE = bzp.PoolEngine(
                    _pool_entries,
                    (_load_state().get("pool_cfg") or {}),
                    log=lambda s: _log(s.replace("[pool]", "[ca-pool]")))
        return _ENGINE or None


def pool_config() -> dict:
    eng = _pool_engine()
    if eng is None:
        return {}
    cfg = eng.config()
    st = _load_state()
    cfg["pool_enabled"] = bool(CONFIG.get("pool_enabled"))
    cfg["_saved"] = bool(st.get("pool_cfg"))
    return cfg


def pool_set_config(**kw) -> dict:
    """改调度参数并落盘（GUI「号池设置」用）。"""
    eng = _pool_engine()
    clean = {k: v for k, v in kw.items() if v is not None}
    if eng is not None:
        eng.set_config(**clean)
        saved = dict(eng.config())
    else:
        saved = clean

    def _run():
        st = _load_state()
        cur = st.get("pool_cfg") or {}
        cur.update(clean)
        st["pool_cfg"] = cur
        _save_state(st)
    _locked_state_update(_run)
    return saved


def pool_targets() -> list:
    """`pool_probe(idx)` 所用的**同一份**条目列表（索引严格对齐）。

    GUI 遍历批量测试时必须用它，别自己按 pool_list() 另列一份：
    条目顺序/过滤不一致会让下标错位、测到错的号。
    """
    return _pool_entries()


def pool_action_text(idx: int) -> str:
    """探测后把引擎对该条目的处置翻成一句人话（供 GUI 展示）。"""
    eng = _pool_engine()
    if eng is None:
        return ""
    for s in (eng.snapshot() or []):
        if s.get("idx") != idx:
            continue
        if s["state"] == "off":
            return "已停用（按复检间隔自动恢复）"
        if s["state"] == "cooling":
            return f"已冷却 {s['cool_remain']:.0f}s"
        return "保持可用"
    return ""


def pool_probe(idx: int) -> tuple:
    """实测第 idx 条：真实走一次续期，**并把轮转后的新 refresh_token 回写**。

    返回 (是否可用, 说明)。回写这一步是关键——refresh_token 单次有效，
    只测不存会把条目的 token 用掉却不更新，等于把这条弄坏。
    失败时按 manual 走**立即处置**（不等连续失败阈值）。
    """
    ents = _pool_entries()
    if not (0 <= idx < len(ents)):
        return False, "条目不存在"
    e = ents[idx]
    label = e.get("label") or f"#{idx}"
    cand = {"refresh_token": e.get("refresh_token"),
            "dpop_priv": e.get("dpop_priv"), "dpop_pub": e.get("dpop_pub"),
            "verifier": e.get("verifier", ""), "station": e.get("station"),
            "label": label}
    t0 = time.time()
    try:
        cred, new_rt = _refresh_candidate(cand)
    except Exception as ex:  # noqa: BLE001
        status = _refresh_status(ex)
        if status in AUTH_DEAD_CA:
            _POOL_CRED.pop(label, None)
        _pool_fail(idx, status, manual=True,
                   note=f"{type(ex).__name__}: {str(ex)[:60]}")
        return False, f"{type(ex).__name__}: {str(ex)[:100]} → {pool_action_text(idx)}"
    _POOL_CRED[label] = cred
    _pool_save_back(idx, cand, new_rt)
    _pool_ok(idx, latency=time.time() - t0)
    return True, f"续期成功，到期 {cred.get('expiration')}"


def pool_reset_stats():
    eng = _pool_engine()
    if eng is not None:
        eng.reset_stats()


def pool_clear_cooldowns():
    eng = _pool_engine()
    if eng is not None:
        eng.clear_cooldowns()
    _POOL_CRED.clear()


def _pool_cached(label: str):
    """该条目有没有还新鲜的凭据缓存（剩 300s 以上才算新鲜）。"""
    cred = _POOL_CRED.get(label)
    if cred and not _need_refresh(cred):
        return cred
    return None


def _pool_order():
    """兼容包装：交给引擎按策略给候选（[(idx, entry)]）。

    ca 内部走引擎；保留这个同名函数是为了与 mc 通道 API 对称，
    也避免外部/脚本按老名字调用时 AttributeError。
    """
    eng = _pool_engine()
    if eng is None:
        return []
    return eng.order()


def _pool_fail(i, status=None, retry_after=None, manual=False, note=""):
    """失败回馈给引擎（按状态分级冷却 / 失效转 off / 指数退避）。

    manual=True：显式健康探测失败，立即按规则处置（不等连续失败阈值）。
    """
    eng = _pool_engine()
    if eng is not None:
        eng.fail(i, status=status, retry_after=retry_after, manual=manual,
                 note=(note or (f"HTTP {status}" if status else "")))


def _pool_ok(i, latency=None):
    eng = _pool_engine()
    if eng is not None:
        eng.ok(i, latency=latency)


def _pool_state():
    """给 /health 与 GUI 的池状态（含引擎统计）。"""
    eng = _pool_engine()
    keys = []
    summ = {}
    if eng is not None:
        ents = _pool_entries()          # 只读一次状态文件（原来每个条目各读一次）
        for s in eng.snapshot():
            e = ents[s["idx"]] if 0 <= s["idx"] < len(ents) else {}
            keys.append({
                "label": s["label"],
                "station": e.get("station") or "china",
                "enabled": s["enabled"],
                "state": s["state"],
                "cooling": s["state"] == "cooling",
                "off": s["state"] == "off",
                "cool_remain": round(s["cool_remain"], 1),
                "cached": _pool_cached(s["label"]) is not None,
                "inflight": s["inflight"],
                "ok": s["ok"],
                "fail": s["fail"],
                "consec": s["consec"],
                "last_error": s["last_error"],
                "latency_ms": s["latency_ms"],
                "weight": s["weight"],
                "priority": s["priority"],
            })
        summ = eng.summary()
    else:
        summ = {}
    return {"enabled": bool(CONFIG.get("pool_enabled")), "keys": keys, "summary": summ,
            "strategy": (eng.config().get("strategy") if eng is not None else "")}


def _refresh_status(err: Exception) -> int | None:
    """从 refresh 异常里抠 HTTP 状态码（_do_refresh 报 refresh 失败(XXX)）。"""
    m = re.search(r"refresh 失败\((\d{3})\)", str(err))
    return int(m.group(1)) if m else None


def _refresh_candidate(cand: dict) -> tuple:
    """用一条候选（池条目 / 临时组装）走 STS 续期，返回 (credentials, 新refresh_token)。

    只续期不落盘：落盘由调用方决定写哪（主 state 还是池条目），
    避免池条目的新 token 盖掉主会话、或反之。
    """
    proof = _dpop_jwt(cand["dpop_priv"], cand["dpop_pub"], "POST", STS)
    _cid = cand.get("client_id") or CLIENT_ID    # 同上：跟签发身份走
    with httpx.Client(timeout=30) as c:
        r = c.post(STS, data={"client_id": _cid, "code_verifier": cand["verifier"],
                              "grant_type": "refresh_token",
                              "refresh_token": cand["refresh_token"]},
                   headers={"DPoP": proof, "Content-Type": "application/x-www-form-urlencoded"})
    body = r.json()
    if r.status_code != 200 or "credentials" not in body:
        raise RuntimeError(f"refresh 失败({r.status_code}): {str(body)[:200]}")
    cred = body["credentials"]
    cred["station"] = cand.get("station") or _current_station()
    return cred, body.get("refresh_token")


def _pool_save_back(idx: int, cand: dict, body_refresh: str | None):
    """池条目续期成功：新 refresh_token 回写到该条目。"""
    def _run():
        st = _load_state()
        pool = st.get("pool") or []
        # 按 label 定位（轮转中条目顺序可能与 _pool_entries 一致，直接按 idx 也行，
        # 但 label 更稳：_pool_entries 过滤禁用条目后下标会对齐，此处双保险）
        target = None
        if 0 <= idx < len(pool) and isinstance(pool[idx], dict) \
                and pool[idx].get("label") == cand.get("label"):
            target = pool[idx]
        else:
            for e in pool:
                if isinstance(e, dict) and e.get("label") == cand.get("label"):
                    target = e
                    break
        if target is not None and body_refresh:
            target["refresh_token"] = body_refresh
            target["station"] = cand.get("station") or target.get("station") or "china"
            _save_state(st)
    _locked_state_update(_run)


def pool_list() -> list:
    """全部池条目（含禁用），供 GUI 对话框展示。"""
    st = _load_state()
    return [dict(e) for e in (st.get("pool") or []) if isinstance(e, dict)]


def pool_harvest() -> dict:
    """从本机各凭证库各收一条会话进池（按 refresh_token 去重）。

    返回 {"added": [...labels], "skipped": [...labels]}。
    """
    added, skipped = [], []

    def _run():
        st = _load_state()
        pool = st.get("pool") or []
        known = {e.get("refresh_token") for e in pool if isinstance(e, dict)}
        for label, sess in _bootstrap_all():
            if not (sess.get("refresh_token") and sess.get("dpop_priv")):
                continue
            if sess["refresh_token"] in known:
                skipped.append(label)
                continue
            station = sess.get("station") or "china"
            tag = "国际" if station == "international" else "国内"
            pool.append({"label": f"{label}·{tag}",
                         "refresh_token": sess["refresh_token"],
                         "dpop_priv": sess["dpop_priv"], "dpop_pub": sess["dpop_pub"],
                         "verifier": sess.get("verifier", ""),
                         # 身份必须随条目存下来，续期时用它（否则 STS 报
                         # invalid client id）
                         "identity": sess.get("identity", "desktop"),
                         "client_id": sess.get("client_id", CLIENT_ID),
                         "station": station, "enabled": True,
                         "weight": 1, "priority": 0})
            known.add(sess["refresh_token"])
            added.append(label)
        st["pool"] = pool
        _save_state(st)
    _locked_state_update(_run)
    _pool_invalidate()
    return {"added": added, "skipped": skipped}


def _pool_invalidate():
    """池结构变了（增删/启停）→ 让引擎复位结构态、清凭据缓存。

    引擎的运行时状态按 label 索引、统计保留；冷却/粘性清掉，避免下标错位。
    """
    eng = _pool_engine()
    if eng is not None:
        eng.invalidate()
    _POOL_CRED.clear()


def pool_toggle(idx: int) -> bool | None:
    """启用/停用第 idx 条（按 pool_list 顺序），返回新 enabled，无效返回 None。"""
    out = [None]

    def _run():
        st = _load_state()
        pool = [e for e in (st.get("pool") or []) if isinstance(e, dict)]
        if 0 <= idx < len(pool):
            pool[idx]["enabled"] = not pool[idx].get("enabled", True)
            st["pool"] = pool
            _save_state(st)
            out[0] = pool[idx]["enabled"]
    _locked_state_update(_run)
    _pool_invalidate()
    return out[0]


def pool_remove(idx: int) -> bool:
    """删除第 idx 条（按 pool_list 顺序）。"""
    out = [False]

    def _run():
        st = _load_state()
        pool = [e for e in (st.get("pool") or []) if isinstance(e, dict)]
        if 0 <= idx < len(pool):
            pool.pop(idx)
            st["pool"] = pool
            _save_state(st)
            out[0] = True
    _locked_state_update(_run)
    _pool_invalidate()
    return out[0]


def _store_ticket_creds() -> dict | None:
    """凭证库里的**直连临时凭证**（登录时签发的 AK/SK + 安全令牌）。

    这类凭证不需要 refresh_token 轮转，直接就能签名调 opengw —— 对 VS Code
    插件登录尤其合适：不会把插件手里那份 refresh_token 顶掉（轮转是单次的，
    我们一续期，插件那边的副本就失效了，用户得重登）。
    """
    for label, sess in _bootstrap_all_cached():
        if not (sess.get("access_key_id") and sess.get("secret_access_key")):
            continue
        exp = sess.get("expires_at")
        if exp:
            try:
                e = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if (e - datetime.now(timezone.utc)).total_seconds() < 300:
                    continue          # 已过期/将过期：交给 DPoP 链去续
            except Exception:
                pass
        _log(f"[ca] 使用 {label} 的直连临时凭证（无需轮转）")
        return {"access_key_id": sess["access_key_id"],
                "secret_access_key": sess["secret_access_key"],
                "security_token": sess.get("security_token", ""),
                "expiration": exp, "station": sess.get("station") or "china"}
    return None


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
# 视作「会话/凭据失效」的状态码：不是慢，而是这条会话不能用了
AUTH_DEAD_CA = (401, 403)


def _content_text(content) -> str:
    """把 chat 消息的 content（str 或 [{type,text},…]）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"):
                out.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                out.append(p)
        return "".join(out)
    return ""


def conv_key_of(messages: list) -> str | None:
    """会话标识：取第一条 user 消息的哈希。

    会话粘性靠它把同一段对话固定到同一池条目（利于上游 prompt cache），
    只做短哈希，不含原文。
    """
    try:
        for m in (messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                txt = _content_text(m.get("content"))
                if txt:
                    return hashlib.sha1(txt.encode()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_creds(prefer: str = "dpop", conv_key: str | None = None) -> dict:
    """prefer: dpop=桌面链（默认）| ticket=独立 OAuth。任一条走不通自动换另一条试。

    conv_key：会话标识（可选）。开了会话粘性时，同一会话固定用同一池条目
    （利于上游 prompt cache），粘住的条目不可用时回落但保留绑定。
    """
    def _run():
        order = ["ticket", "dpop"] if prefer == "ticket" else ["dpop", "ticket"]

        def _try_dpop():
            # 号池开 → 交给调度引擎按策略给候选，逐条试（含 max_retries 上限）
            eng = _pool_engine() if CONFIG.get("pool_enabled") else None
            if eng is not None:
                cands = eng.order(conv_key)
                if not cands:
                    _log("[ca-pool] 池内无健康条目，退回传统候选链")
                for i, ent in cands:
                    label = ent.get("label") or f"池#{i}"
                    eng.begin(i)
                    try:
                        # 先看缓存：凭据没到期就直接用，别再打一次 STS
                        hit = _pool_cached(label)
                        if hit is not None:
                            eng.ok(i)
                            eng.bind(conv_key, i)
                            _log(f"[ca-pool] 用缓存凭据（{label}）")
                            return hit
                        cand = {"refresh_token": ent.get("refresh_token"),
                                "dpop_priv": ent.get("dpop_priv"),
                                "dpop_pub": ent.get("dpop_pub"),
                                "verifier": ent.get("verifier", ""),
                                "station": ent.get("station"),
                                "label": label}
                        t0 = time.time()
                        try:
                            cred, new_rt = _refresh_candidate(cand)
                        except Exception as e:  # noqa: BLE001
                            status = _refresh_status(e)
                            if status in AUTH_DEAD_CA:
                                _POOL_CRED.pop(label, None)
                            _log(f"[ca-pool] {label} 续期失败: {e}")
                            eng.fail(i, status=status,
                                     note=str(e)[:80])
                            continue
                        _POOL_CRED[label] = cred
                        _pool_save_back(i, cand, new_rt)
                        eng.ok(i, latency=time.time() - t0)
                        eng.bind(conv_key, i)
                        _log(f"[ca-pool] DPoP 续期成功（{label}，"
                             f"到期 {cred.get('expiration')}）")
                        return cred
                    finally:
                        eng.end(i)
                _log("[ca-pool] 池候选试尽，退回传统候选链")
            for cand in _dpop_candidates():
                try:
                    proof = _dpop_jwt(cand["dpop_priv"], cand["dpop_pub"], "POST", STS)
                    _cid = cand.get("client_id") or CLIENT_ID
                    with httpx.Client(timeout=30) as c:
                        r = c.post(STS, data={"client_id": _cid, "code_verifier": cand["verifier"],
                                              "grant_type": "refresh_token",
                                              "refresh_token": cand["refresh_token"]},
                                   headers={"DPoP": proof, "Content-Type": "application/x-www-form-urlencoded"})
                    body = r.json()
                    if r.status_code == 200 and "credentials" in body:
                        st = _load_state()
                        st.update({"refresh_token": body.get("refresh_token", cand["refresh_token"]),
                                   "dpop_priv": cand["dpop_priv"], "dpop_pub": cand["dpop_pub"],
                                   "verifier": cand["verifier"],
                                   "client_id": _cid,
                                   "identity": cand.get("identity")
                                   or ("vscode" if _cid == IDENTITIES["vscode"]["client_id"]
                                       else "desktop"),
                                   "last": body})
                        _save_state(st)
                        _log(f"[ca] DPoP 续期成功（{cand['label']}）")
                        cred = body["credentials"]
                        cred["station"] = _current_station()
                        return cred
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
            # 桌面端 / VS Code 插件登录时签发的直连临时凭证（免轮转）
            return _store_ticket_creds()

        for which in order:
            cred = _try_ticket() if which == "ticket" else _try_dpop()
            if cred:
                return cred
        raise RuntimeError("CodeArts 会话已失效：请在 CodeArts 桌面端或 VS Code 插件"
                           "（huaweicloud.vscode-codebot）登录一次，"
                           "或点面板「授权」重新授权（余额/签到用 ticket 链，对话用 DPoP 链）")
    cred = _locked_state_update(_run)
    # 记住最近一次成功凭据，供 prewarm() 判断何时该提前续期
    try:
        remember_cred(cred)
    except Exception:  # noqa: BLE001
        pass
    return cred


# ---------------------------------------------------------------------------
# 会话保活 & 自动授权（用户反馈：「每次登录都很麻烦，必须点登录 + 开桌面端」）
# ---------------------------------------------------------------------------
# 两个优化，目标是把「反复重登」降到「一次都不用管」：
#   ① prewarm()   主动续期 —— 会话在网关运行期间一直保活，不会闲置到过期
#   ② auto_login() 凭据失效时**自动**拉起授权页（免点「授权」按钮）
# refresh_token 单次有效：只要没人用它就一直是有效的；一旦闲置过久/被别处用掉
# 就失效了。所以「定期主动续」比「等用到再续」稳得多。

_LAST_CRED: dict = {"cred": None, "at": 0.0}
_CRED_LOCK = _th.Lock()
_PREWARM_AT = [0.0]           # 上次尝试时间（节流，避免频繁打 STS）
_PREWARM_MIN_INTERVAL = 120.0  # 两次续期尝试的最小间隔（秒）
# 最近一次续期结果（供 GUI 显示"到底还能不能用"）
_PREWARM_LAST: dict = {"ok": None, "action": "", "at": 0.0, "error": ""}


def _expires_in(cred: dict | None) -> int:
    """剩余有效秒数；解析不了返回 -1。"""
    if not cred:
        return -1
    try:
        exp = datetime.fromisoformat((cred.get("expiration") or "").replace("Z", "+00:00"))
        return int((exp - datetime.now(timezone.utc)).total_seconds())
    except Exception:  # noqa: BLE001
        return -1


def remember_cred(cred: dict | None) -> None:
    """记住最近一次成功取得的凭据（内存，供保活判断是否该续期）。"""
    if cred:
        with _CRED_LOCK:
            _LAST_CRED["cred"] = cred
            _LAST_CRED["at"] = time.time()


def prewarm(lead_seconds: int = 1200, force: bool = False) -> dict:
    """主动续期：凭据剩余寿命不足 lead_seconds 时提前刷新一次。

    返回 {ok, action: skip|refreshed|failed, expires_in, error}
    """
    now = time.time()
    if not force and now - _PREWARM_AT[0] < _PREWARM_MIN_INTERVAL:
        return {"ok": True, "action": "skip", "expires_in": _expires_in(_LAST_CRED.get("cred")), "error": ""}
    with _CRED_LOCK:
        cred = _LAST_CRED["cred"]
    left = _expires_in(cred)
    if cred and not force and left > lead_seconds:
        _PREWARM_AT[0] = now
        _PREWARM_LAST.update({"ok": True, "action": "skip",
                              "at": now, "error": ""})
        return {"ok": True, "action": "skip", "expires_in": left, "error": ""}
    _PREWARM_AT[0] = now
    try:
        fresh = ensure_creds(prefer="ticket")
        remember_cred(fresh)
        _PREWARM_LAST.update({"ok": True, "action": "refreshed",
                              "at": now, "error": ""})
        return {"ok": True, "action": "refreshed",
                "expires_in": _expires_in(fresh), "error": ""}
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        _PREWARM_LAST.update({"ok": False, "action": "failed",
                              "at": now, "error": err})
        return {"ok": False, "action": "failed", "expires_in": -1,
                "error": err}


def cred_status() -> dict:
    """**只读**凭据状态（不发任何网络请求），供 GUI 展示真实可用性。

    与「文件里有没有 refresh_token」不同：这里反映的是**能不能真的用**
    （最近一次续期成功没有 / 内存里的凭据还剩多久）。
    """
    with _CRED_LOCK:
        cred = _LAST_CRED["cred"]
        at = _LAST_CRED["at"]
    left = _expires_in(cred)
    st = _load_state()
    fp = _state_file()
    return {
        "live": bool(cred) and left > 0,          # 手上真有可用凭据
        "expires_in": left,                        # 剩余秒数（-1 = 未知）
        "checked_at": at,
        "has_session": bool(st.get("refresh_token")),
        "has_ticket": bool((st.get("ticket") or {}).get("access")),
        "last_ok": _PREWARM_LAST.get("ok"),        # None=还没试过
        "last_action": _PREWARM_LAST.get("action", ""),
        "last_at": _PREWARM_LAST.get("at", 0.0),
        "last_error": _PREWARM_LAST.get("error", ""),
        "writable": fp is not None,
        "has_backup": bool(fp and fp.with_suffix(".bak").is_file()),
    }


def auto_login(port: int, station: str | None = None, open_browser: bool = True,
               log=None) -> dict:
    """凭据不可用时**自动**发起授权：生成 URL + 拉起浏览器，免点按钮。

    返回 {ok, url, ticket_id, station, opened, error}
    """
    def _log(m):
        if log:
            log(m)
    try:
        info = build_authorize_url(port, station)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": "", "error": f"{type(e).__name__}: {e}"}
    opened = False
    if open_browser:
        try:
            import webbrowser
            opened = bool(webbrowser.open(info["url"]))
        except Exception as e:  # noqa: BLE001
            _log(f"[ca] 拉起浏览器失败（请手动打开链接）：{e}")
    _log(f"[ca] 已自动发起授权（{info['station']} · 身份 {info.get('identity', '?')}）："
         f"{info['url']}")
    return {"ok": True, "url": info["url"], "ticket_id": info["ticket_id"],
            "station": info["station"], "opened": opened,
            "identity": info.get("identity", ""),
            "identity_key": info.get("identity_key", ""), "error": ""}


def needs_login() -> bool:
    """当前是否处于「必须重新登录」状态（所有凭据来源都不可用）。"""
    try:
        ensure_creds(prefer="ticket")
        return False
    except Exception:  # noqa: BLE001
        return True


def build_authorize_url(port: int, station: str | None = None) -> dict:
    """生成授权 URL（PKCE 存内存待回调配对）。station 缺省跟随当前判定。

    ticket_id 必须是 **UUID v4（含连字符）**，对齐官方插件 `YMs`（uuid v4 生成器）。
    我们原先用 `secrets.token_hex(32)` 产 64 位无连字符 hex，服务端不认，
    导致 `TM.00001001 无效ticketId` —— 无论轮询多久都救不了。
    """
    station = station or _current_station()
    ident = current_identity()               # 身份必须与凭据来源一致
    verifier = _b64u(secrets.token_bytes(64))
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    ticket = str(uuid.uuid4())               # UUID v4，含连字符
    _OAUTH_PENDING[ticket] = {"verifier": verifier, "at": time.time(),
                              "station": station, "identity": ident}
    params = {"theme": "dark", "locale": "zh-cn",
              "uri_scheme": ident["uri_scheme"],
              "client_id": ident["client_id"], "port": str(port),
              "code_challenge": challenge, "code_challenge_method": "S256",
              "ticket_id": ticket,
              # 官方插件会把本地回调地址一并带上（否则 portal 只能靠 port 猜），
              # 参数顺序也照抄插件，避免某些网关按序解析。
              "auth_callback_url": f"http://127.0.0.1:{port}/oauth/callback",
              "plugin-name": ident["plugin_name"],
              "plugin-version": ident["plugin_version"] or PLUGIN_VERSION}
    url = (_portal(station) + "/portal/authorize?"
           + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items()))
    if station == "international":
        # 插件：`t.globalState.get(Ch)==="international" && (I=`${I}&quickcompfalg=1`)`
        url += "&quickcompfalg=1"
    return {"url": url, "ticket_id": ticket, "station": station,
            "identity": ident["label"], "identity_key": ident.get("plugin_name")}


# --- 换票是**轮询**语义（关键！） -------------------------------------------
# portal 重定向回本地回调那一刻，ticket 常常**尚未就绪**。华为云官方 VS Code 插件
# （WebLoginStrategy.queryAuth）就是 setInterval 轮询：
#     TICKET_RETRY_TIME_INTERVAL = 2e3     每 2 秒一次
#     MAX_TOKEN_QUERY_TIME       = 3*60*1e3 最长 3 分钟
# 期间 400 `无效ticketId`(TM.00001001) 属于"还没就绪"，必须继续重试而不是报错；
# 只有 AUTH.9022（浏览器登录的账号与 IDE 当前账号不一致）才立即终止。
# 只换一次就判失败 = 必然误报（本模块曾如此）。
TICKET_POLL_INTERVAL = 2.0
TICKET_POLL_TIMEOUT = 180.0
TICKET_FATAL_CODES = {"AUTH.9022"}
PLUGIN_NAME = "snap_AIIDE"      # 桌面端；VS Code 插件为 snap_vscode
PLUGIN_VERSION = "5.3.0"

# OAuth 换票进度（供 /v1/auth/status 与 GUI 展示）
_AUTH_EXCHANGE: dict = {}


def _ticket_get(ticket_id: str, secret: str, station: str | None = None,
                identity: dict | None = None) -> tuple:
    """单次换票 → (credential|None, error_code, message)。网络异常不抛，转成 message。

    identity：授权时用的那套身份。**必须与 build_authorize_url 用的一致** ——
    portal 把票绑在发起授权的那个身份上，换票头里的 plugin-name 对不上就换不到
    （历史现象：浏览器显示授权成功，网关一直换不到，必须开着桌面端）。
    """
    ident = identity or current_identity()
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(_snap(station) + "/snap-manager/v1/login/ticket",
                      params={"ticket_id": ticket_id, "secret": secret},
                      headers={"Content-Type": "application/json;charset=UTF-8",
                               "plugin-name": ident.get("plugin_name") or PLUGIN_NAME,
                               "plugin-version": ident.get("plugin_version") or PLUGIN_VERSION})
    except Exception as e:  # noqa: BLE001
        return None, "", f"{type(e).__name__}: {str(e)[:120]}"
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"error_msg": (r.text or "")[:200]}
    if not isinstance(body, dict):
        body = {"error_msg": str(body)[:200]}
    if r.status_code == 200 and isinstance(body.get("credential"), dict):
        return body["credential"], "", ""
    code = str(body.get("error_code") or f"HTTP {r.status_code}")
    return None, code, str(body.get("error_msg") or "")[:200]


def _exchange_ticket(ticket_id: str, secret: str, station: str | None = None,
                     log=None, identity: dict | None = None) -> dict:
    """换票（**轮询**，见文件上方常量注释）。成功返回 credential，超时/致命错抛异常。

    注意：这会阻塞最长 TICKET_POLL_TIMEOUT 秒，**只能在线程里调**，
    别直接放在 async 路由里（会把整个服务卡死）。
    """
    t0, attempt, last = time.time(), 0, "未尝试"
    while True:
        attempt += 1
        cred, code, msg = _ticket_get(ticket_id, secret, station, identity)
        if cred is not None:
            return cred
        last = f"{code}: {msg}" if code else msg
        if code in TICKET_FATAL_CODES:
            raise RuntimeError(f"ticket 换票失败({code}): {msg}")
        if time.time() - t0 >= TICKET_POLL_TIMEOUT:
            raise RuntimeError(f"ticket 换票超时（轮询 {attempt} 次 / "
                               f"{TICKET_POLL_TIMEOUT:.0f}s 仍未就绪）：{last}")
        if log and attempt == 1:
            log(f"[ca] 换票暂未就绪（{last}）—— 按官方插件语义每 "
                f"{TICKET_POLL_INTERVAL:.0f}s 重试，最长 {TICKET_POLL_TIMEOUT / 60:.0f} 分钟")
        time.sleep(TICKET_POLL_INTERVAL)


def _exchange_bg(ticket_ids: list, secret: str, station: str | None,
                 identity: dict | None = None) -> None:
    """后台轮询换票（语义见 TICKET_POLL_* 注释）。

    逐个候选 ticket_id 试（最新优先），任一轮成功即落盘并结束。
    只在**后台线程**里跑：最长 3 分钟，绝不能占住事件循环或浏览器。
    """
    t0, attempt, last = time.time(), 0, "未尝试"
    while True:
        attempt += 1
        for tid in ticket_ids:
            cred, code, msg = _ticket_get(tid, secret, station, identity)
            if cred is not None:
                def _run(_c=cred):
                    # patch 语义：只动 ticket 键，避免与其它写点互相覆盖
                    _state_patch(ticket={"access": _c.get("access"),
                                         "secret": _c.get("secret"),
                                         "securitytoken": _c.get("securitytoken", ""),
                                         "expires_at": _c.get("expires_at")})

                try:
                    _locked_state_update(_run)
                except Exception as e:  # noqa: BLE001
                    last = f"存盘失败: {e}"
                    _log(f"[ca] 换票已成功但凭证存盘失败：{e}")
                    _AUTH_EXCHANGE.update({"state": "error", "attempts": attempt,
                                           "error": last, "at": time.time()})
                    return
                _log(f"[ca] OAuth 授权成功（第 {attempt} 轮换票拿到凭证，已存盘）")
                _AUTH_EXCHANGE.update({"state": "ok", "attempts": attempt,
                                       "error": "", "at": time.time()})
                return
            last = f"{code}: {msg}" if code else msg
            if code in TICKET_FATAL_CODES:
                # 例如 AUTH.9022：浏览器里登录的账号 ≠ IDE 当前账号，重试无意义
                _log(f"[ca] 换票终止（{code}）：{msg}")
                _AUTH_EXCHANGE.update({"state": "error", "attempts": attempt,
                                       "error": last, "at": time.time()})
                return
        _AUTH_EXCHANGE.update({"state": "pending", "attempts": attempt,
                               "error": last, "at": time.time()})
        if time.time() - t0 >= TICKET_POLL_TIMEOUT:
            _log(f"[ca] 换票超时（轮询 {attempt} 轮 / {TICKET_POLL_TIMEOUT:.0f}s "
                 f"仍未就绪）：{last}")
            _AUTH_EXCHANGE.update({"state": "timeout", "attempts": attempt,
                                   "error": last, "at": time.time()})
            return
        if attempt == 1:
            _log(f"[ca] 换票暂未就绪（{last}）—— 按官方插件语义每 "
                 f"{TICKET_POLL_INTERVAL:.0f}s 重试，最长 {TICKET_POLL_TIMEOUT / 60:.0f} 分钟")
        time.sleep(TICKET_POLL_INTERVAL)


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
            raise RuntimeError("无 CodeArts 会话：请在 CodeArts 桌面端或 VS Code 插件"
                               "（huaweicloud.vscode-codebot）登录一次（或走 OAuth 授权）")
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
                    _log("[ca] 检测到新的登录会话（桌面端 / VS Code 插件），已切换")
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


def _tool_payload(tools, tool_choice) -> dict:
    """把 OpenAI 的 tools/tool_choice 归一成上游能收的形态。

    实测 snap-access：`tool_choice` 只接受**字符串** auto/required，OpenAI 的
    对象形式（{"type":"function",...}）会 400 InferHub.001001005；不带则默认 auto。
    `tool_choice="none"` 表示禁用工具，直接整块不带。
    """
    if not tools:
        return {}
    tc = tool_choice
    if isinstance(tc, str) and tc.lower() == "none":
        return {}
    out = {"tools": tools}
    if isinstance(tc, str) and tc.lower() in ("auto", "required"):
        out["tool_choice"] = tc.lower()
    elif isinstance(tc, dict):
        out["tool_choice"] = "required"   # 对象形式上游不收，降级为强制
    else:
        out["tool_choice"] = "auto"
    return out


def _chat_prep(model: str, messages: list, max_tokens: int, stream: bool,
               tools: list | None = None, tool_choice=None):
    """返回 (url, headers, raw)：调用方自建 httpx.Client（避免 GC 提前关连接）"""
    # 免费模型优先 ticket 独立链（实测有 benefit 路由），glm-5.2 走桌面 DPoP 链
    cred = ensure_creds(prefer="ticket" if model in BENEFIT_MODELS else "dpop",
                        conv_key=conv_key_of(messages))
    # 对话域跟随出凭证的那条会话的站点（号池里可能国内外混用）
    url = (_snap(cred.get("station")) + "/api/v2/chat/completions")
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": stream}
    payload.update(_tool_payload(tools, tool_choice))
    raw = json.dumps(payload).encode()
    base = {"X-Security-Token": cred["security_token"], "Content-Type": "application/json"}
    base.update(_ot_headers(model))
    headers = _sign(cred["access_key_id"], cred["secret_access_key"], "POST", url, base, raw)
    return url, headers, raw


def _chat_upstream(model: str, messages: list, max_tokens: int,
                   tools: list | None = None, tool_choice=None):
    """非流式对话，返回 (status, data)。必须用 requests（httpx 的 TLS 指纹会被路由层拒）。"""
    import requests as _rq
    url, headers, raw = _chat_prep(model, messages, max_tokens, False, tools, tool_choice)
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
    """OAuth 回调：?secret=（+可选 ticket_id）换票存盘 → 否则 307 回 portal → ?code= 换票。"""
    from fastapi.responses import HTMLResponse, RedirectResponse
    from urllib.parse import urlparse, parse_qs
    q = dict(req.query_params)

    def _station_from_redirect(redirect_url: str) -> str:
            """从 redirect URL 域名识别站点：国内 codearts.huaweicloud.com / 国际 codearts.ap-southeast-1.huaweicloud.com。"""
            try:
                host = urlparse(redirect_url).netloc.lower()
            except Exception:
                return "china"
            if "ap-southeast-1" in host or "intl" in host:
                return "international"
            return "china"

    # 必须先判 secret：否则带 redirect 的回调会被下面的 307 短路，换票代码永远走不到。
    if "secret" in q:
                # ticket 登录流。**回调里没有服务端签发的 ticket_id**（官方插件的回调
                # 也只读 secret/code/redirect），ticket_id 就是他自己发起时生成、
                # 通过 authorize URL 带过去的那一个。多次点「授权」会留下多个 pending，
                # 无法从 secret 反推是哪一个 → 全部收进来逐个试（最新优先）。
                cands_ids: list = []
                for cand in (q.get("ticket_id", ""),
                             (parse_qs(urlparse(q.get("redirect", "")).query)
                              .get("ticket_id") or [""])[0]):
                    if cand and cand not in cands_ids:
                        cands_ids.append(cand)
                now = time.time()
                pend = [(t, v) for t, v in _OAUTH_PENDING.items()
                        if now - v.get("at", 0) < 600]
                for _t, _v in reversed(pend):        # 最新优先
                    if _t not in cands_ids:
                        cands_ids.append(_t)
                if not cands_ids:
                    return HTMLResponse(
                        "<html><body><h2>换票失败：没有可用的 ticket_id</h2>"
                        "<p>请回网关重新点「授权」（不要用旧标签页）。</p></body></html>",
                        status_code=400)
                # 站点：优先 redirect URL 域名判定，兜底该 ticket 的 pending 站点
                redirect = q.get("redirect", "")
                station = _station_from_redirect(redirect) if redirect else (
                    (_OAUTH_PENDING.get(cands_ids[0]) or {}).get("station")
                )
                # ⚠ 换票是轮询语义（最长 3 分钟，见 TICKET_POLL_* 注释）。
                # 绝不能在这里同步等待 —— 会卡住浏览器、也会卡死整个事件循环。
                # 起后台线程去轮询，页面立即返回（官方插件也是让浏览器先走）。
                # 身份必须跟着 ticket 走：发起授权时用的哪套身份，换票就用哪套。
                _ident = (_OAUTH_PENDING.get(cands_ids[0]) or {}).get("identity")
                _AUTH_EXCHANGE.clear()
                _AUTH_EXCHANGE.update({"state": "pending", "attempts": 0,
                                       "at": now, "error": "", "station": station,
                                       "identity": (_ident or {}).get("plugin_name", ""),
                                       "candidates": len(cands_ids)})
                _th.Thread(target=_exchange_bg,
                           args=(list(cands_ids), q["secret"], station, _ident),
                           daemon=True).start()
                return HTMLResponse(
                    "<html><body style='font-family:sans-serif;padding:24px'>"
                    "<h2>授权已收到，正在换取凭证…</h2>"
                    f"<p>ticket 通常需要几秒到几分钟才就绪，网关会按官方插件的语义"
                    f"（每 {TICKET_POLL_INTERVAL:.0f}s 重试，最长 "
                    f"{TICKET_POLL_TIMEOUT / 60:.0f} 分钟）自动重试。</p>"
                    "<p><b>可以直接关掉此页</b>，回网关看日志或点「测试」。</p>"
                    "</body></html>")
    if "redirect" in q and "code" not in q:
        return RedirectResponse(url=q["redirect"], status_code=307)
    if "code" in q:
        # 标准 OAuth code 流：用各 pending verifier 逐个试换
        import httpx as _hx
        import asyncio as _aio
        port = int(CONFIG.get("port") or 9100)
        now = time.time()
        cands = [(t, v) for t, v in _OAUTH_PENDING.items() if now - v.get("at", 0) < 600]
        errs = []
        for _tid, pv in cands:
            try:
                # 本次授权是用哪个身份发起的，就用哪个 client_id 换票
                # （portal 把 code 绑在发起身份上；用错身份必然换不到）
                _ident = pv.get("identity")
                _cid = (_ident.get("client_id") if isinstance(_ident, dict) else "") \
                    or (IDENTITIES.get(str(pv.get("identity_key") or "")) or {}).get("client_id") \
                    or CLIENT_ID

                def _sync(_pv=pv, _code=q["code"], _port=port, _cid=_cid):
                    # 同步 httpx 放线程里，别阻塞事件循环。
                    # 注意 _pv/_code/_port 用默认参数**绑定当前值**：直接闭包引用
                    # 循环变量会在下一轮被改写（晚绑定），拿到错的那个候选。
                    with _hx.Client(timeout=30) as c:
                        return c.post(STS, data={
                            "client_id": _cid,
                            "code_verifier": _pv["verifier"],
                            "grant_type": "authorization_code",
                            "code": _code,
                            "redirect_uri": f"http://127.0.0.1:{_port}/oauth/callback"},
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

                r = await _aio.to_thread(_sync)
                body = r.json()
                if r.status_code == 200 and "credentials" in body:
                    _patch = {"ticket": {
                        "access": body["credentials"].get("access_key_id"),
                        "secret": body["credentials"].get("secret_access_key"),
                        "securitytoken": body["credentials"].get("security_token", ""),
                        "expires_at": body["credentials"].get("expiration")}}
                    if body.get("refresh_token"):
                        _patch["refresh_token"] = body["refresh_token"]
                    _state_patch(**_patch)
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
    import asyncio as _aio
    st = _load_state()
    tk = st.get("ticket") or {}
    out = {"ticket": bool(tk.get("access")), "ticket_expires": tk.get("expires_at"),
           "dpop": bool(st.get("refresh_token")),
           # 当前授权身份（决定 client_id / plugin-name）—— 必须与凭据来源一致
           "identity": {k: v for k, v in current_identity().items()},
           # 会话保活信息（GUI 用来显示"还剩多久 / 何时自动续"）
           "keepalive": {
               "expires_in": _expires_in((_LAST_CRED.get("cred") or None)),
               "last_at": _LAST_CRED.get("at") or 0,
               "writable": _state_file() is not None,
               "has_backup": bool(_state_file() and _state_file().with_suffix(".bak").is_file()),
           },
           # OAuth 换票进度（前端授权后可能还在后台轮询，见 _exchange_bg）
           "exchange": dict(_AUTH_EXCHANGE)}
    # ensure_creds 内部可能走 STS 续期（同步网络），必须放线程，别卡事件循环
    try:
        cred = await _aio.to_thread(ensure_creds)
        out["ok"] = True
        out["expires"] = cred.get("expiration")
        out["expires_in"] = _expires_in(cred)
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:200]
    return out


@app.post("/v1/keepalive")
async def keepalive(force: bool = False):
    """主动续期一次（保活）。定时调它 = 会话永不闲置到过期，用户不必反复重登。

    返回 {ok, action: skip|refreshed|failed, expires_in, error}
    """
    import asyncio as _aio
    return await _aio.to_thread(lambda: prewarm(force=force))


@app.get("/health")
async def health():
    import asyncio as _aio
    ok, info = True, {}
    try:
        cred = await _aio.to_thread(ensure_creds)   # 同上：可能含同步续期
        info = {"expires": cred.get("expiration")}
    except Exception as e:  # noqa: BLE001
        ok, info = False, {"error": str(e)[:200]}
    return {"ok": ok, "service": "codearts2openai", "configured": ok,
            "models": _exposed(), "credential": info,
            "station": await _aio.to_thread(_current_station),
            "pool": await _aio.to_thread(_pool_state)}


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
    tools = body.get("tools") or None
    tool_choice = body.get("tool_choice")
    try:
        if stream:
            from fastapi.responses import StreamingResponse

            async def gen():
                import requests as _rq
                try:
                    url, headers, raw = _chat_prep(model, msgs, max_tokens, True, tools, tool_choice)
                except RuntimeError as e:
                    yield ("data: " + json.dumps(
                        {"error": {"message": str(e), "type": "config_error"}})
                        + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                    return
                try:
                    # requests 流式（TLS 指纹原因，不用 httpx）
                    import asyncio as _aio
                    loop = _aio.get_running_loop()
                    q: _aio.Queue = _aio.Queue()
                    err = {}

                    def _push(item):
                        """把 chunk 从 worker 线程安全投递给事件循环。

                        `asyncio.Queue` **不是线程安全的**：在别的线程直接
                        `q.put_nowait()`，唤不醒阻塞在 `await q.get()` 的事件
                        循环 → 请求永久挂住（现象：流式对话一直不返回）。
                        必须经 `loop.call_soon_threadsafe` 投递。
                        """
                        loop.call_soon_threadsafe(q.put_nowait, item)

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
                                            _push(chunk)
                        except Exception as e:  # noqa: BLE001
                            err["exc"] = str(e)
                        finally:
                            _push(None)

                    import threading as _th
                    _th.Thread(target=_run, daemon=True).start()
                    while True:
                        try:
                            # 看门狗：上游卡住不吐字节时别让客户端无限等
                            chunk = await _aio.wait_for(q.get(), timeout=900)
                        except _aio.TimeoutError:
                            err["exc"] = "upstream idle timeout (900s)"
                            break
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
        # 非流式：`_chat_upstream` 内部是同步 requests（TLS 指纹要求），
        # 必须丢到线程，否则整个事件循环被上游耗时卡住（timeout=900）。
        import asyncio as _aio
        status, data = await _aio.to_thread(
            _chat_upstream, model, msgs, max_tokens, tools, tool_choice)
        return JSONResponse(content=data, status_code=status)
    except httpx.HTTPError as e:
        return JSONResponse({"error": {"message": f"upstream error: {e}", "type": "upstream_error"}},
                            status_code=502)
    except RuntimeError as e:
        return JSONResponse({"error": {"message": str(e), "type": "config_error"}}, status_code=503)


@app.get("/v1/balance")
async def balance():
    import asyncio as _aio

    def _sync():
        # 取凭证可能含 STS 续期 + 同步 httpx，整块放线程，别卡事件循环
        cred = ensure_creds(prefer="ticket")
        url = OPENGW + "/api/v1/user/tokens/balance"
        raw = b""
        base = {"X-Security-Token": cred["security_token"], "Content-Type": "application/json"}
        headers = _sign(cred["access_key_id"], cred["secret_access_key"], "GET", url, base, raw)
        with httpx.Client(timeout=20) as c:
            return c.get(url, headers=headers)

    try:
        r = await _aio.to_thread(_sync)
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
    import asyncio as _aio

    def _sync():
        cred = ensure_creds(prefer="ticket")
        url = OPENGW + "/api/v1/benefit/claim"
        raw = b"{}"
        base = {"X-Security-Token": cred["security_token"], "Content-Type": "application/json"}
        headers = _sign(cred["access_key_id"], cred["secret_access_key"], "POST", url, base, raw)
        with httpx.Client(timeout=20) as c:
            return c.post(url, content=raw, headers=headers)

    try:
        r = await _aio.to_thread(_sync)
        body = r.json()
        if body.get("error_code") != "0000":
            return JSONResponse({"ok": False, "error": body.get("error_msg")}, status_code=502)
        return {"ok": True, "data": body.get("result")}
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"upstream error: {e}"}, status_code=502)


def claim_daily() -> dict:
    """领取华为云每日福利（**同步**，供 GUI 直接调用）。

    CodeArts 只有一个账号，福利走 OPENGW，国内外一致 → 不区分站点。
    返回 {ok, data?/error?}。上游幂等：重复调用仍返回 error_code=0000。
    """
    try:
        cred = ensure_creds(prefer="ticket")
        url = OPENGW + "/api/v1/benefit/claim"
        raw = b"{}"
        base = {"X-Security-Token": cred["security_token"],
                "Content-Type": "application/json"}
        headers = _sign(cred["access_key_id"], cred["secret_access_key"],
                        "POST", url, base, raw)
        with httpx.Client(timeout=20) as c:
            r = c.post(url, content=raw, headers=headers)
        body = r.json()
        if body.get("error_code") != "0000":
            return {"ok": False, "error": body.get("error_msg") or str(body)[:200]}
        return {"ok": True, "data": body.get("result")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9100, log_level="info")
