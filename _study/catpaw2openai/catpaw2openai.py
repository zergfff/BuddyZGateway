"""catpaw2openai: proxy 美团 CatPaw agent upstream as OpenAI-compatible API.

Reads auth from ~/.meituan-catpaw/auth.json (auth.accessToken, desktop login required).
Flow per request: round -> event(running) -> turn(SSE) on
https://ai.catpaw.meituan.com, mapped to OpenAI shape.
Upstream turn SSE frames carry FULL text snapshots; we diff them into deltas.

- GET  /v1/models            -> [{"id": "catpaw", ...}]
- POST /v1/chat/completions  -> upstream round/event/turn (stream + non-stream)
"""
from __future__ import annotations
import argparse, json, time, uuid
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

CONFIG: dict[str, Any] = {
    "api_base": "https://ai.catpaw.meituan.com",
    "auth_file": str(Path.home() / ".meituan-catpaw" / "auth.json"),
    "model_type": 77,
    "source": "CatX",
    "mode": "CLI",
    "tool_version": "2.0.2",
    "model_id": "LongCat-2.0",
    "exposed_models": [],
    "log_path": None,
    "timeout": 180,
}
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

def _get_token() -> str:
    p = Path(CONFIG["auth_file"])
    if not p.is_file():
        raise RuntimeError(f"未找到 CatPaw auth.json：{p}（请先用 CatPaw 桌面端登录一次）")
    d = json.loads(p.read_text(encoding="utf-8"))
    tok = (d.get("auth") or {}).get("accessToken") or d.get("accessToken") or ""
    if not tok:
        raise RuntimeError(f"CatPaw auth.json 内无 accessToken：{p}")
    return tok

def _headers(tok: str, sse: bool = False) -> dict:
    h = {"X-Passport-Token": tok, "Content-Type": "application/json"}
    if sse:
        h.update({"Accept": "text/event-stream", "Cache-Control": "no-cache"})
    return h

def _messages_to_prompt(messages: list[dict]) -> str:
    """OpenAI messages -> 单条 prompt：system 作前缀，history 拼上下文，最后一条 user 为主。"""
    sys_parts, turns = [], []
    for m in messages or []:
        role, content = m.get("role", "user"), m.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = str(content)
        if role == "system":
            sys_parts.append(content)
        elif role == "assistant":
            turns.append(f"助手：{content}")
        else:
            turns.append(f"用户：{content}")
    head = ("【系统设定】\n" + "\n".join(sys_parts) + "\n\n") if sys_parts else ""
    return head + "\n".join(turns) if turns else head

def _round(client: httpx.Client, base: str, headers: dict, cid: str, prompt: str) -> None:
    body = {"conversationId": cid,
            "message": {"type": "user", "messageId": str(uuid.uuid4()),
                        "content": [{"type": "text", "text": prompt}]},
            "modelType": CONFIG["model_type"], "source": CONFIG["source"], "mode": CONFIG["mode"]}
    r = client.post(base + "/api/agent/conversation/round", json=body, headers=headers)
    try: d = r.json()
    except Exception: raise RuntimeError(f"round 非 JSON：{r.status_code} {r.text[:200]}")
    if d.get("code") != 0:
        raise RuntimeError(f"round 失败：{d.get('msg')}")

def _event_running(client: httpx.Client, base: str, headers: dict, cid: str) -> None:
    body = {"conversationId": cid, "eventType": "conversation", "data": {"status": "running"}}
    r = client.post(base + "/api/agent/conversation/event", json=body, headers=headers)
    try: d = r.json()
    except Exception: raise RuntimeError(f"event 非 JSON：{r.status_code} {r.text[:200]}")
    if d.get("code") != 0:
        raise RuntimeError(f"event 失败：{d.get('msg')}")

def _iter_turn_texts(client: httpx.Client, base: str, headers: dict, cid: str, prompt: str):
    """yield 上游 turn SSE 的全量 text 快照（最后 [DONE] 结束）。"""
    body = {"conversationId": cid, "turnRequestId": str(uuid.uuid4())[:32],
            "message": {"type": "user", "messageId": str(uuid.uuid4()),
                        "content": [{"type": "text", "text": prompt}]},
            "source": CONFIG["source"], "modelType": CONFIG["model_type"],
            "mode": CONFIG["mode"], "stream": True, "toolVersion": CONFIG["tool_version"]}
    with client.stream("POST", base + "/api/agent/conversation/turn",
                       json=body, headers=headers) as r:
        if r.status_code != 200:
            raise RuntimeError(f"turn HTTP {r.status_code}：{r.read().decode('utf-8','replace')[:300]}")
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try: d = json.loads(payload)
            except Exception: continue
            if isinstance(d, dict) and d.get("error"):
                raise RuntimeError(f"turn 报错：{(d['error'] or {}).get('message')}")
            msg = (d.get("message") or {}) if isinstance(d, dict) else {}
            for c in (msg.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text") is not None:
                    yield c["text"]

def _check_model(req_model: str | None) -> None:
    exp = CONFIG.get("exposed_models") or []
    if exp and req_model and req_model not in exp:
        raise HTTPException(status_code=404, detail=f"模型 {req_model} 未暴露（当前暴露：{exp}）")

def _to_openai(models: list[str]) -> list[dict]:
    ts = int(time.time())
    return [{"id": n, "object": "model", "created": ts, "owned_by": "catpaw"} for n in models]

def list_models() -> list[dict]:
    return _to_openai([CONFIG["model_id"]])

def _filter_exposed(models: list[dict]) -> list[dict]:
    exp = CONFIG.get("exposed_models") or []
    if not exp: return models
    return [m for m in models if m["id"] in set(exp)]

app = FastAPI(title="catpaw2openai", version="1.0.0")

@app.get("/")
def root():
    return {"service": "catpaw2openai", "upstream": CONFIG["api_base"], "version": "1.0.0"}

@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}

