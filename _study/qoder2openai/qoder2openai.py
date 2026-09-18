# -*- coding: utf-8 -*-
"""
qoder2openai.py — 把 Qoder CLI 包装成 OpenAI 兼容的反代。

背景：Qoder 没有独立的 HTTP 推理 API，AI 能力全部通过命令行 `qodercli` /
`qoderclicn` 提供。本模块的做法是：

  OpenAI /v1/chat/completions
        │
        ▼
  spawn node qodercli.js --print --output-format stream-json
        │
        ▼
  逐行解析 stream-json 事件 → 合成 OpenAI SSE 流

凭据：**全自动，无需手填 Token**
  Qoder 桌面端登录后会把 accessToken 存在
      %APPDATA%\\com.qoder.app.stable\\auth.v1.dat      （国际版）
      %APPDATA%\\com.qodercn.app.stable\\auth.v1.dat    （国内版）
  该文件是 Electron safeStorage 加密的（"v10" + AES-256-GCM，密钥由 Local State
  的 encrypted_key 经 DPAPI 解出）。解开拿到 accessToken 后，调
      POST {openapi}/api/v1/me/jobToken   {"clientId": ...}
  换一个 JobTokenCredential，再通过环境变量 QODER_JOB_TOKEN 传给 CLI。
  全程复用桌面端登录态，和 WorkBuddy / MonkeyCode 等通道的体验一致。

  也支持手动填 Personal Access Token（QODER_PERSONAL_ACCESS_TOKEN）作为覆盖项。

设计要点
  · 每请求一个新子进程（CLI 是单轮进程模型，复用会串会话）
  · 只做最纯粹的对话能力：不启工具、不做 MCP、不做 skill，避免副作用
  · 国内 / 国际分开：两套凭据、两个 CLI、两套模型名
  · **不做号池**（用户明确要求，Qoder 风控严，避免触发封号）

CLI 参数约定（对齐官方 @qoder-ai/qoder-agent-sdk 与社区 qoder-proxy）：
  --print --output-format stream-json --input-format stream-json
  --no-session-persistence --permission-mode bypassPermissions
  --dangerously-skip-permissions --disallowed-tools '*' --tools '' --model <m>

stream-json 协议（协议版本 1.4.0）：
  入：{"type":"user","message":{"role":"user","content":[{"type":"text","text":"…"}]},
       "parent_tool_use_id":null}
  出：
    {"type":"system","subtype":"init",…}                      ← 会话元数据，忽略
    {"type":"assistant","message":{…},"error":"…"}            ← 可能多条；带 error 即失败
    {"type":"result","result":"…","is_error":bool,"usage":{…}} ← 结束
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("qoder2openai")

APP_DATA = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local"))
PROGRAM_FILES = os.environ.get("ProgramFiles", r"C:\Program Files")
PROGRAM_FILES_X86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

# IDE 登录凭据的 clientId（从 IDE 主进程 authClientIds.prod 反编译得到）
_OAUTH_CLIENT_ID = "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa"

# --------------------------------------------------------------------------
# 站点配置：国内 / 国际两套独立凭据、CLI、模型
# --------------------------------------------------------------------------

STATIONS: dict[str, dict[str, Any]] = {
    "intl": {
        "label": "国际版",
        "cli_bin": "qodercli",
        "pat_env": "QODER_PERSONAL_ACCESS_TOKEN",
        "job_env": "QODER_JOB_TOKEN",
        "npm_pkg": "@qoder-ai/qodercli",
        "config_dir": ".qoder",
        "udd_name": "com.qoder.app.stable",          # %APPDATA%\<这里>\auth.v1.dat
        "openapi_base": "https://openapi.qoder.sh",  # /api/v1/me/jobToken
        "domains": ["api.qoder.com", "api3.qoder.sh", "openapi.qoder.sh"],
        # IDE 安装位置（自带 CLI 引擎与登录态）
        "ide_dirs": [
            os.path.join(LOCAL_APP_DATA, "Programs", "Qoder"),
            os.path.join(PROGRAM_FILES, "Qoder"),
        ],
        "ide_exe": "Qoder.exe",
        "ide_sdk": "@qoder-ai/qoder-agent-sdk",
        "models": [
            "Auto", "Ultimate", "Performance", "Efficient", "Sonus", "Cantus",
            "Qwen3.8-Max", "Qwen3.8-Flash", "Qwen3.7-Max", "Qwen3.7-Plus",
            "Kimi-K3", "Kimi-K2.8-Preview", "GLM-5.3", "GLM-5.3-Flash",
            "DeepSeek-V4-Pro", "DeepSeek-Flash", "MiniMax-M3",
        ],
    },
    "cn": {
        "label": "国内版",
        "cli_bin": "qoderclicn",
        "pat_env": "QODERCN_PERSONAL_ACCESS_TOKEN",
        "job_env": "QODERCN_JOB_TOKEN",
        "npm_pkg": "@qodercn-ai/qoderclicn",
        "config_dir": ".qoder-cn",
        "udd_name": "com.qodercn.app.stable",
        "openapi_base": "https://openapi.qoder.com.cn",
        "domains": ["api.qoder.cn", "openapi.qoder.com.cn"],
        "ide_dirs": [
            os.path.join(PROGRAM_FILES, "Qoder CN"),
            os.path.join(LOCAL_APP_DATA, "Programs", "Qoder CN"),
        ],
        "ide_exe": "Qoder CN.exe",
        "ide_sdk": "@qoder-ai/qoder-cn-agent-sdk",
        "models": [
            "Auto", "Qwen3.8-Max", "Qwen3.8-Flash", "Qwen3.7-Max", "Qwen3.7-Plus",
            "Qwen3.7-Flash", "DeepSeek-V4-Pro", "DeepSeek-Flash",
            "GLM-5.3", "GLM-5.3-Flash", "GLM-5.2",
            "Kimi-K3", "Kimi-K2.8-Preview", "MiniMax-M2.7",
        ],
    },
}

# 常见 OpenAI 名 → Qoder 名（客户端写 gpt-4o 也能用）
MODEL_ALIASES = {
    "gpt-4o": "Auto", "gpt-4": "Auto", "gpt-4.1": "Auto",
    "gpt-3.5-turbo": "Efficient", "gpt-4o-mini": "Efficient",
    "claude-3.5-sonnet": "Auto", "claude-sonnet-4-5": "Auto",
    "claude-3-opus": "Ultimate", "o1": "Ultimate", "o3": "Ultimate",
    "deepseek": "DeepSeek-V4-Pro", "qwen": "Qwen3.8-Max",
    "kimi": "Kimi-K3", "glm": "GLM-5.3", "minimax": "MiniMax-M3",
}


def _all_models() -> set[str]:
    out: set[str] = set()
    for m in STATIONS.values():
        out.update(m["models"])
    return out


def _normalize_model(name: str | None, station: str | None = None) -> str:
    """把客户端传来的模型名映射成 Qoder 认的名字（大小写不敏感）。"""
    models = STATIONS.get(station or CONFIG.get("station", "intl"),
                          STATIONS["intl"])["models"]
    lowered = {m.lower(): m for m in models}
    if not name:
        return lowered.get("auto", "Auto")
    n = name.strip()
    low = n.lower()
    if low in lowered:
        return lowered[low]
    if low in MODEL_ALIASES:
        cand = MODEL_ALIASES[low]
        return lowered.get(cand.lower(), cand)
    # 启发式：含 max/pro/ultimate → 高阶；含 flash/mini/lite/efficient → 轻量
    if any(k in low for k in ("ultimate", "opus", "max", "pro")):
        for k in ("ultimate", "qwen3.8-max", "deepseek-v4-pro"):
            if k in lowered:
                return lowered[k]
    if any(k in low for k in ("flash", "mini", "lite", "efficient", "turbo")):
        for k in ("efficient", "qwen3.8-flash", "deepseek-flash"):
            if k in lowered:
                return lowered[k]
    return lowered.get("auto", "Auto")


# --------------------------------------------------------------------------
# 全局配置（由 GUI 启动时下发）
# --------------------------------------------------------------------------

CONFIG: dict[str, Any] = {
    "station": "intl",       # 生效站点
    "cli_path": "",          # 可选：手动指定（IDE exe / 安装目录 / CLI 脚本都行）
    "pat": "",               # 可选：手动 PAT（留空则自动用桌面端登录态）
    "timeout_s": 180,        # 单请求上限
    "node_path": "",         # 可选：手动指定 node
    "api_key": "",           # 本地 /v1 的鉴权 key（可空）
}

_STATION_OVERRIDE: dict[str, dict[str, Any]] = {}


def apply_config(cfg: dict[str, Any]) -> None:
    if not isinstance(cfg, dict):
        return
    CONFIG.update(cfg)


def apply_station(station: str, cli_path: str = "", pat: str = "") -> bool:
    """热切换站点；旧站点的手动配置保留在 _STATION_OVERRIDE。"""
    station = station if station in STATIONS else "intl"
    cur = CONFIG.get("station", "intl")
    if cur != station and (CONFIG.get("cli_path") or CONFIG.get("pat")):
        _STATION_OVERRIDE[cur] = {"cli_path": CONFIG.get("cli_path", ""),
                                  "pat": CONFIG.get("pat", "")}
    saved = _STATION_OVERRIDE.get(station, {})
    CONFIG["station"] = station
    CONFIG["cli_path"] = cli_path or saved.get("cli_path", "")
    CONFIG["pat"] = pat or saved.get("pat", "")
    return True


def station_meta() -> dict[str, Any]:
    return STATIONS.get(CONFIG.get("station", "intl"), STATIONS["intl"])


def pick_auto(pat_cn: str = "", pat_intl: str = "") -> str:
    """auto 选站：有登录态的优先；两边都有则偏好国际。"""
    def _logged_in(st: str) -> bool:
        try:
            return ide_credential(st) is not None
        except Exception:
            return False
    cn_ok, intl_ok = _logged_in("cn"), _logged_in("intl")
    if cn_ok and not intl_ok:
        return "cn"
    if intl_ok and not cn_ok:
        return "intl"
    if not cn_ok and not intl_ok:
        # 都没有登录态 → 看 PAT / CLI
        if pat_cn and not pat_intl:
            return "cn"
        if pat_intl and not pat_cn:
            return "intl"
        cn_cli = cli_health_for("cn").get("cli_found", False)
        intl_cli = cli_health_for("intl").get("cli_found", False)
        if cn_cli and not intl_cli:
            return "cn"
    return "intl"


def resolve_station(choice: str, pat_cn: str = "", pat_intl: str = "") -> str:
    if choice in ("cn", "intl"):
        return choice
    return pick_auto(pat_cn, pat_intl)


# --------------------------------------------------------------------------
# ① 解 IDE 登录态：DPAPI + AES-256-GCM
# --------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi_unprotect(data: bytes) -> bytes:
    """CryptUnprotectData（当前用户上下文）。仅 Windows。"""
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅 Windows 可用")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ib = _blob(data)
    ob = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(ib), None, None, None, None,
                                      0, ctypes.byref(ob)):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(ob.pbData, ob.cbData)
    finally:
        kernel32.LocalFree(ob.pbData)


def _master_key(udd: Path) -> bytes:
    """从 <userdata>/Local State 取 AES 主密钥（DPAPI 包了一层）。"""
    ls = udd / "Local State"
    d = json.loads(ls.read_text(encoding="utf-8", errors="replace"))
    enc = base64.b64decode(d["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise ValueError(f"encrypted_key 前缀异常: {enc[:5]!r}")
    return _dpapi_unprotect(enc[5:])


def _aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] != b"v10":
        raise ValueError(f"auth.v1.dat 前缀异常: {blob[:3]!r}")
    body = blob[3:]
    return AESGCM(key).decrypt(body[:12], body[12:], None)


def ide_credential(station: str) -> dict[str, Any] | None:
    """读桌面端登录态。返回 {token, refreshToken, expiresAt, user} 或 None。

    绝不打印 token 值；调用方只应使用结构和用户信息。
    """
    meta = STATIONS.get(station, STATIONS["intl"])
    udd = Path(APP_DATA) / meta["udd_name"]
    auth = udd / "auth.v1.dat"
    if not auth.is_file():
        return None
    try:
        data = _aes_gcm_decrypt(_master_key(udd), auth.read_bytes())
        d = json.loads(data.decode("utf-8"))
        if not isinstance(d.get("token"), str) or not d["token"]:
            return None
        return d
    except Exception as e:  # noqa: BLE001
        log.debug("[qd] 读取 %s 登录态失败: %s", meta["udd_name"], e)
        return None


# --------------------------------------------------------------------------
# ② accessToken → JobTokenCredential
# --------------------------------------------------------------------------

def _post_json(url: str, body: dict, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(body).encode("utf-8"))
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def mint_job_token(station: str, access_token: str) -> dict[str, Any]:
    """用 accessToken 换 JobTokenCredential（含 24h token + 48h refresh_token）。"""
    meta = STATIONS.get(station, STATIONS["intl"])
    url = f"{meta['openapi_base']}/api/v1/me/jobToken"
    return _post_json(url, {"clientId": _OAUTH_CLIENT_ID},
                      {"Authorization": f"Bearer {access_token}"})


def refresh_job_token(station: str, refresh_token: str) -> dict[str, Any]:
    meta = STATIONS.get(station, STATIONS["intl"])
    url = f"{meta['openapi_base']}/api/v1/jobToken/refresh"
    return _post_json(url, {"refresh_token": refresh_token}, {})


# 站点 → 缓存的 job 凭据（进程内）
_JOB_CACHE: dict[str, dict[str, Any]] = {}


def _expires_at(cred: dict) -> float:
    """把凭据里的 expires_at（ISO）转成 epoch 秒；解析不了返回 0。"""
    s = cred.get("expires_at") or ""
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def get_job_credential(station: str, force: bool = False) -> dict[str, Any] | None:
    """取（必要时刷新/重铸）本机桌面端登录态对应的 JobTokenCredential。

    顺序：内存缓存 → refresh_token 刷新 → 用 accessToken 重新铸。
    提前 5 分钟视为过期，避免用着用着失效。
    """
    now = time.time()
    cached = _JOB_CACHE.get(station)
    if cached and not force and _expires_at(cached) - now > 300:
        return cached

    # 1) 用缓存的 refresh_token 刷新（更轻，不动 IDE token）
    if cached and cached.get("refresh_token"):
        rt_exp = cached.get("refresh_token_expires_at") or ""
        try:
            from datetime import datetime
            ok = (not rt_exp) or (
                datetime.fromisoformat(str(rt_exp).replace("Z", "+00:00")).timestamp() - now > 300)
        except Exception:
            ok = True
        if ok:
            try:
                fresh = refresh_job_token(station, cached["refresh_token"])
                if fresh.get("token"):
                    _JOB_CACHE[station] = fresh
                    log.debug("[qd] jobToken 已刷新（%s）", station)
                    return fresh
            except Exception as e:  # noqa: BLE001
                log.debug("[qd] jobToken 刷新失败，改走重铸: %s", e)

    # 2) 用桌面端 accessToken 重新铸
    ide = ide_credential(station)
    if not ide:
        return None
    try:
        cred = mint_job_token(station, ide["token"])
        if cred.get("token"):
            _JOB_CACHE[station] = cred
            log.debug("[qd] jobToken 已重铸（%s）", station)
            return cred
    except Exception as e:  # noqa: BLE001
        log.warning("[qd] 铸 jobToken 失败（%s）: %s", station, e)
    return None


def credential_status(station: str) -> dict[str, Any]:
    """给 GUI 用的凭据概览（不含任何 token 值）。"""
    meta = STATIONS.get(station, STATIONS["intl"])
    out: dict[str, Any] = {
        "station": station, "label": meta["label"],
        "ide_found": False, "account": "", "email": "", "phone": "",
        "ide_expires": "", "pat_set": False, "ok": False, "error": "",
    }
    ide = ide_credential(station)
    if ide:
        out["ide_found"] = True
        u = ide.get("user") or {}
        out["account"] = u.get("name", "")
        out["email"] = u.get("email", "")
        out["phone"] = u.get("phone", "")
        out["ide_expires"] = ide.get("expiresAt", "")
    if CONFIG.get("pat"):
        out["pat_set"] = True
    if out["pat_set"]:
        out["ok"] = True
    elif ide:
        out["ok"] = True
    else:
        out["error"] = (f"未找到 {meta['label']} 桌面端登录态"
                        f"（%APPDATA%\\{meta['udd_name']}\\auth.v1.dat）")
    return out


# --------------------------------------------------------------------------
# CLI 定位
# --------------------------------------------------------------------------

def ide_cli_candidates(ide_dir: str, meta: dict[str, Any]) -> list[str]:
    """给定 IDE 安装目录，列出自带 CLI 引擎的候选路径。"""
    sdk = meta.get("ide_sdk", "")
    if not sdk:
        return []
    unfpacked = os.path.join(ide_dir, "resources", "app.asar.unpacked")
    out = []
    for runtime in ("qoder-worker-runtime.obf.mjs", "qoder-worker-runtime.mjs"):
        out.append(os.path.join(unfpacked, "node_modules", sdk, "dist", "_worker", runtime))
    for name in (meta["cli_bin"] + ".js", meta["cli_bin"]):
        out.append(os.path.join(unfpacked, name))
        out.append(os.path.join(ide_dir, "resources", name))
    return out


def _find_ide_dir(path: str, station: str) -> str:
    """把「IDE exe / IDE 目录 / 子路径」归一化成 IDE 根目录。"""
    meta = STATIONS.get(station, STATIONS["intl"])
    p = Path(path)
    base = p if p.is_dir() else p.parent
    # 逐级上溯，找带 resources/app.asar.unpacked 的那一层
    cur = base
    for _ in range(4):
        if (cur / "resources" / "app.asar.unpacked").is_dir():
            return str(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    # 上溯失败就按已知 exe 名反查
    for d in meta.get("ide_dirs", []):
        if d and os.path.normcase(str(base)).startswith(os.path.normcase(d)):
            return d
    return str(base)


def _resolve_cli_path(path: str, station: str) -> str | None:
    """path 可能是 IDE exe / 安装目录 / 直接就是 CLI 脚本，返回 CLI 脚本路径。"""
    if not path:
        return None
    p = Path(path)
    if p.is_file() and p.suffix.lower() in (".js", ".mjs", ".cjs"):
        return str(p)
    if p.is_file() and p.suffix.lower() == ".exe":
        p = p.parent
    if not p.is_dir():
        return None
    meta = STATIONS.get(station, STATIONS["intl"])
    root = _find_ide_dir(str(p), station)
    for cand in ide_cli_candidates(root, meta):
        if os.path.isfile(cand):
            return cand
    return None


def _wrap_node(path: str) -> tuple[str, list[str]]:
    """js/mjs 用 node 跑；其它直接执行。"""
    if path.lower().endswith((".js", ".mjs", ".cjs")):
        node = CONFIG.get("node_path") or shutil.which("node") or "node"
        return node, [node, path]
    return path, [path]


def detect_cli(station: str) -> dict[str, Any]:
    """探测 CLI（不启动进程）。返回 {found,path,hint,error}。"""
    meta = STATIONS.get(station, STATIONS["intl"])
    saved_station, saved_cli = CONFIG.get("station"), CONFIG.get("cli_path")
    try:
        CONFIG["station"], CONFIG["cli_path"] = station, ""
        cmd, prefix = _find_cli(station)
        path = prefix[-1]
        if "app.asar.unpacked" in path:
            ide_name = ""
            for d in meta.get("ide_dirs", []):
                if d and os.path.normcase(path).startswith(os.path.normcase(d)):
                    ide_name = os.path.basename(d)
                    break
            hint = f"IDE 自带（{ide_name}）" if ide_name else "IDE 自带"
        elif "node_modules" in path:
            hint = "npm 全局"
        else:
            hint = "PATH"
        return {"found": True, "path": path, "command": cmd, "hint": hint, "error": ""}
    except FileNotFoundError as e:
        return {"found": False, "path": "", "command": "", "hint": "", "error": str(e)}
    finally:
        CONFIG["station"], CONFIG["cli_path"] = saved_station, saved_cli


def _find_cli(station: str | None = None) -> tuple[str, list[str]]:
    """返回 (command, argv)。优先级：手动指定 > PATH > npm 全局 > IDE 自带。"""
    st = station or CONFIG.get("station", "intl")
    meta = STATIONS.get(st, STATIONS["intl"])

    manual = CONFIG.get("cli_path") or ""
    if manual:
        resolved = _resolve_cli_path(manual, st)
        if resolved:
            return _wrap_node(resolved)

    for name in (meta["cli_bin"], "qoder"):
        p = shutil.which(name)
        if p:
            return _wrap_node(p)

    pkg_scope, pkg_name = meta["npm_pkg"].split("/", 1)
    npm_bin = os.path.join(APP_DATA, "npm")
    for c in (os.path.join(npm_bin, meta["cli_bin"]),
              os.path.join(npm_bin, meta["cli_bin"] + ".cmd"),
              os.path.join(npm_bin, "node_modules", pkg_scope, pkg_name,
                           "bundle", meta["cli_bin"] + ".js")):
        if os.path.isfile(c):
            return _wrap_node(c)

    for ide in meta.get("ide_dirs", []):
        if not ide or not os.path.isdir(ide):
            continue
        for cand in ide_cli_candidates(ide, meta):
            if os.path.isfile(cand):
                return _wrap_node(cand)

    raise FileNotFoundError(
        f"未找到 Qoder {meta['label']} CLI（{meta['cli_bin']}）。"
        f"可任选其一：① 安装 Qoder {meta['label']} 桌面端（自带 CLI，推荐）；"
        f"② npm install -g {meta['npm_pkg']}；"
        f"③ 在「程序位置({'国内' if st == 'cn' else '国际'})」里指定。"
    )


def cli_health_for(station: str) -> dict[str, Any]:
    """只读探测 CLI 状态（不动 CONFIG），供 GUI 与 /health 使用。"""
    meta = STATIONS.get(station, STATIONS["intl"])
    d = detect_cli(station)
    out = {"cli_found": d["found"], "cli_path": d.get("path", ""),
           "cli_source": d.get("hint", ""), "error": d.get("error", ""),
           "cli_bin": meta["cli_bin"], "label": meta["label"]}
    try:
        node = shutil.which("node")
        if node:
            r = subprocess.run([node, "--version"], capture_output=True, timeout=5)
            out["node_version"] = r.stdout.decode().strip()
    except Exception:
        out["node_version"] = ""
    return out


def cli_health() -> dict[str, Any]:
    st = CONFIG.get("station", "intl")
    h = cli_health_for(st)
    h["station"] = st
    h["pat_set"] = bool(CONFIG.get("pat"))
    h["timeout_s"] = CONFIG.get("timeout_s", 180)
    cred = credential_status(st)
    h["ide_found"] = cred["ide_found"]
    h["account"] = cred["account"]
    h["credential_ok"] = cred["ok"]
    return h


# --------------------------------------------------------------------------
# 环境变量（把凭据交给 CLI）
# --------------------------------------------------------------------------

def build_env() -> dict[str, str]:
    """构造子进程环境：优先自动 jobToken；手填 PAT 时以 PAT 为准。"""
    env = os.environ.copy()
    meta = station_meta()
    st = CONFIG.get("station", "intl")
    # 清掉可能存在的旧值，避免互相干扰
    for m in STATIONS.values():
        env.pop(m["job_env"], None)
        env.pop(m["pat_env"], None)

    pat = (CONFIG.get("pat") or "").strip()
    if pat:
        env[meta["pat_env"]] = pat
        return env
    cred = get_job_credential(st)
    if cred:
        env[meta["job_env"]] = json.dumps(cred)
    return env


def credential_summary() -> str:
    """给日志用的一句话（不含令牌值）。"""
    st = CONFIG.get("station", "intl")
    meta = STATIONS[st]
    if (CONFIG.get("pat") or "").strip():
        return f"{meta['label']} · 手动 PAT"
    ide = ide_credential(st)
    if ide:
        u = ide.get("user") or {}
        who = u.get("name") or u.get("email") or u.get("phone") or "已登录账号"
        return f"{meta['label']} · 桌面端登录态（{who}）"
    return f"{meta['label']} · 无凭据"


# --------------------------------------------------------------------------
# stream-json 消息转换
# --------------------------------------------------------------------------

def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


def openai_to_stream_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """把 OpenAI messages 压成 stream-json 的 user 事件（CLI 只吃一条）。"""
    texts: list[str] = []
    system_parts: list[str] = []
    for m in messages:
        role = m.get("role", "")
        content = _msg_text(m.get("content", ""))
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            texts.append(content)
        elif role == "assistant":
            texts.append(f"assistant: {content}")
    prefix = "\n\n".join(system_parts)
    body = "\n\n---\n\n".join(texts)
    prompt = f"{prefix}\n\n---\n\n{body}" if prefix and body else (prefix or body)
    if not prompt:
        prompt = "（空消息）"
    return {"type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            "parent_tool_use_id": None}


def cli_event_to_text(ev: dict[str, Any]) -> str:
    t = ev.get("type")
    if t == "assistant":
        msg = ev.get("message") or {}
        return "".join(c.get("text", "") for c in (msg.get("content") or [])
                       if isinstance(c, dict) and c.get("type") == "text")
    if t == "result":
        return ev.get("result", "") or ""
    return ""


# --------------------------------------------------------------------------
# 子进程调用
# --------------------------------------------------------------------------

def _fixed_args(model: str) -> list[str]:
    return ["--print",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--no-session-persistence",
            "--permission-mode", "bypassPermissions",
            "--dangerously-skip-permissions",
            "--disallowed-tools", "*",
            "--tools", "",
            "--model", model]


async def _run_cli_once(model: str, prompt_json: str,
                        log_fn: Callable[[str], None] | None = None
                        ) -> tuple[str, dict[str, Any], list[str]]:
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    cmd, prefix = _find_cli()
    full_cmd = list(prefix) + _fixed_args(model)
    _log(f"[qd] spawn: {' '.join(shlex.quote(x) for x in full_cmd)}")

    # 凭据准备可能要发 HTTP（铸 jobToken），必须放线程里
    try:
        env = await asyncio.to_thread(build_env)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"准备 Qoder 凭据失败：{type(e).__name__}: {e}") from None

    timeout_s = int(CONFIG.get("timeout_s", 180))
    proc = await asyncio.to_thread(
        subprocess.Popen, full_cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, bufsize=0,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _log(f"[qd] pid={proc.pid}")

    async def _write_stdin() -> None:
        try:
            if proc.stdin:
                proc.stdin.write(prompt_json.encode("utf-8") + b"\n")
                proc.stdin.flush()
                proc.stdin.close()
        except Exception as e:  # noqa: BLE001
            _log(f"[qd] stdin write failed: {e}")

    write_task = asyncio.create_task(_write_stdin())

    texts: list[str] = []
    usage: dict[str, Any] = {}
    stderr_lines: list[str] = []
    raw_lines: list[str] = []
    cli_error: str = ""
    exit_code: int = -1

    async def _read_stdout() -> None:
        nonlocal texts, cli_error
        assert proc.stdout
        while True:
            line_bytes = await asyncio.to_thread(proc.stdout.readline)
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                raw_lines.append(line)
                if len(raw_lines) > 30:
                    raw_lines.pop(0)
                _log(f"[qd] non-JSON stdout: {line[:160]}")
                continue
            t = ev.get("type")
            if t == "assistant":
                # ⚠ CLI 用 assistant 事件 + error 字段报错（authentication_failed 等），
                # 此时正文是 "Not logged in · …" 这类提示，绝不能当正常回复返回。
                ev_err = ev.get("error") or ""
                text = cli_event_to_text(ev)
                if ev_err:
                    cli_error = cli_error or (f"{ev_err}: {text}" if text else ev_err)
                    _log(f"[qd] CLI error event: {ev_err} — {text[:120]}")
                    continue
                if text:
                    texts.append(text)
            elif t == "result":
                usage = ev.get("usage") or {}
                if ev.get("is_error"):
                    err = ev.get("result") or ev.get("terminal_reason") or "unknown"
                    cli_error = cli_error or f"{ev.get('terminal_reason') or 'error'}: {err}"
                elif ev.get("result") and not texts:
                    texts.append(ev["result"])

    async def _read_stderr() -> None:
        assert proc.stderr
        while True:
            line_bytes = await asyncio.to_thread(proc.stderr.readline)
            if not line_bytes:
                break
            s = line_bytes.decode("utf-8", "replace").rstrip("\r\n")
            if s:
                stderr_lines.append(s)
                if len(stderr_lines) > 40:
                    stderr_lines.pop(0)

    t0 = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            await asyncio.gather(_read_stdout(), _read_stderr())
            await write_task
            exit_code = proc.returncode if proc.returncode is not None else (
                await asyncio.to_thread(proc.wait))
    except TimeoutError:
        _log(f"[qd] timeout after {timeout_s}s, killing pid={proc.pid}")
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            pass
        raise HTTPException(504, f"Qoder CLI 超时（>{timeout_s}s），已终止子进程") from None
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise

    elapsed = time.monotonic() - t0
    _log(f"[qd] done code={exit_code} elapsed={elapsed:.2f}s "
         f"chars={sum(len(x) for x in texts)} err={cli_error[:80]!r}")

    if cli_error:
        low = cli_error.lower()
        meta = station_meta()
        if any(k in low for k in ("auth", "login", "token", "credential", "unauthorized")):
            raise HTTPException(
                401,
                f"Qoder {meta['label']} 未登录或凭据无效。"
                f"请打开 Qoder {meta['label']} 桌面端登录一次（网关会自动复用其登录态）；"
                f"也可在本面板手填 Personal Access Token。原始信息：{cli_error[:300]}")
        raise HTTPException(502, f"Qoder CLI 报错：{cli_error[:300]}")

    text = "\n".join(texts)
    if not text:
        blob = "\n".join(raw_lines[-6:] or stderr_lines[-6:])
        low = blob.lower()
        if "not logged in" in low or "personal_access_token" in low or \
                ("auth" in low and "rejected" in low):
            meta = station_meta()
            raise HTTPException(
                401,
                f"Qoder {meta['label']} 未登录。请打开 Qoder {meta['label']} 桌面端登录一次，"
                f"或在本面板手填 Personal Access Token。原始输出：{blob[:300]}")
        raise HTTPException(502, f"Qoder CLI 无输出（exit={exit_code}）。"
                                 f"原始输出：{blob[:400] or '（空）'}")
    return text, usage, stderr_lines


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------

def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def create_app() -> FastAPI:
    app = FastAPI(title="qoder2openai")

    def _check_auth(req: Request) -> None:
        key = (CONFIG.get("api_key") or "").strip()
        if not key:
            return
        hdr = req.headers.get("authorization") or ""
        if not hdr.startswith("Bearer "):
            raise HTTPException(401, "缺少 Authorization: Bearer ***")
        if hdr.split(None, 1)[1].strip() != key:
            raise HTTPException(401, "API key 不匹配")

    @app.get("/health")
    async def _health():
        h = await asyncio.to_thread(cli_health)
        return {"status": "ok" if (h.get("cli_found") and h.get("credential_ok"))
                else "degraded",
                "station": h.get("station"), "label": h.get("label"),
                "cli_found": h.get("cli_found"),
                "cli_source": h.get("cli_source", ""),
                "ide_found": h.get("ide_found"),
                "account": h.get("account", ""),
                "pat_set": h.get("pat_set"),
                "credential_ok": h.get("credential_ok"),
                "node_version": h.get("node_version", ""),
                "error": h.get("error", "")}

    @app.get("/v1/health")
    async def _health_v1():
        return await _health()

    @app.get("/v1/models")
    async def _models():
        st = CONFIG.get("station", "intl")
        ids = list(STATIONS.get(st, STATIONS["intl"])["models"])
        return {"object": "list",
                "data": [{"id": i, "object": "model", "created": 0,
                          "owned_by": "qoder"} for i in ids]}

    @app.post("/v1/chat/completions")
    async def _chat(req: Request):
        _check_auth(req)
        body = await req.json()
        model = body.get("model") or "Auto"
        messages = body.get("messages") or []
        if not messages:
            raise HTTPException(400, "messages 为空")
        stream = bool(body.get("stream"))
        st = CONFIG.get("station", "intl")
        norm_model = _normalize_model(model, st)
        prompt_json = json.dumps(openai_to_stream_json(messages), ensure_ascii=False)

        def _err_chunk(cid: str, detail: str, code: int = 0) -> str:
            return _sse({"id": cid, "object": "chat.completion.chunk",
                         "created": int(time.time()), "model": model,
                         "choices": [{"index": 0,
                                      "delta": {"content": f"\n\n[错误{' ' + str(code) if code else ''}] {detail}"},
                                      "finish_reason": "error"}]})

        async def _gen() -> AsyncIterator[str]:
            cid = f"chatcmpl-{int(time.time() * 1000)}"
            yield _sse({"id": cid, "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": model,
                        "choices": [{"index": 0,
                                     "delta": {"role": "assistant", "content": ""},
                                     "finish_reason": None}]})
            try:
                text, usage, _ = await _run_cli_once(norm_model, prompt_json)
            except HTTPException as e:
                yield _err_chunk(cid, str(e.detail), e.status_code)
                yield "data: [DONE]\n\n"
                return
            except Exception as e:  # noqa: BLE001
                yield _err_chunk(cid, str(e))
                yield "data: [DONE]\n\n"
                return
            for i in range(0, len(text), 40):
                yield _sse({"id": cid, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model,
                            "choices": [{"index": 0,
                                         "delta": {"content": text[i:i + 40]},
                                         "finish_reason": None}]})
            yield _sse({"id": cid, "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": model,
                        "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "stop"}],
                        "usage": usage or {"input_tokens": 0, "output_tokens": 0}})
            yield "data: [DONE]\n\n"

        if stream:
            return StreamingResponse(_gen(), media_type="text/event-stream")

        text, usage, _ = await _run_cli_once(norm_model, prompt_json)
        return JSONResponse({
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": usage or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        })

    return app


# --------------------------------------------------------------------------
# GUI 辅助
# --------------------------------------------------------------------------

async def probe(log_fn: Callable[[str], None] | None = None) -> dict[str, Any]:
    """连通性自检：跑 CLI 的 --list-models（**零模型额度**），只验鉴权。

    返回 {ok, text, usage, error, elapsed, models}。
    """
    t0 = time.monotonic()
    try:
        cmd, prefix = _find_cli()
        env = await asyncio.to_thread(build_env)
        full = list(prefix) + ["--list-models"]

        def _run():
            return subprocess.run(full, capture_output=True, env=env, timeout=90,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        r = await asyncio.to_thread(_run)
        out = (r.stdout.decode("utf-8", "replace") + "\n"
               + r.stderr.decode("utf-8", "replace")).strip()
        models = [ln.strip() for ln in out.splitlines()
                  if ln.strip() and not ln.strip().startswith("MODEL")]
        ok = r.returncode == 0 and bool(models)
        return {"ok": ok, "text": out[:600],
                "models": models if ok else [],
                "usage": {}, "error": "" if ok else out[:400],
                "elapsed": time.monotonic() - t0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "models": [],
                "usage": {}, "error": f"{type(e).__name__}: {e}",
                "elapsed": time.monotonic() - t0}


def list_models_live(station: str | None = None) -> dict[str, Any]:
    """实时拉模型列表（--list-models，零额度）。同步版，供 GUI 调用。"""
    st = station or CONFIG.get("station", "intl")
    saved = CONFIG.get("station")
    try:
        CONFIG["station"] = st
        cmd, prefix = _find_cli()
        env = build_env()
        r = subprocess.run(list(prefix) + ["--list-models"], capture_output=True,
                           env=env, timeout=90,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout.decode("utf-8", "replace") + "\n"
               + r.stderr.decode("utf-8", "replace")).strip()
        models = [ln.strip() for ln in out.splitlines()
                  if ln.strip() and not ln.strip().startswith("MODEL")]
        return {"ok": r.returncode == 0 and bool(models), "models": models,
                "fallback": list(STATIONS[st]["models"]), "raw": out[:400]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "models": [], "fallback": list(STATIONS[st]["models"]),
                "raw": f"{type(e).__name__}: {e}"}
    finally:
        CONFIG["station"] = saved


def install_cli_hint(station: str | None = None) -> dict[str, Any]:
    st = station or CONFIG.get("station", "intl")
    meta = STATIONS[st]
    node = shutil.which("node")
    return {"station": st, "label": meta["label"], "npm_pkg": meta["npm_pkg"],
            "cli_bin": meta["cli_bin"], "config_dir": meta["config_dir"],
            "pat_env": meta["pat_env"], "job_env": meta["job_env"],
            "install_cmd": f"npm install -g {meta['npm_pkg']}",
            "node_found": bool(node), "node_path": node or ""}


def find_ide_install(station: str) -> str:
    """自动搜索本机 Qoder IDE 安装位置（返回 exe 路径，找不到返回 ""）。"""
    meta = STATIONS.get(station, STATIONS["intl"])
    exe = meta["ide_exe"]
    for d in meta.get("ide_dirs", []):
        if not d:
            continue
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return ""
