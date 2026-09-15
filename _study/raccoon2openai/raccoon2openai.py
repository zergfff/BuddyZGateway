"""raccoon2openai: proxy xiaohuanxiong upstream as OpenAI-compatible API.

Reads auth from ~/.box-agent/config/auth.json (access_token + refresh_token).
- GET  /v1/models   -> upstream GET /api/web/llm/v2/model_catalog, mapped to OpenAI shape
- POST /v1/chat/completions -> upstream /api/web/llm/v2/chat/completions (stream + non-stream)
- 401 on upstream triggers one-shot token refresh, then retry.
"""
from __future__ import annotations
import argparse, json, os, threading, time
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

CONFIG: dict[str, Any] = {
    "api_base": "https://xiaohuanxiong.com/api/web/llm/v2",
    "auth_dir": str(Path(os.environ.get("BOX_AGENT_CONFIG_DIR", str(Path.home() / ".box-agent" / "config")))),
    "exposed_models": [],
    "log_path": None,
    "timeout": 120,
    "local_api_key": "",     # 可选：本地客户端鉴权（不设置则不校验）
}
DEFAULT_MODELS = ["raccoon-8c4485", "sn-sensenova-6-8-flash-lite", "sn-glm-5-3", "sn-glm-5-3-flash", "sn-kimi-k3", "sn-deepseek-v4-pro"]
# 兜底名单 = 最后已知的线上模型名称：仅 catalog 拉取失败时使用，平时一律以上游 catalog 为准
_lock = threading.RLock()
_log_fh = None

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    p = CONFIG.get("log_path")
    if p:
        global _log_fh
        try:
            if _log_fh is None or getattr(_log_fh, "closed", False):
                _log_fh = open(p, "a", encoding="utf-8")
            _log_fh.write(line + "\n"); _log_fh.flush()
        except Exception: pass

def _auth_path() -> Path:
    return Path(CONFIG["auth_dir"]) / "auth.json"

def read_auth() -> dict:
    p = _auth_path()
    if not p.is_file():
        raise RuntimeError(f"未找到 auth.json：{p}（请先用桌面端登录一次）")
    d = json.loads(p.read_text(encoding="utf-8"))
    tok = d.get("access_token") or d.get("token") or d.get("auth_token")
    if not tok: raise RuntimeError(f"auth.json 内无 access_token：{p}")
    return d

def write_auth(data: dict) -> None:
    p = _auth_path(); p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: data.get(k) for k in ("access_token","refresh_token","office_identity","office_org_name","office_org_role") if data.get(k)}
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f: f.write(serialized)
    try: os.replace(tmp, p)
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise

def refresh_token_locked() -> dict | None:
    with _lock:
        try: d = read_auth()
        except Exception as e: log(f"[raccoon] refresh: 读 auth 失败 {e}"); return None
        rt = d.get("refresh_token")
        if not rt: return None
        # api_base 形如 https://host/api/web/llm/v2，而刷新端点是 https://host/api/web/auth/v1/refresh。
        # 原来用 rsplit("/v2",1)[0] 只砍掉了 "/v2"，留下 "/api/web/llm"，于是拼出
        # .../api/web/llm/api/web/auth/v1/refresh（/api/web 重复两次）→ 上游 404。
        # 正确做法是按 "/api/" 取源站前缀。
        _origin = CONFIG["api_base"].split("/api/", 1)[0]
        url = _origin + "/api/web/auth/v1/refresh"
        try:
            with httpx.Client(timeout=20) as c:
                r = c.post(url, json={"refresh_token": rt}, headers={"Content-Type": "application/json"})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log(f"[raccoon] refresh HTTP 失败：{e}"); return None
        if not isinstance(data, dict): return None
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        new_at = payload.get("access_token") or payload.get("accessToken") or payload.get("token")
        if not new_at:
            log(f"[raccoon] refresh 无新 token: {str(data)[:120]}"); return None
        d["access_token"] = new_at
        new_rt = payload.get("refresh_token") or payload.get("refreshToken")
        if new_rt: d["refresh_token"] = new_rt
        write_auth(d)
        log("[raccoon] access_token 已刷新并落盘")
        return d
    return None

def _get_token() -> str:
    return read_auth()["access_token"]

_catalog_cache: dict = {"ts": 0.0, "data": None}

def fetch_catalog(timeout=20) -> dict:
    now = time.time()
    with _lock:
        if _catalog_cache["data"] is not None and now - _catalog_cache["ts"] < 60:
            return _catalog_cache["data"]
    tok = _get_token()
    url = CONFIG["api_base"] + "/model_catalog"
    headers = {"Authorization": f"Bearer {tok}"}
    with httpx.Client(timeout=timeout) as c:
        r = c.get(url, headers=headers)
        if r.status_code == 401:
            if refresh_token_locked():
                headers["Authorization"] = f"Bearer {_get_token()}"
                r = c.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"model_catalog 返回异常：{data}")
    with _lock:
        _catalog_cache["ts"] = time.time(); _catalog_cache["data"] = data["data"]
    return data["data"]