@app.get("/v1/models")
def models_route():
    return {"object": "list", "data": _filter_exposed(list_models())}

@app.get("/v1/balance")
def balance_route():
    try:
        tok = _get_token()
        return {"ok": True, "auth": True, "token_len": len(tok),
                "auth_path": str(CONFIG["auth_file"]),
                "note": "CatPaw 上游无公开余额查询接口"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try: body = await request.json()
    except Exception: raise HTTPException(status_code=400, detail="body 必须是 JSON")
    _check_model(body.get("model"))
    stream = bool(body.get("stream"))
    prompt = _messages_to_prompt(body.get("messages") or [])
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="messages 为空")
    base, timeout = CONFIG["api_base"], CONFIG["timeout"]
    try:
        tok = _get_token()
    except Exception as e:
        return JSONResponse({"error": str(e), "type": "auth_error"}, status_code=502)
    headers = _headers(tok)
    sse_headers = _headers(tok, sse=True)
    cid = str(uuid.uuid4())
    created = int(time.time())
    cmpl_id = "chatcmpl-" + cid[:8]
    model_id = CONFIG["model_id"]

    def run_turn() -> str:
        full = ""
        with httpx.Client(timeout=timeout) as client:
            _round(client, base, headers, cid, prompt)
            _event_running(client, base, headers, cid)
            for snapshot in _iter_turn_texts(client, base, sse_headers, cid, prompt):
                full = snapshot  # 上游是全量快照，取最后一份
        return full

    if not stream:
        try:
            full = await _run_in_thread(run_turn)
        except Exception as e:
            log(f"[catpaw] chat 上游异常：{e}")
            return JSONResponse({"error": str(e), "type": "upstream_error"}, status_code=502)
        return {"id": cmpl_id, "object": "chat.completion", "created": created, "model": model_id,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": full},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    def gen():
        sent = ""
        try:
            with httpx.Client(timeout=timeout) as client:
                _round(client, base, headers, cid, prompt)
                _event_running(client, base, headers, cid)
                for snapshot in _iter_turn_texts(client, base, sse_headers, cid, prompt):
                    delta = snapshot[len(sent):] if snapshot.startswith(sent) else snapshot
                    sent = snapshot
                    if delta:
                        yield "data: " + json.dumps(
                            {"id": cmpl_id, "object": "chat.completion.chunk", "created": created,
                             "model": model_id,
                             "choices": [{"index": 0, "delta": {"role": "assistant", "content": delta},
                                          "finish_reason": None}]}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e), "type": "upstream_error"}) + "\n\n"
        yield "data: " + json.dumps(
            {"id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": model_id,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})

import asyncio as _asyncio
async def _run_in_thread(fn):
    return await _asyncio.to_thread(fn)

def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9300)
    p.add_argument("--api-base", default=CONFIG["api_base"])
    p.add_argument("--auth-file", default=CONFIG["auth_file"])
    p.add_argument("--model-type", type=int, default=CONFIG["model_type"])
    p.add_argument("--expose", default="", help="逗号分隔的模型白名单；空=全部暴露")
    p.add_argument("--log", default=CONFIG["log_path"])
    p.add_argument("--timeout", type=int, default=CONFIG["timeout"])
    return p.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    CONFIG["api_base"] = args.api_base.rstrip("/")
    CONFIG["auth_file"] = args.auth_file
    CONFIG["model_type"] = args.model_type
    CONFIG["exposed_models"] = [s.strip() for s in args.expose.split(",") if s.strip()]
    CONFIG["log_path"] = args.log
    CONFIG["timeout"] = args.timeout
    try:
        tok = _get_token()
        log(f"[catpaw] auth 就绪 (token_len={len(tok)})")
    except Exception as e:
        log(f"[catpaw] 启动警告：{e}")
    log(f"[catpaw] 启动中 … http://{args.host}:{args.port}")
    log(f"[catpaw] 上游：{CONFIG['api_base']}  model_type={CONFIG['model_type']} 暴露模型：{CONFIG['exposed_models'] or '全部'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", lifespan="on")

if __name__ == "__main__":
    main()
