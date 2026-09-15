# -*- coding: utf-8 -*-
"""loomy2openai — 把 Loomy（讯飞 iModel / spark-x）封装成标准 OpenAI 兼容 API。

对外：
  GET  /health                探活（含凭证来源/模型数）
  GET  /v1/models             模型目录
  POST /v1/chat/completions   对话（SSE 流式 + 非流式；支持 tools/tool_calls）

对内：Loomy 桌面端是 Electron + 内嵌 opencode。它会把 provider 配置注入运行时
opencode 服务（默认 127.0.0.1:4431），`GET /provider` 能直接读到：

    providerID=imodel  options={baseURL, apiKey, useSessionAuth:true}

所以凭证优先级：① 运行中的 opencode /provider（最准，含最新 token）
                ② 解析 Loomy 的 Local Storage leveldb（loomy-auth-session.session）
                ③ 用户手填 base_url / api_key

鉴权注意（实测）：
  /chat/completions  →  Authorization: Bearer <apiKey>
  /models            →  token: <apiKey>          ← 两个端点不一样！
  tool_choice        →  只认字符串 auto/required；对象形式不触发（降级为 required）
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CONFIG = {
    "local_api_key": "",
    "log_path": None,
    "exposed_models": [],
    "port": 9400,
    "base_url": "",          # 留空=自动探测
    "api_key": "",           # 留空=自动探测
    "opencode_port": 4431,
}

DEFAULT_BASE = "https://loomyad.xunfei.cn/api/v1"
# 12 个模型；倍数来自 Loomy 界面标注（x12.0 表示扣 12 倍积分）
MODEL_HINTS = {
    "spark-x": "Spark X2.5（限时免费）",
    "qwen3.8-flash": "qwen 3.8 flash（x0.8）",
    "GLM-5.3-Flash": "GLM 5.3 Flash（x0.8）",
    "doubao-seed-2.0-mini": "Doubao Seed 2.0 mini（x0.8）",
    "qwen3.5-flash": "Qwen3.5 Flash（x1.0）",
    "deepseek-v4-flash-0731": "DeepSeek V4 Flash 0731（x3.0）",
    "mimo-v2.5": "MiMo V2.5（x3.3）",
    "MiniMax-M3": "MiniMax M3（x4.0）",
    "Kimi-k2.6": "Kimi k2.6（x6.5）",
    "qwen-3.8-max": "Qwen 3.8 Max（x12.0）",
    "doubao-seedream-5-lite": "doubao-seedream-5-lite",
    "qwen-image-3.0-pro": "qwen-image-3.0-pro",
}
FREE_MODELS = {"spark-x"}
# 默认暴露全部 12 个；spark-x 排在最前，作为默认选中项（唯一免费）
DEFAULT_MODELS = [
    "spark-x",
    "qwen3.8-flash",
    "GLM-5.3-Flash",
    "doubao-seed-2.0-mini",
    "qwen3.5-flash",
    "deepseek-v4-flash-0731",
    "mimo-v2.5",
    "MiniMax-M3",
    "Kimi-k2.6",
    "qwen-3.8-max",
    "doubao-seedream-5-lite",
    "qwen-image-3.0-pro",
]


def _log(line: str):
    lp = CONFIG.get("log_path")
    if not lp:
        return
    try:
        with open(lp, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 凭证探测
# ---------------------------------------------------------------------------

def _from_opencode() -> tuple[str, str, list] | None:
    """从运行中的 Loomy opencode 服务读 imodel provider（baseURL + apiKey + 模型表）。"""
    port = int(CONFIG.get("opencode_port") or 4431)
    try:
        with httpx.Client(timeout=8) as c:
            r = c.get(f"http://127.0.0.1:{port}/provider")
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:  # noqa: BLE001
        return None
    for p in (d.get("all") or []):
        if not isinstance(p, dict):
            continue
        opts = p.get("options") or {}
        if p.get("id") == "imodel" or opts.get("useSessionAuth"):
            base = str(opts.get("baseURL") or "").rstrip("/")
            key = str(opts.get("apiKey") or "")
            models = list((p.get("models") or {}).keys())
            if base and key:
                return base, key, models
    return None


def _loomy_roots() -> list:
    out = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        v = os.environ.get(env)
        if v:
            out.append(Path(v) / "loomy")
            out.append(Path(v) / "Loomy")
    return out


def _from_local_storage() -> tuple[str, str, list] | None:
    """解析 Loomy 的 Local Storage(leveldb) 里的 loomy-auth-session.session。"""
    for root in _loomy_roots():
        d = root / "Local Storage" / "leveldb"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.log")) + sorted(d.glob("*.ldb")):
            try:
                b = f.read_bytes()
            except OSError:
                continue
            txt = b.decode("utf-8", "replace")
            m = re.search(r'"session"\s*:\s*"([0-9a-zA-Z_\-]{8,})"', txt)
            if m:
                return DEFAULT_BASE, m.group(1), []
    return None


def resolve_creds(force: bool = False) -> tuple[str, str, list]:
    """返回 (base_url, api_key, models)。优先 CONFIG 手填 → opencode 实时 → Local Storage。"""
    if CONFIG.get("base_url") and CONFIG.get("api_key"):
        return CONFIG["base_url"].rstrip("/"), CONFIG["api_key"], []
    got = _from_opencode()
    if got:
        base, key, models = got
        _log(f"[loomy] 凭证来自 opencode 服务（{len(models)} 个模型）")
        return base, key, models
    got = _from_local_storage()
    if got:
        base, key, models = got
        _log("[loomy] 凭证来自 Local Storage（opencode 未运行）")
        return base, key, models
    raise RuntimeError("未找到 Loomy 登录凭证：请先打开 Loomy 桌面端登录一次")


def read_points() -> dict:
    """读 Loomy 的积分摘要缓存（Local Storage 的 `loomy-points-summary`）。

    实测：积分查询走 Electron 主进程 IPC（electronAPI.points.queryRecordsV2），
    真实 HTTP 端点未在渲染层暴露（app.asar 前 120MB 内未找到，候选路径均 404），
    所以以 Loomy 自己缓存的摘要为准——它由 App 每次刷新时写入。
    字段：balance（总余额）/ dailyBalance（当日余额）/ teamDaily*（团队日限额）。
    """
    import re as _re
    # leveldb 里「键」与「值」之间是二进制控制字节（实测 \xbc\x01\x01 / L\x01），
    # 不是空白，所以不能用 \s*，要按字节放宽容忍。
    pat = _re.compile(rb"loomy-points-summary.{0,8}?(\{[^{}]*\})", _re.S)
    best = None
    for root in _loomy_roots():
        d = root / "Local Storage" / "leveldb"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.log")) + sorted(d.glob("*.ldb")):
            try:
                raw = f.read_bytes()
            except OSError:
                continue
            for m in pat.finditer(raw):
                try:
                    obj = json.loads(m.group(1).decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    continue
                if "balance" not in obj:
                    continue
                if best is None or (obj.get("updatedAt") or "") > (best.get("updatedAt") or ""):
                    best = obj
    return best or {}


def _exposed() -> list:
    raw = list(CONFIG["exposed_models"]) if CONFIG["exposed_models"] else list(DEFAULT_MODELS)
    return raw


def _check_local_auth(req: Request) -> bool:
    key = CONFIG.get("local_api_key") or ""
    if not key:
        return True
    auth = req.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == key


def _tool_payload(tools, tool_choice) -> dict:
    """上游只认字符串 tool_choice；对象形式不触发，降级为 required。"""
    if not tools:
        return {}
    tc = tool_choice
    if isinstance(tc, str) and tc.lower() == "none":
        return {}
    out = {"tools": tools}
    if isinstance(tc, str) and tc.lower() in ("auto", "required"):
        out["tool_choice"] = tc.lower()
    elif isinstance(tc, dict):
        out["tool_choice"] = "required"
    return out


app = FastAPI(title="Loomy→OpenAI")


@app.get("/health")
async def health():
    ok, info, models = True, {}, []
    try:
        base, key, models = resolve_creds()
        info = {"base_url": base, "key_len": len(key)}
    except Exception as e:  # noqa: BLE001
        ok, info = False, {"error": str(e)[:200]}
    return {"ok": ok, "service": "loomy2openai", "configured": ok,
            "models": _exposed(), "credential": info}


@app.get("/v1/models")
async def models(req: Request):
    if not _check_local_auth(req):
        return JSONResponse({"error": {"message": "invalid API key", "type": "auth_error"}}, status_code=401)
    return {"object": "list",
            "data": [{"id": m, "object": "model", "created": 0, "owned_by": "loomy"} for m in _exposed()]}


@app.get("/v1/points")
async def points():
    """积分余额（读 Loomy 本地缓存摘要）。"""
    try:
        d = read_points()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if not d:
        return JSONResponse({"ok": False,
                             "error": "未找到积分缓存（请打开 Loomy 桌面端一次让它刷新）"},
                            status_code=404)
    return {"ok": True, "data": d,
            "note": "读自 Loomy 本地缓存；实时接口走 Electron 主进程 IPC，未公开"}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not _check_local_auth(req):
        return JSONResponse({"error": {"message": "invalid API key", "type": "auth_error"}}, status_code=401)
    try:
        body = json.loads(await req.body() or b"{}")
    except Exception:
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}},
                            status_code=400)
    msgs = body.get("messages") or []
    if not msgs:
        return JSONResponse({"error": {"message": "messages 为空", "type": "invalid_request"}}, status_code=400)
    model = body.get("model") or (_exposed()[0])
    if model not in MODEL_HINTS and not CONFIG.get("allow_any_model"):
        # 未在表内也放行（上游会自己报错），但记录一条
        _log(f"[loomy] 未在已知表内的模型：{model}")
    max_tokens = int(body.get("max_tokens") or 4096)
    stream = bool(body.get("stream", False))
    tools = body.get("tools") or None
    tool_choice = body.get("tool_choice")

    try:
        base, key, _ = resolve_creds()
    except RuntimeError as e:
        return JSONResponse({"error": {"message": str(e), "type": "config_error"}}, status_code=503)

    url = base + "/chat/completions"
    payload = {"model": model, "messages": msgs, "max_tokens": max_tokens, "stream": stream}
    payload.update(_tool_payload(tools, tool_choice))
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    if not stream:
        import asyncio as _aio

        def _sync_post():
            # 同步 httpx 必须放线程：直接在 async 里调用会把事件循环卡住
            # 整段上游耗时（timeout=900），其间该服务的所有其它请求都被堵。
            with httpx.Client(timeout=900) as c:
                rr = c.post(url, json=payload, headers=headers)
                txt = rr.text
            try:
                return rr.status_code, json.loads(txt)
            except Exception:  # noqa: BLE001
                return rr.status_code, {"error": {"message": txt[:500],
                                                  "type": "upstream_error"}}

        try:
            status, data = await _aio.to_thread(_sync_post)
            return JSONResponse(content=data, status_code=status)
        except httpx.HTTPError as e:
            return JSONResponse({"error": {"message": f"upstream error: {e}", "type": "upstream_error"}},
                                status_code=502)

    def gen():
        try:
            with httpx.Client(timeout=900) as c:
                with c.stream("POST", url, json=payload, headers=headers) as r:
                    if r.status_code != 200:
                        yield ("data: " + json.dumps(
                            {"error": {"message": r.read().decode('utf-8', 'replace')[:500],
                                       "type": "upstream_error"}}) + "\n\n").encode()
                        yield b"data: [DONE]\n\n"
                        return
                    for chunk in r.iter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as e:
            yield ("data: " + json.dumps(
                {"error": {"message": f"upstream error: {e}", "type": "upstream_error"}}) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(CONFIG["port"]), log_level="info")
