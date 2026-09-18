#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",)):
        return body

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND_CN = "https://copilot.tencent.com"
BACKEND_INTL = "https://www.codebuddy.ai"
DEFAULT_DOMAIN = "www.codebuddy.cn"
# 兼容旧引用；实际端点按登录账号的 domain 自动解析（见 backend_host()）
BACKEND = BACKEND_CN


def backend_host(domain: str | None) -> str:
    """按登录账号的 domain 选择后端网关：
    国际版（workbuddy.ai / codebuddy.ai）→ codebuddy.ai，其余（.cn/tencent.com）→ copilot.tencent.com

    注意：**不要**用站点选择去覆盖这里。站点只决定读哪个 auth 文件；后端必须跟随
    账号自身的 domain，否则会把国际凭据发到国内后端（反之亦然）→ 认证失败。
    """
    d = domain or ""
    if any(k in d for k in ("workbuddy.ai", "codebuddy.ai")):
        return BACKEND_INTL
    return BACKEND_CN
USER_AGENT = "codebuddy2openai/2.0"

# 站点 → auth 文件名。国内与国际**同时登录**时各自独立，互不覆盖。
STATION_AUTH_FILE = {
    "cn": "workbuddy-desktop.info",          # 国内 www.workbuddy.cn
    "intl": "workbuddy-desktop-ai.info",     # 国际 www.workbuddy.ai
}
STATION_LABEL = {"cn": "国内", "intl": "国际"}

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_file(station: str | None = None) -> Path | None:
    """定位 auth 文件。

    station: None(自动) | "cn" | "intl"
      · "cn" / "intl" → 精确取该站点的登录文件（两版可同时登录、互不覆盖）
      · None          → 在两个 canonical 文件里取 mtime 最新的（最近活跃的那个）

    ⚠ 历史坑：原实现是 `sorted(d.glob("*.info"))[0]`，而备份文件
    `workbuddy-desktop-ai.2026-…Z.9160.<uuid>.info` 因为 `-`(0x2D) < `.`(0x2E)
    会排在 `workbuddy-desktop.info` **前面** → 选中"已登出账号的旧备份"。
    所以这里只认两个 canonical 文件名，绝不 glob 排序。
    """
    st = station if station is not None else CONFIG.get("station")
    for d in auth_dirs():
        if not d.is_dir():
            continue
        if st in STATION_AUTH_FILE:
            p = d / STATION_AUTH_FILE[st]
            if p.is_file():
                return p
            continue          # 该目录没有这个站点的文件 → 试下一个目录
        cands = [d / fn for fn in STATION_AUTH_FILE.values() if (d / fn).is_file()]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    return None


def station_of_file(path: Path | None) -> str | None:
    """由 auth 文件路径反推站点（按文件名）。"""
    if path is None:
        return None
    for st, fn in STATION_AUTH_FILE.items():
        if path.name == fn:
            return st
    return None


def list_stations() -> dict:
    """各站点是否已有登录文件 → {"cn": bool, "intl": bool}（供 GUI 显示可用性）。"""
    out = {st: False for st in STATION_AUTH_FILE}
    for d in auth_dirs():
        if not d.is_dir():
            continue
        for st, fn in STATION_AUTH_FILE.items():
            if (d / fn).is_file():
                out[st] = True
    return out


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{self.get_backend()}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            # 原因已在消息里，from None 避免重复堆栈
            raise RuntimeError(f"刷新 token 网络失败：{e}") from None
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def get_backend(self) -> str:
        """按当前登录账号的 domain 解析后端网关地址。"""
        s = self._session()
        domain = (s.get("auth") or {}).get("domain") or ""
        return backend_host(domain)

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        dom = auth.get("domain") or ""
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "domain": dom,
            # 站点由**账号 domain** 判定（比文件名可靠），backend 也据此选
            "station": station_of_file(self.path) or (
                "intl" if any(k in dom for k in ("workbuddy.ai", "codebuddy.ai")) else "cn"),
            "backend": self.get_backend(),
            "auth_file": self.path.name,
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------