def _to_openai(models: list[str]) -> list[dict]:
    ts = int(time.time())
    return [{"id": n, "object": "model", "created": ts, "owned_by": "raccoon"} for n in models]

def _display_of(m: dict) -> str:
    """catalog 显示名：description 优先（例 Raccoon-Work-260817-A），无则用内部名。"""
    return ((m.get("description") or m.get("name") or m.get("model_name")) or "").strip()

def _internal_of(m: dict) -> str:
    return ((m.get("name") or m.get("model_name")) or "").strip()

def catalog_name_map() -> dict:
    """显示名 -> 上游内部名（chat 时回写）。失败返回 {}。"""
    try: cat = fetch_catalog()
    except Exception: return {}
    out: dict[str, str] = {}
    for c in cat.get("categories", []):
        for m in c.get("models", []):
            internal = _internal_of(m)
            disp = _display_of(m) or internal
            if internal and disp and disp not in out:
                out[disp] = internal
    return out

def catalog_displays() -> list[str]:
    """按 catalog 顺序返回显示名；无 catalog 返回 []（调用方再用线上名兜底）。"""
    return list(catalog_name_map().keys())

def resolve_model(name: str | None) -> str | None:
    """显示名或内部名 -> 上游内部名；未知原样透传。"""
    if not name: return name
    try: return catalog_name_map().get(name, name)
    except Exception: return name

def list_models() -> list[dict]:
    try: cat = fetch_catalog()
    except Exception as e:
        log(f"[raccoon] 拉取 catalog 失败，用线上模型名称兜底：{e}")
        return _to_openai(DEFAULT_MODELS)
    out: list[str] = []
    for c in cat.get("categories", []):
        for m in c.get("models", []):
            n = _display_of(m) or _internal_of(m)
            if n and n not in out: out.append(n)
    if not out: out = list(DEFAULT_MODELS)
    return _to_openai(out)

def _filter_exposed(models: list[dict]) -> list[dict]:
    exp = CONFIG.get("exposed_models") or []
    if not exp: return models
    eset = set(exp)
    try: mp = catalog_name_map()  # 显示名 -> 内部名（兼容旧存名单里的内部名）
    except Exception: mp = {}
    return [m for m in models if m["id"] in eset or mp.get(m["id"]) in eset]

def _chat_prep(body: dict, stream: bool):
    """构造 (url, headers, payload)。**不做 IO**（401 重试由调用方处理）。"""
    tok = _get_token()
    url = CONFIG["api_base"] + "/chat/completions"
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
               "Accept": "text/event-stream" if stream else "application/json"}
    exp = CONFIG.get("exposed_models") or []
    req_model = body.get("model")
    internal = resolve_model(req_model) or req_model
    if exp and req_model and req_model not in exp and internal not in exp:
        raise HTTPException(status_code=404, detail=f"模型 {req_model} 未暴露（当前暴露：{exp}）")
    payload = dict(body)
    if internal:
        payload["model"] = internal  # 显示名回写为上游内部名
    return url, headers, payload


def _do_chat(body: dict, stream: bool, timeout: int):
    """非流式请求（同步）。**调用方必须放到线程里跑**，否则会卡住事件循环。"""
    url, headers, payload = _chat_prep(body, False)
    with httpx.Client(timeout=timeout, headers=headers) as c:
        r = c.post(url, json=payload)
        if r.status_code == 401:
            if refresh_token_locked():
                c.headers["Authorization"] = f"Bearer {_get_token()}"
                r = c.post(url, json=payload)
        return r.status_code, r

def _check_local_auth(req: Request) -> bool:
    """若设置了 local_api_key，校验客户端 Bearer；返回是否放行。"""
    key = CONFIG.get("local_api_key") or ""
    if not key:
        return True
    auth = req.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == key


def _unauthorized():
    return JSONResponse({"error": {"message": "invalid API key", "type": "auth_error"}},
                        status_code=401)


app = FastAPI(title="raccoon2openai", version="1.0.0")

@app.get("/")
def root():
    return {"service": "raccoon2openai", "upstream": CONFIG["api_base"], "version": "1.0.0"}

@app.get("/v1/models")
def models_route(request: Request):
    if not _check_local_auth(request):
        return _unauthorized()
    try: return {"object": "list", "data": _filter_exposed(list_models())}
    except Exception as e:
        log(f"[raccoon] /v1/models 失败：{e}")
        return {"object": "list", "data": _filter_exposed(_to_openai(DEFAULT_MODELS))}

