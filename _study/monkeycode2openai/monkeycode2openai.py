# -*- coding: utf-8 -*-
"""monkeycode2openai — 把 MonkeyCode 桌面端封装成标准 OpenAI 兼容 API。

对外暴露统一接口：

  GET  /health                探活（含当前 mode / 上游）
  GET  /v1/models             模型目录
  POST /v1/chat/completions   对话（SSE 流式 + 非流式）

上行有两种模式，由 auto_configure 自动选择：

  ① mode="ohmyagent"  —— 官方代理（优先）
     凭据：%APPDATA%\\com.chaitin.baizhi.monkeycode\\monkeycode-ohmyagent-key.json
     端点：https://proxy.monkeycode-ai.com/v1  (oma_ key + omas_ 签名密钥)
     协议：OpenAI **Responses** API（/responses），不是 chat/completions，
           且每个请求必须带签名头
             X-Ohmyagent-Signature: v1=HMAC-SHA256(instructions, signing_secret) 的 hex
           其中 instructions 就是请求体里的 instructions 字段原文（非空）。
           本模块负责把 chat/completions 请求翻译成 Responses 请求，再把
           Responses 结果/SSE 翻译回 chat.completions / chat.completion.chunk。
     这条通道用的是 MonkeyCode 账号额度（有每日 token 配额）。

  ② mode="static"     —— 静态 key 透传（回退）
     凭据：桌面端 config.json 的 models[] 里带 api_key 的条目，
           且优先 base_url 含 /openai 的（OpenAI 兼容端点）。
     协议：原生 chat/completions 原样转发。

环境变量覆盖：MC2_OPENAI_BASE_URL / MC2_OPENAI_API_KEY（设置后走 static）。
可选 local_api_key：要求客户端携带 `Authorization: Bearer *** 才能访问。

依赖：fastapi + uvicorn + httpx。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CONFIG = {
    "base_url": "",          # 上游端点（从 config.json / ohmyagent-key.json 读取）
    "api_key": "",           # 上游 key
    "local_api_key": "",     # 可选：本地客户端鉴权（不设置则不校验）
    "log_path": None,
    "exposed_models": [],    # 可选模型白名单（空 = 透传全部）
    "pool_enabled": False,   # 号池轮转（默认关）
    "pool_keys": [],         # 号池 key：[{base_url, api_key, enabled, label}]
    "mode": "static",        # "static"（透传）| "ohmyagent"（官方代理，需签名）
    "signing_secret": "",    # ohmyagent 模式的签名密钥（omas_…）
    "basic_prefix": "monkeycode-basic/",   # 免费基础模型的上游前缀
    "default_model": "",     # 未指定 model 时用
    "model_types": {},       # {上游模型名: "anthropic"|"openai-responses"}，决定走哪套协议
}


# ---------------------------------------------------------------------------
# 上游配置自动探测
# ---------------------------------------------------------------------------

def find_desktop_config() -> Path | None:
    """定位本机 MonkeyCode 桌面端 config.json。"""
    app = "com.chaitin.baizhi.monkeycode"
    home = Path.home()
    for base in (
        os.environ.get("APPDATA"),
        home / "AppData" / "Roaming",
        os.environ.get("LOCALAPPDATA"),
        home / "AppData" / "Local",
    ):
        if not base:
            continue
        p = Path(base) / app / "config.json"
        if p.is_file():
            return p
    return None


def _is_openai_base(url: str) -> bool:
    """base_url 是否为 OpenAI 兼容端点（本模块只讲 OpenAI 协议，必须走这类端点）。"""
    return "/openai" in (url or "").lower()


def _load_from_desktop():
    """从桌面端 config.json 提取 (base_url, api_key)。

    优先 OpenAI 兼容端点：config.json 的 models[] 同时含
    Anthropic（provider=anthropic → /api/anthropic）与
    OpenAI（/api/openai/v1）两类端点，而本模块只转发 OpenAI 协议的
    /chat/completions，选到 Anthropic 端点必然 404（上游回 "Gateway 路由不存在"）。

    不要退回"第一条带 key 的条目"的写法：桌面端改写 config.json（例如前面
    插入一批 base_url/api_key 为空的 monkeycode-basic/* 免费条目）会让这个
    位置漂移到 /api/anthropic。仅当没有任何 OpenAI 端点时才回退。
    """
    p = find_desktop_config()
    if not p:
        return None, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    keyed = [(m["base_url"].rstrip("/"), m["api_key"])
             for m in (data.get("models") or [])
             if m.get("api_key") and m.get("base_url")]
    if not keyed:
        return None, None
    for base, key in keyed:
        if _is_openai_base(base):
            return base, key
    return keyed[0]


def _basic_models() -> list[str]:
    """桌面端 config.json 里 monkeycode-basic/* 的免费基础模型（去前缀，保序）。"""
    p = find_desktop_config()
    if not p:
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for m in (data.get("models") or []):
        mid = (m.get("model") or "")
        if mid.startswith(CONFIG["basic_prefix"]):
            s = mid.split("/", 1)[1]
            if s and s not in out:
                out.append(s)
    return out


def find_ohmyagent_key() -> dict | None:
    """定位官方代理凭据 monkeycode-ohmyagent-key.json。

    形如：{"api_key": "oma_…", "base_url": "https://proxy.monkeycode-ai.com/v1",
           "signing_secret": "omas_…", ...}
    """
    app = "com.chaitin.baizhi.monkeycode"
    home = Path.home()
    for base in (
        os.environ.get("APPDATA"),
        home / "AppData" / "Roaming",
        os.environ.get("LOCALAPPDATA"),
        home / "AppData" / "Local",
    ):
        if not base:
            continue
        p = Path(base) / app / "monkeycode-ohmyagent-key.json"
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if d.get("api_key") and d.get("base_url") and d.get("signing_secret"):
            return d
        return None
    return None


def find_ohmyagent_settings() -> dict:
    """读官方代理的模型表 %APPDATA%\\com.chaitin.baizhi.monkeycode\\ohmyagent\\settings.json。"""
    app = "com.chaitin.baizhi.monkeycode"
    home = Path.home()
    for base in (
        os.environ.get("APPDATA"),
        home / "AppData" / "Roaming",
        os.environ.get("LOCALAPPDATA"),
        home / "AppData" / "Local",
    ):
        if not base:
            continue
        p = Path(base) / app / "ohmyagent" / "settings.json"
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def _load_model_protocols() -> dict:
    """{上游模型名: 协议类型}，取自桌面端模型表（type 字段）。

    官方代理**按模型名分流协议**：
      type=openai-responses → POST /responses
      type=anthropic        → POST /messages  (Anthropic Messages API)
    选错协议上游回 `404 路由不存在`（"not_found_error"），所以必须按这张表路由，
    不能一律走 /responses。
    """
    out: dict = {}
    for v in (find_ohmyagent_settings().get("models") or {}).values():
        if not isinstance(v, dict):
            continue
        if "proxy.monkeycode-ai.com" not in str(v.get("base_url") or ""):
            continue
        name, typ = v.get("model"), v.get("type")
        if name and typ:
            out[str(name)] = str(typ)
    return out


def _protocol_for(model: str) -> str:
    """上游模型名 → 用哪套协议（未知按 openai-responses 兜底）。"""
    t = (CONFIG.get("model_types") or {}).get(model, "openai-responses")
    return "anthropic" if t == "anthropic" else "responses"


def _sign_instructions(instructions: str) -> str:
    """官方代理要求的签名头值：v1=<HMAC-SHA256(<签名文本>, signing_secret) 的 hex>。

    签名文本就是请求体里那段“静态提示词”的**原文**，且不能为空：
      · /responses → body 的 `instructions` 字段
      · /messages  → body 的 `system` 字段
    服务端用同一密钥复算校验，所以并不要求用某个固定提示词——任意非空文本都能过；
    签错文本、签名缺失或文本为空都会得到 `403 {"error":"invalid ohmyagent request"}`。
    """
    mac = hmac.new((CONFIG.get("signing_secret") or "").encode(),
                   instructions.encode(), hashlib.sha256)
    return "v1=" + mac.hexdigest()


def _ohmyagent_headers(instructions: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + CONFIG["api_key"],
        "X-Ohmyagent-Signature": _sign_instructions(instructions),
        "User-Agent": "ohmyagent f6b21ad",
    }


def _upstream_model(local: str) -> str:
    """本地模型名 → 上游模型名。

    GUI 白名单里是去前缀的免费模型名（如 qwen3.8-flash），上游要全名
    monkeycode-basic/qwen3.8-flash；已经带 '/' 的原样透传。
    """
    local = (local or "").strip()
    if not local:
        return CONFIG.get("default_model") or (CONFIG["basic_prefix"] + "qwen3.8-flash")
    if "/" in local:
        return local
    return (CONFIG.get("basic_prefix") or "") + local


def _msg_text(content) -> str:
    """把 chat 消息的 content（str 或 [{type,text}]）压成纯文本。"""
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


def _openai_tools(payload: dict) -> list:
    """OpenAI chat 的 tools → [{name, description, parameters}]（兼容嵌套 function 写法）。"""
    out = []
    for t in (payload.get("tools") or []):
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        out.append({"name": str(name),
                    "description": str(fn.get("description") or ""),
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _tool_choice_name(tc):
    """tool_choice={"type":"function","function":{"name":X}} → X（其它形态返回 None）。"""
    if isinstance(tc, dict):
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        return fn.get("name")
    return None


def _args_json(args) -> str:
    """tool_call 的 arguments 可能是 str 或 dict；统一成 JSON 字符串。"""
    if isinstance(args, str):
        return args
    if args is None:
        return "{}"
    return json.dumps(args, ensure_ascii=False)


def _args_obj(args):
    """arguments → dict（Anthropic tool_use.input 要对象）。"""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            v = json.loads(args or "{}")
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            return {}
    return {}


def _chat_to_responses(payload: dict, model: str) -> dict:
    """OpenAI chat/completions 请求 → Responses API 请求体。

    system/developer 消息合并成 instructions（签名对象，不能为空）；其余消息映射到
    input。助手侧 tool_calls → function_call 项，role=tool → function_call_output 项
    （按 call_id 回填），tools/tool_choice 也一并转换。
    """
    sys_parts, turns = [], []
    for m in (payload.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").lower()
        text = _msg_text(m.get("content"))
        if role in ("system", "developer"):
            if text:
                sys_parts.append(text)
            continue
        if role == "tool":
            turns.append({"type": "function_call_output",
                          "call_id": m.get("tool_call_id") or "",
                          "output": text or ""})
            continue
        if text:
            turns.append({"role": "assistant" if role == "assistant" else "user",
                          "content": [{"type": "output_text" if role == "assistant" else "input_text",
                                       "text": text}]})
        for tc in (m.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            turns.append({"type": "function_call",
                          "call_id": tc.get("id") or "",
                          "name": fn.get("name") or "",
                          "arguments": _args_json(fn.get("arguments"))})
    instructions = "\n\n".join(sys_parts).strip() or "You are a helpful assistant."
    if not turns:  # input 不能为空
        turns = [{"role": "user", "content": [{"type": "input_text", "text": "(empty)"}]}]
    body = {"model": _upstream_model(model), "instructions": instructions,
            "input": turns, "store": False, "stream": bool(payload.get("stream"))}
    if payload.get("max_tokens"):
        body["max_output_tokens"] = int(payload["max_tokens"])
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        body["top_p"] = payload["top_p"]
    tools = _openai_tools(payload)
    tc = payload.get("tool_choice")
    # 注意：不能用 str(tc).lower()=="none" 判断——tc 为 None 时 str() 会得到 "None"，
    # 会把「未指定 tool_choice」误判成 "none"，导致 tools 被整段丢掉。
    tc_none = isinstance(tc, str) and tc.strip().lower() == "none"
    if tools and not tc_none:
        body["tools"] = [{"type": "function", "name": t["name"],
                          "description": t["description"], "parameters": t["parameters"]}
                         for t in tools]
        if isinstance(tc, dict):
            n = _tool_choice_name(tc)
            body["tool_choice"] = {"type": "function", "name": n} if n else "auto"
        elif isinstance(tc, str) and tc.strip().lower() in ("required", "any"):
            body["tool_choice"] = "required"
        elif isinstance(tc, str) and tc.strip():
            body["tool_choice"] = "auto"
    return body


def _responses_to_chat(resp: dict, model: str) -> dict:
    """Responses API 响应 → chat.completion（function_call → tool_calls）。"""
    text, reasoning, tool_calls = "", "", []
    for item in (resp.get("output") or []):
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "reasoning":
            for s in (item.get("summary") or []):
                if isinstance(s, dict):
                    reasoning += str(s.get("text") or "")
        elif t == "message":
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    text += str(c.get("text") or "")
        elif t == "function_call":
            tool_calls.append({"id": item.get("call_id") or item.get("id") or "",
                               "type": "function",
                               "function": {"name": item.get("name") or "",
                                            "arguments": _args_json(item.get("arguments"))}})
    msg = {"role": "assistant", "content": text}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
        if not text:
            msg["content"] = None
    u = resp.get("usage") or {}
    pt = int(u.get("input_tokens") or 0)
    ct = int(u.get("output_tokens") or 0)
    _record_usage(pt, ct)
    if tool_calls:
        finish = "tool_calls"
    elif resp.get("status") in (None, "completed"):
        finish = "stop"
    elif str(resp.get("status")) == "incomplete":
        finish = "length"
    else:
        finish = str(resp.get("status"))
    return {
        "id": resp.get("id") or ("chatcmpl-" + uuid.uuid4().hex[:24]),
        "object": "chat.completion",
        "created": int(resp.get("created_at") or time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


def _chunk(cid, model, created, delta, finish=None) -> bytes:
    obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


# ---- Anthropic Messages 协议（type=anthropic 的模型走这条）----

def _chat_to_messages(payload: dict, model: str) -> dict:
    """OpenAI chat/completions → Anthropic Messages 请求体（含工具）。

    system 合并成 `system` 字段（签名对象，不能为空）。
    助手侧 tool_calls → tool_use 内容块；role=tool 的结果按 tool_use_id 变成
    tool_result 块，且连续的多个结果合并进同一条 user 消息（Anthropic 要求
    tool_result 出现在 user 回合里）。Anthropic 要求 max_tokens 必填，缺省 4096。
    """
    sys_parts, turns = [], []

    def _push(role, blocks):
        """同角色相邻的块合并进同一条消息，避免连续 user 回合。"""
        if turns and turns[-1]["role"] == role and isinstance(turns[-1]["content"], list):
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    for m in (payload.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").lower()
        text = _msg_text(m.get("content"))
        if role in ("system", "developer"):
            if text:
                sys_parts.append(text)
            continue
        if role == "tool":
            _push("user", [{"type": "tool_result",
                            "tool_use_id": m.get("tool_call_id") or "",
                            "content": text or ""}])
            continue
        if role == "assistant":
            blocks = []
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in (m.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "",
                               "name": fn.get("name") or "",
                               "input": _args_obj(fn.get("arguments"))})
            if blocks:
                turns.append({"role": "assistant", "content": blocks})
            continue
        if text:
            _push("user", [{"type": "text", "text": text}])
    system = "\n\n".join(sys_parts).strip() or "You are a helpful assistant."
    if not turns:
        turns = [{"role": "user", "content": [{"type": "text", "text": "(empty)"}]}]
    body = {"model": model, "system": system, "messages": turns,
            "max_tokens": int(payload.get("max_tokens") or 4096)}
    if payload.get("stream"):
        body["stream"] = True
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        body["top_p"] = payload["top_p"]
    tools = _openai_tools(payload)
    tc = payload.get("tool_choice")
    tc_none = isinstance(tc, str) and tc.strip().lower() == "none"   # 别用 str(None) 判断
    if tools and not tc_none:
        body["tools"] = [{"name": t["name"], "description": t["description"],
                          "input_schema": t["parameters"]} for t in tools]
        if isinstance(tc, dict):
            n = _tool_choice_name(tc)
            body["tool_choice"] = {"type": "tool", "name": n} if n else {"type": "auto"}
        elif isinstance(tc, str) and tc.strip().lower() in ("required", "any"):
            body["tool_choice"] = {"type": "any"}
        elif isinstance(tc, str) and tc.strip():
            body["tool_choice"] = {"type": "auto"}
    return body


def _finish_reason(stop_reason) -> str:
    if stop_reason in (None, "end_turn", "stop_sequence", "stop"):
        return "stop"
    return str(stop_reason)


def _messages_to_chat(resp: dict, model: str) -> dict:
    """Anthropic Messages 响应 → chat.completion（tool_use → tool_calls）。"""
    text, reasoning, tool_calls = "", "", []
    for c in (resp.get("content") or []):
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            text += str(c.get("text") or "")
        elif t == "thinking":
            reasoning += str(c.get("thinking") or "")
        elif t == "tool_use":
            tool_calls.append({"id": c.get("id") or "", "type": "function",
                               "function": {"name": c.get("name") or "",
                                            "arguments": _args_json(c.get("input"))}})
    msg = {"role": "assistant", "content": text}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
        if not text:
            msg["content"] = None
    u = resp.get("usage") or {}
    pt = int(u.get("input_tokens") or 0)
    ct = int(u.get("output_tokens") or 0)
    _record_usage(pt, ct)
    return {
        "id": resp.get("id") or ("chatcmpl-" + uuid.uuid4().hex[:24]),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": "tool_calls" if tool_calls
                                      else _finish_reason(resp.get("stop_reason"))}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


async def _anthropic_stream(body: dict, model: str):
    """Anthropic Messages SSE → chat.completion.chunk 流。"""
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _ohmyagent_headers(body["system"])
    headers["anthropic-version"] = "2023-06-01"
    headers["Accept"] = "text/event-stream"
    yield _chunk(cid, model, created, {"role": "assistant", "content": ""})
    tool_seq: dict = {}          # Anthropic 内容块 index → OpenAI tool_calls 序号
    got_tools = False
    try:
        async with _client() as c:
            async with c.stream("POST", "/messages", content=payload, headers=headers) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "replace")
                    _log(f"[chat] {model} 官方代理(/messages) HTTP {r.status_code}: {detail[:200]}")
                    err = {"error": {"message": f"monkeycode upstream {r.status_code}: {detail[:400]}",
                                     "type": "upstream_error", "code": r.status_code}}
                    yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    et = ev.get("type")
                    if et == "content_block_start":
                        cb = ev.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            ti = len(tool_seq)
                            tool_seq[ev.get("index")] = ti
                            got_tools = True
                            yield _chunk(cid, model, created, {"tool_calls": [{
                                "index": ti, "id": cb.get("id") or "", "type": "function",
                                "function": {"name": cb.get("name") or "", "arguments": ""}}]})
                    elif et == "content_block_delta":
                        d = ev.get("delta") or {}
                        dt = d.get("type")
                        if dt == "text_delta" and d.get("text"):
                            yield _chunk(cid, model, created, {"content": d["text"]})
                        elif dt == "thinking_delta" and d.get("thinking"):
                            yield _chunk(cid, model, created, {"reasoning_content": d["thinking"]})
                        elif dt == "input_json_delta" and d.get("partial_json"):
                            ti = tool_seq.get(ev.get("index"), 0)
                            yield _chunk(cid, model, created, {"tool_calls": [{
                                "index": ti,
                                "function": {"arguments": d["partial_json"]}}]})
                    elif et == "message_delta":
                        u = ev.get("usage") or {}
                        _record_usage(u.get("input_tokens", 0), u.get("output_tokens", 0))
    except Exception as e:  # noqa: BLE001
        _log(f"[chat] {model} 官方代理(/messages) 流式错误: {type(e).__name__}: {e}")
        err = {"error": {"message": f"monkeycode upstream error: {e}", "type": "upstream_error"}}
        yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"
        return
    yield _chunk(cid, model, created, {}, finish="tool_calls" if got_tools else "stop")
    yield b"data: [DONE]\n\n"


async def _ohmyagent_stream(body: dict, model: str):
    """把上游 Responses SSE 转成 chat.completion.chunk 流。"""
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _ohmyagent_headers(body["instructions"])
    headers["Accept"] = "text/event-stream"
    yield _chunk(cid, model, created, {"role": "assistant", "content": ""})
    fc_seq: dict = {}            # Responses item_id → OpenAI tool_calls 序号
    n_fc = 0
    got_tools = False
    try:
        async with _client() as c:
            async with c.stream("POST", "/responses", content=payload, headers=headers) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "replace")
                    _log(f"[chat] {model} 官方代理(/responses) HTTP {r.status_code}: {detail[:200]}")
                    err = {"error": {"message": f"monkeycode upstream {r.status_code}: {detail[:400]}",
                                     "type": "upstream_error", "code": r.status_code}}
                    yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    et = ev.get("type")
                    if et == "response.output_item.added":
                        it = ev.get("item") or {}
                        if it.get("type") == "function_call":
                            ti = n_fc
                            n_fc += 1
                            for k in (it.get("id"), ev.get("item_id")):
                                if k:
                                    fc_seq[k] = ti
                            got_tools = True
                            yield _chunk(cid, model, created, {"tool_calls": [{
                                "index": ti, "id": it.get("call_id") or it.get("id") or "",
                                "type": "function",
                                "function": {"name": it.get("name") or "", "arguments": ""}}]})
                    elif et == "response.function_call_arguments.delta":
                        if ev.get("delta"):
                            ti = fc_seq.get(ev.get("item_id"), 0)
                            yield _chunk(cid, model, created, {"tool_calls": [{
                                "index": ti, "function": {"arguments": ev["delta"]}}]})
                    elif et == "response.output_text.delta":
                        if ev.get("delta"):
                            yield _chunk(cid, model, created, {"content": ev["delta"]})
                    elif et == "response.reasoning_summary_text.delta":
                        if ev.get("delta"):
                            yield _chunk(cid, model, created, {"reasoning_content": ev["delta"]})
                    elif et == "response.completed":
                        u = ((ev.get("response") or {}).get("usage") or {})
                        _record_usage(u.get("input_tokens", 0), u.get("output_tokens", 0))
    except Exception as e:  # noqa: BLE001
        _log(f"[chat] {model} 官方代理(/responses) 流式错误: {type(e).__name__}: {e}")
        err = {"error": {"message": f"monkeycode upstream error: {e}", "type": "upstream_error"}}
        yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"
        return
    yield _chunk(cid, model, created, {}, finish="tool_calls" if got_tools else "stop")
    yield b"data: [DONE]\n\n"


def auto_configure(log=print):
    """解析上游：环境变量 > 官方代理（ohmyagent）> 静态 key 透传。"""
    base = os.environ.get("MC2_OPENAI_BASE_URL", "").strip()
    key = os.environ.get("MC2_OPENAI_API_KEY", "").strip()
    if base and key:
        CONFIG["mode"] = "static"
        CONFIG["base_url"], CONFIG["api_key"] = base, key
        CONFIG["signing_secret"] = ""
        log(f"[mc] 上游（环境变量/static）：{base}  (key: {key[:6]}…)")
        return

    oma = find_ohmyagent_key()
    if oma:
        CONFIG["mode"] = "ohmyagent"
        CONFIG["base_url"] = oma["base_url"].rstrip("/")
        CONFIG["api_key"] = oma["api_key"]
        CONFIG["signing_secret"] = oma["signing_secret"]
        CONFIG["model_types"] = _load_model_protocols()
        basics = _basic_models()
        if basics:
            CONFIG["default_model"] = CONFIG["basic_prefix"] + basics[0]
        types = CONFIG["model_types"]
        n_ant = sum(1 for v in types.values() if v == "anthropic")
        log(f"[mc] 上游（官方代理/ohmyagent）：{CONFIG['base_url']}  "
            f"(key: {oma['api_key'][:6]}…) 签名已启用")
        log(f"[mc] 模型协议表：{len(types)} 个（{n_ant} 个走 /messages，"
            f"{len(types) - n_ant} 个走 /responses）")
        return

    fb, fk = _load_from_desktop()
    CONFIG["mode"] = "static"
    CONFIG["base_url"], CONFIG["api_key"] = fb or "", fk or ""
    CONFIG["signing_secret"] = ""
    if fb and fk:
        log(f"[mc] 上游（桌面端静态 key/static）：{fb}  (key: {fk[:6]}…)")
        if not _is_openai_base(fb):
            log(f"[mc] ⚠ 上游 {fb} 不是 OpenAI 兼容端点（缺 /openai）；"
                f"本模块只转发 /chat/completions，请求大概率 404。"
                f"请检查桌面端 config.json 是否有 /api/openai/v1 条目。")
    else:
        log("[mc] ⚠ 未找到上游配置（ohmyagent-key.json 与 config.json 都没有可用凭据）")


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


def _usage_file():
    d = os.environ.get("BUDDYZ_DATA_DIR", "")
    if not d:
        return None
    return str(Path(d) / "usage_mc.json")


def _record_usage(prompt: int, completion: int):
    fp = _usage_file()
    if fp is None:
        return
    try:
        day = time.strftime("%Y-%m-%d")
        try:
            alld = json.loads(Path(fp).read_text(encoding="utf-8"))
        except Exception:
            alld = {}
        e = alld.get(day) or {}
        e["prompt"] = int(e.get("prompt", 0)) + max(0, int(prompt or 0))
        e["completion"] = int(e.get("completion", 0)) + max(0, int(completion or 0))
        e["total"] = e["prompt"] + e["completion"]
        e["n"] = int(e.get("n", 0)) + 1
        alld[day] = e
        # 只保留近 14 天
        for k in [k for k in alld if k < day][:0]:
            pass
        keys = sorted(alld)[-14:]
        alld = {k: alld[k] for k in keys}
        Path(fp).write_text(json.dumps(alld, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _read_usage() -> dict:
    fp = _usage_file()
    day = time.strftime("%Y-%m-%d")
    try:
        alld = json.loads(Path(fp).read_text(encoding="utf-8"))
        e = alld.get(day) or {}
        return {"date": day, "prompt": int(e.get("prompt", 0)),
                "completion": int(e.get("completion", 0)),
                "total": int(e.get("total", 0)), "requests": int(e.get("n", 0))}
    except Exception:
        return {"date": day, "prompt": 0, "completion": 0, "total": 0, "requests": 0}


def _client():
    return httpx.AsyncClient(
        base_url=CONFIG["base_url"],
        headers={"Authorization": "Bearer " + CONFIG["api_key"]},
        timeout=httpx.Timeout(900.0),
    )


# ---------------------------------------------------------------------------
# 账号池（多组透传 key 故障轮转；默认关闭）
# ---------------------------------------------------------------------------
import threading as _th

_POOL = {"idx": 0, "cd": {}}
_POOL_LOCK = _th.RLock()


def _pool_entries():
    """候选 key 列表：本机桌面端打底 + 启用的池 key"""
    ents = []
    if CONFIG.get("base_url") and CONFIG.get("api_key"):
        ents.append({"base_url": CONFIG["base_url"], "api_key": CONFIG["api_key"],
                     "label": "本机桌面端"})
    if CONFIG.get("pool_enabled"):
        for k in (CONFIG.get("pool_keys") or []):
            if not isinstance(k, dict):
                continue
            if not k.get("enabled", True):
                continue
            if k.get("base_url") and k.get("api_key"):
                ents.append({"base_url": k["base_url"], "api_key": k["api_key"],
                             "label": k.get("label") or k["base_url"]})
    return ents


def _client_for(ent):
    return httpx.AsyncClient(
        base_url=ent["base_url"],
        headers={"Authorization": "Bearer " + ent["api_key"]},
        timeout=httpx.Timeout(900.0),
    )


def _pool_order():
    """健康 key 的轮转顺序（跳过冷却/禁用）；返回 [(idx, entry)]"""
    ents = _pool_entries()
    now = time.time()
    order = []
    for i, e in enumerate(ents):
        st = _POOL["cd"].get(i)
        if st == "off":
            continue
        if isinstance(st, (int, float)) and st > now:
            continue
        order.append((i, e))
    if order:
        k = _POOL["idx"] % len(order)
        order = order[k:] + order[:k]
    return order


def _pool_fail(i, status=None):
    with _POOL_LOCK:
        if status in (401, 403):
            _POOL["cd"][i] = "off"  # key 失效，不再自动试
        elif status == 429 or (isinstance(status, int) and status >= 500):
            _POOL["cd"][i] = time.time() + 300
        else:  # 传输异常等
            _POOL["cd"][i] = time.time() + 60
        _POOL["idx"] += 1


def _pool_ok(i):
    # 成功也推进轮转下标，负载摊到所有健康 key；顺手清掉旧冷却
    with _POOL_LOCK:
        _POOL["cd"].pop(i, None)
        _POOL["idx"] += 1


def _pool_state():
    now = time.time()
    out = []
    for i, e in enumerate(_pool_entries()):
        st = _POOL["cd"].get(i)
        out.append({"label": e.get("label") or "",
                    "key_prefix": (e.get("api_key") or "")[:6],
                    "cooling": bool(isinstance(st, (int, float)) and st > now),
                    "off": st == "off"})
    return {"enabled": bool(CONFIG.get("pool_enabled")), "keys": out}


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": (resp.text or "")[:500], "type": "upstream_error",
                          "code": resp.status_code}}


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="MonkeyCode→OpenAI")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "monkeycode2openai",
        "mode": CONFIG.get("mode"),
        "configured": bool(CONFIG["base_url"] and CONFIG["api_key"]),
        "base_url": CONFIG["base_url"] or None,
        "pool": _pool_state(),
    }


def _check_local_auth(req: Request) -> bool:
    """若设置了 local_api_key，校验客户端 Bearer；返回是否放行。"""
    key = CONFIG.get("local_api_key") or ""
    if not key:
        return True
    auth = req.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == key


def _apply_exposed_filter(body):
    """按 CONFIG['exposed_models'] 白名单过滤上游模型列表；空=不过滤"""
    allow = CONFIG.get("exposed_models") or []
    if not allow or not isinstance(body, dict):
        return body
    allowset = set(allow)
    data = body.get("data") or []
    body["data"] = [m for m in data if isinstance(m, dict) and m.get("id") in allowset]
    return body


@app.get("/v1/models")
async def models(req: Request):
    if not _check_local_auth(req):
        return JSONResponse(
            {"error": {"message": "invalid API key", "type": "auth_error"}},
            status_code=401,
        )
    if not (CONFIG["base_url"] and CONFIG["api_key"]):
        return JSONResponse({"object": "list", "data": []})

    if CONFIG.get("mode") == "ohmyagent":
        # 官方代理没有 /models；模型名来自桌面端 config.json 的 monkeycode-basic/*。
        data = [{"id": m, "object": "model", "created": 0, "owned_by": "monkeycode"}
                for m in _basic_models()]
        body = {"object": "list", "data": data}
        if req.query_params.get("all") != "1":
            body = _apply_exposed_filter(body)
        return JSONResponse(content=body)

    last_err = None
    for i, ent in _pool_order():
        try:
            async with _client_for(ent) as c:
                r = await c.get("/models")
        except Exception as e:  # noqa: BLE001
            _log(f"[models] {ent.get('label')} 上游错误: {e}")
            _pool_fail(i)
            last_err = e
            continue
        if r.status_code == 200:
            _pool_ok(i)
            try:
                body = r.json()
            except Exception:
                body = {"object": "list", "data": []}
            # ?all=1 跳过白名单过滤（供本机 GUI 取全量候选用）
            if req.query_params.get("all") != "1":
                body = _apply_exposed_filter(body)
            return JSONResponse(content=body, status_code=r.status_code)
        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            _log(f"[models] {ent.get('label')} HTTP {r.status_code}，切下一个 key")
            _pool_fail(i, r.status_code)
            last_err = _safe_json(r)
            continue
        try:
            body = r.json()
        except Exception:
            body = {"object": "list", "data": []}
        return JSONResponse(content=body, status_code=r.status_code)
    _log(f"[models] 全部 key 失败: {str(last_err)[:200]}")
    return JSONResponse({"object": "list", "data": []})


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not _check_local_auth(req):
        return JSONResponse(
            {"error": {"message": "invalid API key", "type": "auth_error"}},
            status_code=401,
        )
    if not (CONFIG["base_url"] and CONFIG["api_key"]):
        return JSONResponse(
            {"error": {"message": "MonkeyCode upstream not configured "
                                  "(no config.json models[0].api_key)",
                       "type": "config_error"}},
            status_code=503,
        )

    raw = await req.body()
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    stream = bool(payload.get("stream", False))
    model = payload.get("model") or ""   # 不要用 "?" 兜底：会被当成模型名去拼前缀
    shown = model or "(默认)"

    # ---- 官方代理（ohmyagent）：按模型 type 分流协议 ----
    #   anthropic        → POST /messages   （Anthropic Messages API）
    #   openai-responses → POST /responses  （OpenAI Responses API）
    # 选错协议上游会回 404 路由不存在。
    if CONFIG.get("mode") == "ohmyagent":
        up_model = _upstream_model(model)
        proto = _protocol_for(up_model)
        if proto == "anthropic":
            body = _chat_to_messages(payload, up_model)
            path, signed = "/messages", body["system"]
            convert = _messages_to_chat
        else:
            body = _chat_to_responses(payload, model)
            path, signed = "/responses", body["instructions"]
            convert = _responses_to_chat

        if stream:
            gen = (_anthropic_stream(body, model) if proto == "anthropic"
                   else _ohmyagent_stream(body, model))
            return StreamingResponse(gen, media_type="text/event-stream")

        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = _ohmyagent_headers(signed)
        if proto == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        try:
            async with _client() as c:
                r = await c.post(path, content=payload_bytes, headers=headers)
        except Exception as e:  # noqa: BLE001
            _log(f"[chat] {shown} 官方代理({path}) 错误: {type(e).__name__}: {e}")
            return JSONResponse(
                {"error": {"message": f"monkeycode upstream error: {e}",
                           "type": "upstream_error"}},
                status_code=502,
            )
        if r.status_code != 200:
            _log(f"[chat] {shown} 官方代理({path}) HTTP {r.status_code}: {r.text[:200]}")
            return JSONResponse(
                {"error": {"message": f"monkeycode upstream {r.status_code}: {r.text[:400]}",
                           "type": "upstream_error", "code": r.status_code}},
                status_code=r.status_code,
            )
        try:
            return JSONResponse(content=convert(r.json(), model))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": {"message": f"bad upstream body: {e}", "type": "upstream_error"}},
                status_code=502,
            )

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }

    async def gen(ent, idx):
        try:
            async with _client_for(ent) as c:
                async with c.stream("POST", "/chat/completions", content=raw, headers=headers) as r:
                    if r.status_code != 200:
                        _pool_fail(idx, r.status_code)
                    else:
                        _pool_ok(idx)
                    async for chunk in r.aiter_raw():
                        yield chunk
        except Exception as e:  # noqa: BLE001
            _log(f"[chat] {model} {ent.get('label')} 上游错误: {e}")
            _pool_fail(idx)
            err = json.dumps({"error": {"message": f"monkeycode upstream error: {e}",
                                        "type": "upstream_error"}})
            yield ("data: " + err + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"

    if stream:
        order = _pool_order()
        if not order:
            return JSONResponse(
                {"error": {"message": "no healthy upstream key (all cooling/disabled)",
                           "type": "pool_exhausted"}},
                status_code=503,
            )
        idx, ent = order[0]
        return StreamingResponse(gen(ent, idx), media_type="text/event-stream")

    last_resp = None
    for idx, ent in _pool_order():
        try:
            async with _client_for(ent) as c:
                r = await c.post("/chat/completions", content=raw, headers=headers)
        except Exception as e:  # noqa: BLE001
            _log(f"[chat] {model} {ent.get('label')} 上游错误: {e}")
            _pool_fail(idx)
            last_resp = None
            continue
        if r.status_code == 200:
            _pool_ok(idx)
            try:
                body = r.json()
                u = (body.get("usage") or {}) if isinstance(body, dict) else {}
                _record_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                return JSONResponse(content=body, status_code=r.status_code)
            except Exception:
                return JSONResponse(
                    {"error": {"message": r.text[:1000], "type": "upstream_error",
                               "code": r.status_code}},
                    status_code=r.status_code,
                )
        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            _log(f"[chat] {model} {ent.get('label')} HTTP {r.status_code}，切下一个 key")
            _pool_fail(idx, r.status_code)
            last_resp = r
            continue
        return JSONResponse(content=_safe_json(r), status_code=r.status_code)
    if last_resp is not None:
        return JSONResponse(content=_safe_json(last_resp), status_code=last_resp.status_code)
    return JSONResponse(
        {"error": {"message": "no healthy upstream key (all cooling/disabled)",
                   "type": "pool_exhausted"}},
        status_code=503,
    )


@app.get("/v1/usage")
async def usage():
    """本地累计的今日 token 用量（上游无额度接口，只能统计已用）"""
    d = _read_usage()
    d["stream_note"] = "仅统计非流式请求；流式不上报 usage"
    return d


def _saas():
    from pathlib import Path as _P
    import sys as _s
    _d = str(_P(__file__).resolve().parent)
    if _d not in _s.path:
        _s.path.insert(0, _d)
    import mc_saas
    return mc_saas


@app.get("/v1/wallet")
async def wallet():
    """SaaS 钱包：积分余额 + 每日 token 额度"""
    try:
        return {"ok": True, "data": _saas().get_wallet()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.get("/v1/checkin")
async def checkin_status():
    try:
        return {"ok": True, "data": _saas().get_checkin_status()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.post("/v1/checkin")
async def checkin():
    """每日签到（Cap.js PoW 自动解，成功 +100 积分）"""
    try:
        return {"ok": True, "data": _saas().do_checkin()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


if __name__ == "__main__":
    import uvicorn
    auto_configure()
    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="info")