# WorkBuddy 桌面端模型表 displayName（以软件显示为准；内部仍用线上小写名）
WB_DISPLAY_MAP = {
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
    "deepseek-v4.1-flash": "DeepSeek-V4.1-Flash",
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

def catalog_name_map() -> dict:
    """显示名 -> 线上内部名。"""
    return {v: k for k, v in WB_DISPLAY_MAP.items()}

def display_of(name: str | None) -> str | None:
    """线上内部名 -> 显示名；未知回退原名。"""
    if not name: return name
    return WB_DISPLAY_MAP.get(name, name)

def resolve_model(name: str | None) -> str | None:
    """显示名或内部名 -> 线上内部名；未知原样透传。"""
    if not name: return name
    if name in WB_DISPLAY_MAP: return name
    return catalog_name_map().get(name, name)

def probe_model(name: str, timeout: int = 30) -> bool | None:
    """直连上游低成本探测模型是否存活：True 存活 / False 已下线 / None 未知。
    发最小 chat（system+hi，stream=True 读首帧即关），只耗个位数 token。"""
    internal = resolve_model(name) or name
    try:
        af = find_auth_file()
        if af is None:
            return None
        cred = CredentialManager(af)
        url = f"{cred.get_backend()}/v2/chat/completions"
        headers = cred.get_headers()
        body = {"model": internal,
                "messages": [{"role": "system", "content": "you are a helpful assistant"},
                             {"role": "user", "content": "hi"}],
                "stream": True, "max_tokens": 5}
        with httpx.Client(timeout=timeout) as c:
            with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code == 200:
                    for line in r.iter_lines():
                        if line.startswith("data:"):
                            return True
                    return True
                try:
                    d = r.read().decode("utf-8", "replace")
                    j = json.loads(d)
                    detail = j.get("detail") or j
                    code = detail.get("code") if isinstance(detail, dict) else None
                    if code == 11102:
                        return False  # service info not found = 已下线
                    return None
                except Exception:
                    return None
    except Exception:
        return None

def probe_models(names: list, timeout: int = 30) -> dict:
    """批量探测，返回 {name: True/False/None}。"""
    return {n: probe_model(n, timeout) for n in names}

DEFAULT_MODELS = [
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4.1-flash",
    "minimax-m3", "hy3", "hy4-preview", "auto",
]

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None,
                "desensitize": False,
                "exposed_models": None,  # None → 暴露全部 DEFAULT_MODELS；GUI 可设子集
                # 站点：None/"auto" 自动（取最近活跃的登录）；"cn" 国内；"intl" 国际。
                # 只影响**读哪个 auth 文件**，后端始终跟随账号自身 domain。
                "station": None}


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _cred() -> CredentialManager:
    if CONFIG["cred"] is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return CONFIG["cred"]


def apply_station(station: str | None = None) -> dict:
    """运行时切换站点：只换凭据对象，不用重启服务。

    station: None/"auto" → 取 mtime 最新的登录文件；"cn"/"intl" → 精确取该站点。
    返回 summary()，便于 GUI 显示"切过去之后到底是谁在生效"。
    """
    CONFIG["station"] = station if station in STATION_AUTH_FILE else None
    af = find_auth_file()
    CONFIG["cred"] = CredentialManager(af) if af else None
    return CONFIG["cred"].summary() if CONFIG["cred"] else {
        "station": CONFIG["station"], "auth_file": "(未找到)"}


@app.get("/health")
def health():
    cred = CONFIG["cred"]
    info: dict = {"status": "ok", "platform": sys.platform, "python": sys.version.split()[0],
                  "auth_file": str(find_auth_file() or "(未找到)"),
                  "station_requested": CONFIG.get("station") or "auto",
                  "stations_available": list_stations(),
                  "mode": "direct-proxy (native function calling)"}
    if cred is not None:
        try:
            info["credential"] = cred.summary()
        except Exception as e:
            info["credential_error"] = str(e)
    return info


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    data = [{"id": display_of(m), "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in (CONFIG.get("exposed_models") or DEFAULT_MODELS)]
    return {"object": "list", "data": data}


@app.get("/v1/credits")
def credits(authorization: Optional[str] = Header(default=None),
            x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """WorkBuddy 积分余额（上游 billing/meter 资源包汇总）"""
    _check_auth(authorization, x_api_key)
    cred = _cred()
    headers = cred.get_headers()
    headers["Accept-Language"] = "zh"
    url = f"{cred.get_backend()}/billing/meter/get-user-resource-summary"
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(url, headers=headers, json={})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"upstream error: {e}") from None
    try:
        body = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail=r.text[:300]) from None
    if body.get("code") != 0:
        raise HTTPException(status_code=502, detail=f"upstream code={body.get('code')} msg={body.get('msg')}")
    data = body.get("data") or {}
    pkgs = []
    remain = total = used = 0.0
    for p in (data.get("Packages") or []):
        try:
            t, rm, u = float(p.get("CycleTotalCapacity") or 0), float(p.get("CycleRemainCapacity") or 0), float(p.get("CycleUsedCapacity") or 0)
        except (TypeError, ValueError):
            continue
        total += t
        remain += rm
        used += u
        pkgs.append({"code": p.get("PackageCode"), "total": t, "remain": rm,
                     "used": u, "unit": p.get("CapacityUnit")})
    return {"remain": remain, "total": total, "used": used,
            "is_paid_user": bool(data.get("IsPaidUser")), "packages": pkgs}


@app.get("/v1/checkin")
def checkin_status(authorization: Optional[str] = Header(default=None),
                   x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """查今日签到/活动状态：POST {backend}/v2/billing/meter/checkin-activity-status"""
    _check_auth(authorization, x_api_key)
    cred = _cred()
    headers = cred.get_headers()
    headers["Accept-Language"] = "zh"
    url = f"{cred.get_backend()}/v2/billing/meter/checkin-activity-status"
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(url, headers=headers, json={})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"upstream error: {e}") from None
    try:
        body = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail=r.text[:300]) from None
    if body.get("code") != 0:
        raise HTTPException(status_code=502,
                            detail=f"upstream code={body.get('code')} msg={body.get('msg')}")
    return {"ok": True, "data": body.get("data") or {}}