@app.get("/v1/balance")
def balance_route(request: Request):
    if not _check_local_auth(request):
        return _unauthorized()
    try:
        tok = _get_token()
        return {"ok": True, "auth": True, "token_len": len(tok), "auth_path": str(_auth_path()),
                "note": "小浣熊上游无公开余额查询接口"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _check_local_auth(request):
        return _unauthorized()
    try: body = await request.json()
    except Exception: raise HTTPException(status_code=400, detail="body 必须是 JSON") from None
    stream = bool(body.get("stream"))
    import asyncio as _aio

    if stream:
        # 真正的增量透传。
        # 之前是「整体读完之后逐行吐出」——上游没开 stream=True，响应被 httpx
        # 全量缓冲，客户端在整段生成完之前收不到任何字节（看着像卡死）。
        url, headers, payload = _chat_prep(body, True)
        loop = _aio.get_running_loop()
        q: _aio.Queue = _aio.Queue()

        def _push(item):
            # asyncio.Queue 不是线程安全的，必须经 loop 线程安全投递
            loop.call_soon_threadsafe(q.put_nowait, item)

        def _worker():
            c = httpx.Client(timeout=CONFIG["timeout"], headers=headers)
            try:
                r = c.send(c.build_request("POST", url, json=payload), stream=True)
                if r.status_code == 401 and refresh_token_locked():
                    r.close()
                    h2 = dict(headers)
                    h2["Authorization"] = f"Bearer {_get_token()}"
                    r = c.send(c.build_request("POST", url, json=payload,
                                               headers=h2), stream=True)
                if r.status_code != 200:
                    _push({"err": f"upstream {r.status_code}: "
                                   f"{r.read().decode('utf-8', 'replace')[:400]}"})
                else:
                    for chunk in r.iter_bytes():
                        if chunk:
                            _push({"chunk": chunk})
            except Exception as e:  # noqa: BLE001
                log(f"[raccoon] 上游流式异常：{e}")
                _push({"err": str(e)})
            finally:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
                _push(None)

        async def gen():
            import threading as _th
            _th.Thread(target=_worker, daemon=True).start()
            while True:
                item = await q.get()
                if item is None:
                    break
                if "err" in item:
                    yield (b"data: " + json.dumps(
                        {"error": {"message": item["err"], "type": "upstream_error"}},
                        ensure_ascii=False).encode("utf-8") + b"\n\n")
                    break
                yield item["chunk"]
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "Connection": "keep-alive",
                                          "X-Accel-Buffering": "no"})

    try:
        # 同步 httpx 放线程：直接在 async 里跑会把事件循环卡住整段上游耗时
        status, r = await _aio.to_thread(_do_chat, body, False, CONFIG["timeout"])
    except HTTPException: raise
    except Exception as e:
        log(f"[raccoon] chat 上游异常：{e}")
        return JSONResponse({"error": str(e), "type": "upstream_error"}, status_code=502)
    if status == 200:
        try: data = r.json()
        except Exception as e:
            txt = r.text[:400]
            return JSONResponse({"error": f"upstream 非 JSON：{e}", "raw": txt}, status_code=502)
        return data
    try: err = r.text[:800]
    except Exception: err = ""
    try: j = json.loads(err)
    except Exception: j = {"error": err, "status": status}
    return JSONResponse(j, status_code=status)

@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}

def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9200)
    p.add_argument("--api-base", default=CONFIG["api_base"])
    p.add_argument("--auth-dir", default=CONFIG["auth_dir"])
    p.add_argument("--expose", default="", help="逗号分隔的模型白名单；空=全部暴露")
    p.add_argument("--log", default=CONFIG["log_path"])
    p.add_argument("--timeout", type=int, default=CONFIG["timeout"])
    return p.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    CONFIG["api_base"] = args.api_base.rstrip("/")
    CONFIG["auth_dir"] = args.auth_dir
    CONFIG["exposed_models"] = [s.strip() for s in args.expose.split(",") if s.strip()]
    CONFIG["log_path"] = args.log
    CONFIG["timeout"] = args.timeout
    try:
        d = read_auth()
        log(f"[raccoon] auth 就绪：{d.get('office_identity','unknown')} (token_len={len(d['access_token'])})")
    except Exception as e:
        log(f"[raccoon] 启动警告：{e}")
    log(f"[raccoon] 启动中 … http://{args.host}:{args.port}")
    log(f"[raccoon] 上游：{CONFIG['api_base']}  暴露模型：{CONFIG['exposed_models'] or '全部'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", lifespan="on")

if __name__ == "__main__":
    main()
