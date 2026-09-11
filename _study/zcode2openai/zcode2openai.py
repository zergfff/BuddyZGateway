# -*- coding: utf-8 -*-
"""zcode2openai — 把 ZCode（z.ai / BigModel）封装成标准 OpenAI 兼容 API。

参考 github.com/TriDefender/zcode-api 的思路：单一凭证（不再使用账号池），
对外暴露 OpenAI 兼容端点，内部把 OpenAI 请求翻译为 Anthropic Messages 协议，
转发到 ZCode 上游，再把 Anthropic 响应翻译回 OpenAI 格式。

凭证（二选一，单例）：
  - 编码计划 API Key（推荐，零额外依赖）：ZC_API_KEY=sk_xxx
        → 上游 https://api.z.ai/api/anthropic/v1/messages   （x-api-key 鉴权）
  - ZCode 桌面端会话 JWT（start-plan）：ZC_JWT=eyJ...
        → 上游 https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages  （Bearer 鉴权）
        → 该上游需要人机校验，会自动调用内嵌的 captcha_node 求解（需本机 Node.js）

其余：
  - ZC_PROVIDER: zai(默认) | bigmodel   （仅影响 api_key 模式的下游地址）
  - 可选 local_api_key：要求客户端携带 Authorization: Bearer <local_api_key>
  - 可选 ZC_MODELS：逗号分隔的暴露模型白名单（默认见 DEFAULT_MODELS）

依赖：fastapi + uvicorn + httpx（+ 仅 JWT 模式需要 Node.js + captcha_node）
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CONFIG = {
    "provider": "zai",          # zai | bigmodel （仅 api_key 模式使用）
    "api_key": "",              # 编码计划 API Key（coding-plan，免验证码）
    "jwt": "",                  # ZCode 桌面端会话 JWT（start-plan，需验证码）
    "local_api_key": "",        # 可选：本地客户端鉴权（不设置则不校验）
    "log_path": None,
    "exposed_models": [],       # 暴露模型白名单（空 = 默认全集）
}

# 模型大小写敏感映射（ZCode 上游需要 GLM-5.3 这种大小写）
MODEL_NAME_MAP = {
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}

# 上游地址
UPSTREAM = {
    "zai_jwt": "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
    "zai_key": "https://api.z.ai/api/anthropic/v1/messages",
    "bigmodel_key": "https://open.bigmodel.cn/api/anthropic/v1/messages",
}

USER_AGENT = "ZCode/3.0.1"
APP_VERSION = "1.0.0"

# /v1/models 默认暴露的模型
DEFAULT_MODELS = ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-5-Turbo", "GLM-5.1", "GLM-4.7"]


# ---------------------------------------------------------------------------
# 轻量日志
# ---------------------------------------------------------------------------

def _log(line: str):
    lp = CONFIG.get("log_path")
    if not lp:
        return
    try:
        stamp = time.strftime("%H:%M:%S")
        with open(lp, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 凭证解析
# ---------------------------------------------------------------------------

def detect_desktop_jwt() -> str | None:
    """自动扫描 ZCode 桌面端的 Local Storage，提取登录 JWT（start-plan）。

    ZCode 桌面端基于 Electron，登录凭证存储在：
        %APPDATA%/ZCode/session/Partitions/zcode-coding-plan/Local Storage/leveldb/*.log
    返回第一个有效的 JWT（三段式，含 signature），未找到则返回 None。
    """
    import re
    import glob

    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    base = Path(appdata) / "ZCode" / "session" / "Partitions" / "zcode-coding-plan" / "Local Storage" / "leveldb"
    if not base.is_dir():
        return None

    # JWT 正则：Header.Payload.Signature（每段 base64url 字符）
    jwt_re = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

    # 按修改时间倒序，优先读最新日志
    logs = sorted(base.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for logf in logs:
        try:
            raw = logf.read_bytes()
            # 从二进制中直接搜索 ASCII JWT 模式
            for m in jwt_re.finditer(raw.decode("utf-8", "ignore")):
                candidate = m.group(0)
                # 基础校验：三段式且总长度合理（JWT 通常 >100 字符）
                if candidate.count(".") == 2 and len(candidate) > 100:
                    return candidate
        except Exception:
            continue
    return None


def auto_configure(log=print):
    """解析凭证：环境变量优先，否则尝试自动检测桌面端 JWT，最后保持空。"""
    p = os.environ.get("ZC_PROVIDER", "").strip()
    if p:
        CONFIG["provider"] = p
    ak = os.environ.get("ZC_API_KEY", "").strip()
    if ak:
        CONFIG["api_key"] = ak
    jwt = os.environ.get("ZC_JWT", "").strip()
    if jwt:
        CONFIG["jwt"] = jwt
    # 若环境变量未提供 JWT，尝试自动检测 ZCode 桌面端
    if not CONFIG["jwt"] and not CONFIG["api_key"]:
        detected = detect_desktop_jwt()
        if detected:
            CONFIG["jwt"] = detected
            log(f"[zc] 已自动检测到 ZCode 桌面端登录凭证（JWT 长度 {len(detected)}）")
    env_models = [m.strip() for m in os.environ.get("ZC_MODELS", "").split(",") if m.strip()]
    if env_models:
        CONFIG["exposed_models"] = env_models
    mode = "jwt" if CONFIG["jwt"] else ("apikey" if CONFIG["api_key"] else "none")
    if mode == "none":
        log("[zc] [!] 未配置凭证：请设置 ZC_API_KEY（推荐）或 ZC_JWT，或在 GUI 面板填入")
    else:
        log(f"[zc] 凭证模式：{mode}  provider={CONFIG['provider']}")


def _credential():
    """返回 (url, auth_headers, mode)。mode ∈ {jwt, key, none}。"""
    jwt = (CONFIG.get("jwt") or "").strip()
    api_key = (CONFIG.get("api_key") or "").strip()
    provider = (CONFIG.get("provider") or "zai").strip().lower()
    if jwt:
        return UPSTREAM["zai_jwt"], {"Authorization": f"Bearer {jwt}"}, "jwt"
    if api_key:
        url = UPSTREAM["bigmodel_key"] if provider == "bigmodel" else UPSTREAM["zai_key"]
        return url, {"x-api-key": api_key}, "key"
    return None, None, "none"


def _base_headers(auth: dict) -> dict:
    return {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "User-Agent": USER_AGENT,
        "X-ZCode-App-Version": "3.0.1",
        "X-ZCode-Agent": "glm",
        "HTTP-Referer": "https://zcode.z.ai/",
        **auth,
    }


# ---------------------------------------------------------------------------
# 验证码求解（仅 JWT / start-plan 模式需要；复用内嵌 captcha_node）
# ---------------------------------------------------------------------------

_CAP_HEADERS = {
    "User-Agent": USER_AGENT,
    "X-ZCode-App-Version": "3.0.1",
    "X-ZCode-Agent": "glm",
    "HTTP-Referer": "https://zcode.z.ai/",
    "Origin": "https://zcode.z.ai",
    "Accept": "application/json, text/plain, */*",
}