@app.post("/v1/checkin")
def checkin_claim(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """执行每日签到：POST {backend}/v2/billing/meter/daily-checkin"""
    _check_auth(authorization, x_api_key)
    cred = _cred()
    headers = cred.get_headers()
    headers["Accept-Language"] = "zh"
    url = f"{cred.get_backend()}/v2/billing/meter/daily-checkin"
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(url, headers=headers, json={})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"upstream error: {e}") from None
    try:
        body = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail=r.text[:300]) from None
    code = body.get("code")
    # 上游对「今天已领过」也返回非 0（幂等）；把 msg 原样带回去，由 GUI 判定
    return {"ok": code == 0, "code": code, "msg": body.get("msg") or "",
            "data": body.get("data") or {}}


def _checkin_headers(cred) -> dict:
    h = cred.get_headers()
    h["Accept-Language"] = "zh"
    return h


def claim_for_station(station: str) -> dict:
    """**按站点**执行签到，不经过本地服务、不改 CONFIG。

    这样 GUI 可以一次把国内/国际两个账号各领一次，互不干扰。

    返回 {ok, station, already?, credit?, streak?, error?}
    """
    out = {"ok": False, "station": station, "already": False, "credit": 0,
           "streak": 0, "error": ""}
    af = find_auth_file(station)
    if af is None:
        out["error"] = "未登录（找不到该版本的 auth 文件）"
        return out
    try:
        cred = CredentialManager(af)
        headers = _checkin_headers(cred)
        base = cred.get_backend()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"读取凭据失败: {e}"
        return out
    try:
        with httpx.Client(timeout=25) as c:
            # ① 查状态
            try:
                r = c.post(f"{base}/v2/billing/meter/checkin-activity-status",
                           headers=headers, json={})
                st = r.json()
            except Exception as e:  # noqa: BLE001
                out["error"] = f"查询签到状态失败: {e}"
                return out
            d = (st.get("data") or {}) if st.get("code") == 0 else {}
            if d.get("today_checked_in"):
                out.update({"ok": True, "already": True,
                            "credit": d.get("today_credit") or 0,
                            "streak": d.get("streak_days") or 0})
                return out
            # ② 领
            try:
                r2 = c.post(f"{base}/v2/billing/meter/daily-checkin",
                            headers=headers, json={})
                res = r2.json()
            except Exception as e:  # noqa: BLE001
                out["error"] = f"签到请求失败: {e}"
                return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"签到请求异常: {e}"
        return out

    data = res.get("data") or {}
    if res.get("code") == 0 or "credit" in data:
        out.update({"ok": True, "credit": data.get("credit") or 0,
                    "streak": data.get("streak_days") or 0,
                    "msg": str(res.get("msg") or "")})
        return out
    msg = str(res.get("msg") or "")
    # 幂等：上游对「今天已经领过」会返回非 0，措辞有几种，收紧匹配避免把
    # 真错误（如「活动已结束」）误判成已领取。
    if any(k in msg for k in ("已签到", "已领取", "已领过", "已参与",
                              "重复", "already checked", "already claimed")):
        out.update({"ok": True, "already": True, "msg": msg})
        return out
    out["msg"] = msg
    out["error"] = f"upstream code={res.get('code')} msg={msg}"
    return out


