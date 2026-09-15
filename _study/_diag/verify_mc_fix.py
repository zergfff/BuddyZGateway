# -*- coding: utf-8 -*-
"""验证 monkeycode2openai：起在 9010，实测 /health、/v1/models、chat 非流式 + 流式。

用 runtime 目录里的模块（materialize 出来的那份），确保测的是真正会跑起来的代码。
"""
import sys, os, json, threading, time, urllib.request, urllib.error

RT = os.path.join(os.environ["LOCALAPPDATA"], "BuddyZGateway", "runtime", "monkeycode2openai")
sys.path.insert(0, RT)

import monkeycode2openai as mc
mc.auto_configure()
print("mode      :", mc.CONFIG.get("mode"))
print("base_url  :", mc.CONFIG["base_url"])
print("signing   :", "已启用" if mc.CONFIG.get("signing_secret") else "未启用")
print("default   :", mc.CONFIG.get("default_model"))
print()

PORT = 9010
import uvicorn
cfg = uvicorn.Config(mc.app, host="127.0.0.1", port=PORT, log_level="warning")
srv = uvicorn.Server(cfg)
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(60):
    time.sleep(0.2)
    if srv.started:
        break


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def post(path, obj, raw_stream=False):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            if raw_stream:
                return r.status, r.read().decode("utf-8", "replace")
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


st, body = get("/health")
print("health :", st, body[:220])

st, body = get("/v1/models")
try:
    ids = [m["id"] for m in json.loads(body).get("data", [])]
except Exception:
    ids = []
print("models :", st, "count=", len(ids), ids[:8])

print("\n--- chat 非流式 ---")
st, body = post("/v1/chat/completions", {
    "model": "qwen3.8-flash",
    "messages": [{"role": "system", "content": "你是一个简洁的助手。"},
                 {"role": "user", "content": "用一句话说明什么是HTTP"}],
    "max_tokens": 80, "stream": False})
print("status :", st)
try:
    d = json.loads(body)
    ch = (d.get("choices") or [{}])[0]
    print("content:", (ch.get("message") or {}).get("content"))
    print("finish :", ch.get("finish_reason"), "| usage:", d.get("usage"))
except Exception:
    print("body   :", body[:300])

print("\n--- chat 流式 ---")
st, body = post("/v1/chat/completions", {
    "model": "qwen3.8-flash",
    "messages": [{"role": "user", "content": "数 1 到 5"}],
    "max_tokens": 60, "stream": True})
print("status :", st)
text = ""
nchunks = 0
for ln in body.splitlines():
    if not ln.startswith("data:"):
        continue
    p = ln[5:].strip()
    if p == "[DONE]":
        break
    try:
        o = json.loads(p)
    except Exception:
        continue
    nchunks += 1
    d = (o.get("choices") or [{}])[0].get("delta") or {}
    text += d.get("content") or ""
print("chunks :", nchunks)
print("text   :", text)

srv.should_exit = True
time.sleep(0.3)
print("\nDONE")