class _CaptchaSolver:
    def __init__(self) -> None:
        self._cache: str | None = None
        self._cache_at: float = 0.0
        self._lock = asyncio.Lock()
        self._cfg_cache: dict | None = None
        self._cfg_at: float = 0.0

    @staticmethod
    def _solver_path() -> Path:
        return Path(__file__).resolve().parent / "captcha_node" / "solver.js"

    @staticmethod
    def _node() -> str | None:
        for c in (shutil.which("node"),
                  r"C:\Program Files\nodejs\node.exe",
                  r"C:\Program Files (x86)\nodejs\node.exe"):
            if c and Path(c).is_file():
                return c
        return None

    async def _fetch_config(self) -> dict:
        now = time.time() * 1000
        if self._cfg_cache and now - self._cfg_at < 600_000:
            return self._cfg_cache
        try:
            async with httpx.AsyncClient(timeout=15, headers=_CAP_HEADERS) as cl:
                rel = await cl.get("https://zcode.z.ai/api/v1/releases/latest")
                rel.raise_for_status()
                version = (rel.json().get("version") or "").strip()
                res = await cl.get(
                    "https://zcode.z.ai/api/v1/client/configs",
                    params={"os": "win32", "config_version": version},
                )
                res.raise_for_status()
                cap = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
                if cap and cap.get("sceneId"):
                    self._cfg_cache = cap
                    self._cfg_at = now
                    return cap
        except Exception:
            pass
        # 兜底：region 当前真实值为 cn
        return {"enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd"}

    async def get_verify_param(self) -> str:
        now = time.time() * 1000
        if self._cache and now - self._cache_at < 45_000:
            return self._cache
        async with self._lock:
            if self._cache and time.time() * 1000 - self._cache_at < 45_000:
                return self._cache
            cfg = await self._fetch_config()
            node = self._node()
            sp = self._solver_path()
            if not node or not sp.is_file():
                raise RuntimeError(
                    "JWT 模式需要 Node.js 与 captcha_node 求解器；请改用 ZC_API_KEY（编码计划，免验证码）")
            proc = await asyncio.create_subprocess_exec(
                node, str(sp),
                cfg.get("sceneId", "11xygtvd"),
                cfg.get("region", "cn"),
                cfg.get("prefix", "no8xfe"),
                cwd=str(sp.parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=40)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                raise RuntimeError("验证码求解超时")
            param = None
            for line in out.decode("utf-8", "ignore").splitlines():
                if line.startswith("VERIFY_PARAM="):
                    param = line[len("VERIFY_PARAM="):].strip()
            if not param:
                raise RuntimeError("验证码求解无结果（请确认已安装 Node.js 并点击「修复验证码依赖」）")
            self._cache = param
            self._cache_at = time.time() * 1000
            return param


_captcha = _CaptchaSolver()


# ---------------------------------------------------------------------------
# 请求 / 响应翻译（OpenAI <-> Anthropic）
# ---------------------------------------------------------------------------

def normalize_model(m):
    if not isinstance(m, str):
        return "GLM-5.3"
    s = m.strip()
    if "/" in s:                       # 去掉 provider 前缀，如 bigmodel/glm-5.3
        s = s.split("/", 1)[1]
    return MODEL_NAME_MAP.get(s.lower(), s)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _to_anthropic_content(content):
    """OpenAI content（str 或 parts 数组） → Anthropic content。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = []
        for p in content:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text":
                blocks.append({"type": "text", "text": p.get("text", "")})
            elif t == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
                if url:
                    # 尽力而为：Anthropic 支持 source.type=url
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        return blocks if blocks else ""
    return content


def openai_to_anthropic(body: dict) -> dict:
    model = normalize_model(body.get("model", ""))
    sys_parts = []
    messages = []
    for m in body.get("messages", []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            txt = content if isinstance(content, str) else _text_of(content)
            if txt:
                sys_parts.append(txt)
        elif role in ("user", "assistant"):
            anth_content = _to_anthropic_content(content)
            if anth_content != "":
                messages.append({"role": role, "content": anth_content})
        # tool / function 角色：尽力而为，主用例是纯文本对话

    anth = {
        "model": model,
        "messages": messages,
        "max_tokens": int(body.get("max_tokens") or 4096),
        "stream": bool(body.get("stream", False)),
    }
    if sys_parts:
        anth["system"] = "\n\n".join(sys_parts)
    for k in ("temperature", "top_p"):
        if k in body and body[k] is not None:
            anth[k] = body[k]
    if body.get("stop"):
        anth["stop_sequences"] = (body["stop"] if isinstance(body["stop"], list)
                                  else [body["stop"]])
    return anth


def _map_finish(reason):
    return {"end_turn": "stop", "stop_sequence": "stop",
            "max_tokens": "length", "tool_use": "tool_calls"}.get(reason, "stop")


def anthropic_to_openai(anth: dict) -> dict:
    content = ""
    for block in anth.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            content += block.get("text", "")
    usage = anth.get("usage", {}) or {}
    return {
        "id": anth.get("id") or ("chatcmpl-" + secrets.token_hex(4)),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": anth.get("model", ""),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": _map_finish(anth.get("stop_reason")),
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


async def translate_stream(resp):
    """把 Anthropic SSE 流翻译为 OpenAI SSE 流。"""
    buf: list[str] = []
    created = int(time.time())
    cid = [None]
    model = [None]
    finish_reason = [None]
    usage_in = [0]
    usage_out = [0]

    def chunk(obj) -> bytes:
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")

    def handle(ev, ds):
        out = []
        try:
            d = json.loads(ds)
        except Exception:
            return out
        if ev == "message_start":
            msg = d.get("message", {})
            cid[0] = msg.get("id")
            model[0] = msg.get("model")
            usage_in[0] = (msg.get("usage") or {}).get("input_tokens", 0)
            out.append(chunk({
                "id": cid[0] or ("chatcmpl-" + secrets.token_hex(4)),
                "object": "chat.completion.chunk", "created": created,
                "model": model[0] or "",
                "choices": [{"index": 0, "delta": {"role": "assistant"},
                             "finish_reason": None}],
            }))
        elif ev == "content_block_delta":
            delta = d.get("delta", {})
            if delta.get("type") == "text_delta":
                out.append(chunk({
                    "id": cid[0] or "", "object": "chat.completion.chunk",
                    "created": created, "model": model[0] or "",
                    "choices": [{"index": 0,
                                 "delta": {"content": delta.get("text", "")},
                                 "finish_reason": None}],
                }))
        elif ev == "message_delta":
            finish_reason[0] = _map_finish(d.get("delta", {}).get("stop_reason"))
            usage_out[0] = (d.get("usage") or {}).get("output_tokens", 0)
        elif ev == "message_stop":
            out.append(chunk({
                "id": cid[0] or "", "object": "chat.completion.chunk",
                "created": created, "model": model[0] or "",
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": finish_reason[0] or "stop"}],
                "usage": {
                    "prompt_tokens": usage_in[0],
                    "completion_tokens": usage_out[0],
                    "total_tokens": usage_in[0] + usage_out[0],
                },
            }))
        return out

    def split_sse(blk):
        ev = None
        ds = None
        for ln in blk.split("\n"):
            if ln.startswith("event:"):
                ev = ln[6:].strip()
            elif ln.startswith("data:"):
                ds = ln[5:].strip()
        return ev, ds

    async for line in resp.aiter_lines():
        if line == "":
            if buf:
                blk = "\n".join(buf)
                buf.clear()
                ev, ds = split_sse(blk)
                if ev and ds:
                    for b in handle(ev, ds):
                        yield b
        else:
            buf.append(line)
    if buf:
        blk = "\n".join(buf)
        ev, ds = split_sse(blk)
        if ev and ds:
            for b in handle(ev, ds):
                yield b
    yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="ZCode→OpenAI")


def _exposed():
    return list(CONFIG["exposed_models"]) if CONFIG["exposed_models"] else list(DEFAULT_MODELS)


def _check_local_auth(req: Request) -> bool:
    key = CONFIG.get("local_api_key") or ""
    if not key:
        return True
    auth = req.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == key


@app.get("/health")
async def health():
    url, _, mode = _credential()
    return {
        "ok": True,
        "service": "zcode2openai",
        "configured": mode != "none",
        "credential_mode": mode,
        "provider": CONFIG["provider"],
        "models": _exposed(),
    }


@app.get("/v1/models")
async def models(req: Request):
    if not _check_local_auth(req):
        return JSONResponse(
            {"error": {"message": "invalid API key", "type": "auth_error"}},
            status_code=401,
        )
    data = [{
        "id": m, "object": "model", "created": 0,
        "owned_by": "zcode", "_allow_reference": True,
    } for m in _exposed()]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not _check_local_auth(req):
        return JSONResponse(
            {"error": {"message": "invalid API key", "type": "auth_error"}},
            status_code=401,
        )
    url, auth, mode = _credential()
    if mode == "none":
        return JSONResponse(
            {"error": {"message": "ZCode 未配置凭证（请在 GUI 填入 ZC API Key 或 JWT，"
                                  "或用环境变量 ZC_API_KEY / ZC_JWT）",
                       "type": "config_error"}},
            status_code=503,
        )

    raw = await req.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        return JSONResponse(
            {"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}},
            status_code=400,
        )

    anth = openai_to_anthropic(body)
    stream = bool(body.get("stream", False))
    headers = _base_headers(auth)

    if mode == "jwt":
        try:
            vp = await _captcha.get_verify_param()
            headers["X-Aliyun-Captcha-Verify-Param"] = vp
        except Exception as e:  # noqa: BLE001
            _log(f"[zc] 验证码求解失败: {e}")
            return JSONResponse(
                {"error": {"message": f"JWT 模式需要人机校验且求解失败：{e}。"
                                      f"建议改用 ZC_API_KEY（编码计划，免验证码）。",
                           "type": "captcha_error"}},
                status_code=400,
            )

    payload = json.dumps(anth, ensure_ascii=False).encode("utf-8")
    _log(f"[zc] → {anth.get('model')} stream={stream} mode={mode}")

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))

    if stream:
        async def gen():
            try:
                async with client.stream("POST", url, headers=headers,
                                         content=payload) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "ignore")
                        try:
                            err = json.loads(text)
                        except Exception:
                            err = {"error": {"message": text[:800],
                                            "type": "upstream_error"}}
                        yield ("data: " + json.dumps(err, ensure_ascii=False)
                               + "\n\n").encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
                    async for b in translate_stream(resp):
                        yield b
            except Exception as e:  # noqa: BLE001
                _log(f"[zc] 流传输中断: {e}")
                yield ("data: " + json.dumps({"error": {"message": f"upstream error: {e}",
                                                         "type": "upstream_error"}})
                       + "\n\n").encode("utf-8")
                yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        resp = await client.post(url, headers=headers, content=payload)
    except httpx.HTTPError as e:
        await client.aclose()
        return JSONResponse(
            {"error": {"message": f"upstream error: {e}", "type": "upstream_error"}},
            status_code=502,
        )

    if resp.status_code >= 400:
        text = resp.text
        await client.aclose()
        try:
            err = resp.json()
        except Exception:
            err = {"error": {"message": text[:800], "type": "upstream_error"}}
        return JSONResponse(err, status_code=resp.status_code)

    try:
        anth_resp = resp.json()
    except Exception:
        await client.aclose()
        return JSONResponse(
            {"error": {"message": resp.text[:800], "type": "upstream_error"}},
            status_code=502,
        )
    await client.aclose()
    return JSONResponse(anthropic_to_openai(anth_resp))


if __name__ == "__main__":
    import uvicorn
    auto_configure()
    uvicorn.run(app, host="127.0.0.1", port=3000, log_level="info")