def checkin_status_for_station(station: str) -> dict:
    """**按站点**查签到状态（不发起领取）。返回 {ok, today_checked_in, streak_days,
    daily_credit, today_credit, active, error}。"""
    af = find_auth_file(station)
    if af is None:
        return {"ok": False, "error": "未登录"}
    try:
        cred = CredentialManager(af)
        headers = _checkin_headers(cred)
        base = cred.get_backend()
        with httpx.Client(timeout=20) as c:
            r = c.post(f"{base}/v2/billing/meter/checkin-activity-status",
                       headers=headers, json={})
            body = r.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if body.get("code") != 0:
        return {"ok": False, "error": f"code={body.get('code')} msg={body.get('msg')}"}
    d = body.get("data") or {}
    return {"ok": True, "active": bool(d.get("active")),
            "today_checked_in": bool(d.get("today_checked_in")),
            "streak_days": d.get("streak_days") or 0,
            "daily_credit": d.get("daily_credit") or 0,
            "today_credit": d.get("today_credit") or 0}


# ---------------------------------------------------------------------------
# 模型徽章 / 优惠标签（**仅用于界面展示**，内部模型名不变）
#
# 数据源：WorkBuddy 桌面端缓存的远端产品配置
#   ~/.workbuddy/cache/acc-product-config-v3.json
# 其中两处会产出徽章：
#   · models[].tags 里的 "badge:<标签>:<hex颜色>"
#   · 顶层 modelPromotions[]（带 schedule 时段 / validFrom-validUntil 区间、
#     badge.label 文案、discount.discountedCredits 折扣倍率）
# 桌面端自己的规则：活动徽章带时间调度与折扣语义，优先于 tags 侧同名项。
# ---------------------------------------------------------------------------

WB_CONFIG_CACHE = Path.home() / ".workbuddy" / "cache" / "acc-product-config-v3.json"

# 北京时间（中国无夏令时，固定 +08:00）
_CST = timezone(timedelta(hours=8))


