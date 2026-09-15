# -*- coding: utf-8 -*-
"""Hermes 式工具调用往返测试：第 1 轮模型要工具 → 第 2 轮喂回结果 → 模型作答。
覆盖两个协议（/messages 与 /responses）× 流式/非流式。"""
import sys, os, json, threading, time, urllib.request, urllib.error

RT = os.path.join(os.environ["LOCALAPPDATA"], "BuddyZGateway", "runtime", "monkeycode2openai")
sys.path.insert(0, RT)
import monkeycode2openai as mc
mc.auto_configure()

PORT = 9014
import uvicorn
cfg = uvicorn.Config(mc.app, host="127.0.0.1", port=PORT, log_level="warning")
srv = uvicorn.Server(cfg); threading.Thread(target=srv.run, daemon=True).start()
for _ in range(60):
    time.sleep(0.2)
    if srv.started: break

BASE = f"http://127.0.0.1:{PORT}/v1"
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string", "description": "城市名"}},
                       "required": ["city"]},
    },
}]


def post(payload):
    req = urllib.request.Request(BASE + "/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def collect_stream(raw):
    """把 SSE 拼成 (content, tool_calls, finish_reason)。"""
    content, tools, finish = "", {}, None
    for ln in raw.splitlines():
        if not ln.startswith("data:"):
            continue
        p = ln[5:].strip()
        if p == "[DONE]":
            break
        try:
            d = json.loads(p)
        except Exception:
            continue
        ch = (d.get("choices") or [{}])[0]
        if ch.get("finish_reason"):
            finish = ch["finish_reason"]
        dl = ch.get("delta") or {}
        if dl.get("content"):
            content += dl["content"]
        for tc in (dl.get("tool_calls") or []):
            i = tc.get("index", 0)
            slot = tools.setdefault(i, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]
    return content, list(tools.values()), finish


for model in ("qwen3.8-flash", "kimi-k2.5"):
    proto = mc._protocol_for(mc._upstream_model(model))
    print(f"\n{'='*70}\n模型 {model}  (协议={proto})\n{'='*70}")

    msgs = [{"role": "system", "content": "You are a helpful assistant. Use tools when needed."},
            {"role": "user", "content": "北京今天天气怎么样？请调用工具查询。"}]

    # --- 第 1 轮：期望模型要求调用工具 ---
    st, body = post({"model": model, "messages": msgs, "tools": TOOLS,
                     "max_tokens": 300, "stream": False})
    if st != 200:
        print(f"  第1轮失败 HTTP {st}: {body[:200]}"); continue
    d = json.loads(body)
    ch = (d.get("choices") or [{}])[0]
    m = ch.get("message") or {}
    tcalls = m.get("tool_calls") or []
    print(f"  第1轮 finish={ch.get('finish_reason')} tool_calls={len(tcalls)}")
    for tc in tcalls:
        print(f"        id={tc['id']}  name={tc['function']['name']}  args={tc['function']['arguments']}")
    if not tcalls:
        print("       ✗ 模型没有调用工具（翻译可能没生效）"); continue

    # --- 第 2 轮：回喂工具结果 ---
    msgs2 = msgs + [{"role": "assistant", "content": m.get("content"), "tool_calls": tcalls},
                    {"role": "tool", "tool_call_id": tcalls[0]["id"],
                     "content": "北京：晴，26°C，微风"}]
    st, body = post({"model": model, "messages": msgs2, "tools": TOOLS,
                     "max_tokens": 300, "stream": False})
    if st != 200:
        print(f"  第2轮失败 HTTP {st}: {body[:250]}"); continue
    d2 = json.loads(body)
    c2 = (d2.get("choices") or [{}])[0]
    txt = ((c2.get("message") or {}).get("content") or "").replace("\n", " ")
    print(f"  第2轮 finish={c2.get('finish_reason')}")
    print(f"        回答: {txt[:110]}")

    # --- 流式：同样两轮 ---
    st, raw = post({"model": model, "messages": msgs, "tools": TOOLS,
                    "max_tokens": 300, "stream": True})
    content, strm_tools, finish = collect_stream(raw)
    ok = bool(strm_tools)
    print(f"  流式第1轮 finish={finish} tool_calls={len(strm_tools)} → {'OK' if ok else '失败'}")
    for t in strm_tools:
        print(f"        id={t['id']}  name={t['name']}  args={t['args']}")

    msgs3 = msgs + [{"role": "assistant", "content": None,
                     "tool_calls": [{"id": t["id"], "type": "function",
                                     "function": {"name": t["name"], "arguments": t["args"]}}
                                    for t in strm_tools]},
                    {"role": "tool", "tool_call_id": strm_tools[0]["id"],
                     "content": "北京：晴，26°C，微风"}] if strm_tools else []
    if msgs3:
        st, raw = post({"model": model, "messages": msgs3, "tools": TOOLS,
                        "max_tokens": 300, "stream": True})
        content, _, finish = collect_stream(raw)
        print(f"  流式第2轮 finish={finish} 回答: {content.replace(chr(10),' ')[:110]}")

srv.should_exit = True
time.sleep(0.3)
print("\nDONE")