def load_product_config() -> dict:
    """读桌面端缓存的远端产品配置；读不到返回 {}。"""
    try:
        return json.loads(WB_CONFIG_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _parse_hhmm(s: str) -> int | None:
    """'23:00' -> 分钟数；非法返回 None。"""
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return None


def promotions_for(model_id: str, cfg: dict | None = None) -> list:
    """返回该模型当前**命中**的推广活动（已按 schedule 过滤），按 priority 降序。"""
    cfg = cfg if cfg is not None else load_product_config()
    now = datetime.now(_CST)
    now_min = now.hour * 60 + now.minute
    out = []
    for p in (cfg.get("modelPromotions") or []):
        if not isinstance(p, dict) or not p.get("enabled", True):
            continue
        ids = p.get("modelIds") or []
        if model_id not in ids:
            continue
        sch = p.get("schedule") or {}
        ok = True
        # 绝对区间
        vf, vu = sch.get("validFrom"), sch.get("validUntil")
        try:
            if vf and now < datetime.fromisoformat(str(vf).replace("Z", "+00:00")):
                ok = False
            if vu and now > datetime.fromisoformat(str(vu).replace("Z", "+00:00")):
                ok = False
        except Exception:  # noqa: BLE001
            pass
        # 每日时段（可跨零点）
        daily = sch.get("daily") or []
        if ok and daily:
            hit = False
            for w in daily:
                s = _parse_hhmm((w or {}).get("start"))
                e = _parse_hhmm((w or {}).get("end"))
                if s is None or e is None:
                    continue
                if s <= e:
                    hit = hit or (s <= now_min < e)
                else:                      # 跨零点，如 23:00 → 7:50
                    hit = hit or (now_min >= s or now_min < e)
            ok = ok and hit
        if ok:
            out.append(p)
    out.sort(key=lambda x: x.get("priority") or 0, reverse=True)
    return out


def badge_labels(cfg: dict | None = None) -> dict:
    """{模型内部名: [展示标签, ...]}。

    标签形如 "夜间折扣 0.50x" / "限时免费"；只用于 GUI 显示，
    内部发请求时仍用**原始模型名**（resolve_model 不受影响）。
    """
    cfg = cfg if cfg is not None else load_product_config()
    models = cfg.get("models") or []
    out: dict = {}

    # ① 活动徽章（带折扣语义，优先）
    promo_by_model: dict = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        labels = []
        for p in promotions_for(mid, cfg):
            b = p.get("badge") or {}
            lab = (b.get("label") or "").strip()
            d = p.get("discount") or {}
            dc = (d.get("discountedCredits") or "").strip()
            # badge.display == "activeOnly" 时只在活动进行中显示——
            # promotions_for 已按 schedule 过滤，命中即进行中
            if lab and dc:
                labels.append(f"{lab} {dc}")
            elif lab:
                labels.append(lab)
        if labels:
            promo_by_model[mid] = labels

    # ② tags 里的 badge:<label>:<color>（活动同名的剔除，与桌面端一致）
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        tag_labels = []
        for t in (m.get("tags") or []):
            if not isinstance(t, str) or not t.startswith("badge:"):
                continue
            rest = t[6:]
            i = rest.rfind(":")
            lab = (rest[:i] if i > 0 else rest).strip()
            if lab and lab not in tag_labels:
                tag_labels.append(lab)
        promo_labels = promo_by_model.get(mid) or []
        promo_plain = {x.split(" ")[0].lower() for x in promo_labels}
        tag_labels = [x for x in tag_labels if x.lower() not in promo_plain]
        merged = promo_labels + tag_labels
        if merged:
            out[mid] = merged
    return out


@app.get("/v1/model_badges")
def model_badges(authorization: Optional[str] = Header(default=None),
                 x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """模型展示标签（仅界面用）。键=显示名，值=标签数组，方便 GUI 直接匹配。"""
    _check_auth(authorization, x_api_key)
    raw = badge_labels()
    return {"badges": raw,
            "by_display": {display_of(k): v for k, v in raw.items()},
            "source": str(WB_CONFIG_CACHE),
            "cache_exists": WB_CONFIG_CACHE.is_file()}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}}) from None

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    body["model"] = resolve_model(body.get("model")) or "auto"  # 显示名回写为线上内部名
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 ZCode 的 system 声明）被后端误判为敏感词。
    # 只对 system 角色消息里的"合规声明高频词"插入零宽空格，不改用户输入。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system",))

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    url = f"{cred.get_backend()}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                    _log(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8','replace')}")
                    raise HTTPException(status_code=r.status_code, detail=_safe_err_raw(raw, r.status_code))
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}}) from None
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []   # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True

    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                    yield _err_event(err, r.status_code)
                    return
                async for chunk in r.aiter_bytes():
                    if chunk:
                        raw_parts.append(chunk)
                        _feed(chunk)
                        yield chunk
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        yield _err_event(str(e).encode(), 502)

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
         + (f" | tool_calls={tool_names}" if tool_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整原始 SSE（后端返回的全部内容）
    _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    af = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {CONFIG.get('resolved_backend') or BACKEND} (按登录域名自动解析)\n")
    st_req = CONFIG.get("station") or "auto"
    avail = list_stations()
    sys.stderr.write(f"站点选择  : {st_req}"
                     f"  (可用：国内={'有' if avail.get('cn') else '无'} / "
                     f"国际={'有' if avail.get('intl') else '无'})\n")
    for st, fn in STATION_AUTH_FILE.items():
        p = next((d / fn for d in auth_dirs() if (d / fn).is_file()), None)
        sys.stderr.write(f"  [{STATION_LABEL[st]}] {fn:<28} "
                         f"{'✓ ' + str(p) if p else '（未登录）'}\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    ok = True
    if af is None:
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        try:
            cm = CredentialManager(af)
            info = cm.summary()
            sys.stderr.write(f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}\n")
            sys.stderr.write(f"站点/后端 : {info.get('station')} → {info.get('backend')}\n")
            sys.stderr.write(f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n")
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败：{e}\n")
            ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--station", choices=("auto", "cn", "intl"), default="auto",
                    help="用哪个站点的登录凭据：auto=最近活跃 / cn=国内(workbuddy.cn) / "
                         "intl=国际(workbuddy.ai)。两版可同时登录，互不覆盖。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    # None → find_auth_file() 自动取最近活跃的那个
    CONFIG["station"] = None if args.station == "auto" else args.station
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    af = find_auth_file()
    CONFIG["cred"] = CredentialManager(af) if af else None
    if CONFIG["cred"] is not None:
        try:
            CONFIG["resolved_backend"] = CONFIG["cred"].get_backend()
        except Exception:
            pass

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        sys.stderr.write("   脱敏      : 已启用（system 合规词零宽处理）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log("==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
