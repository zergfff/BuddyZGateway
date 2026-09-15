# -*- coding: utf-8 -*-
"""逐个打通 6 个免费基础模型：确认按 type 分流协议后都能出话。"""
import sys, os, json, threading, time, urllib.request, urllib.error

RT = os.path.join(os.environ["LOCALAPPDATA"], "BuddyZGateway", "runtime", "monkeycode2openai")
sys.path.insert(0, RT)
import monkeycode2openai as mc
mc.auto_configure()

print("mode        :", mc.CONFIG.get("mode"))
types = mc.CONFIG.get("model_types") or {}
print("协议表条目  :", len(types))
models = [mc.CONFIG["basic_prefix"] + m for m in mc._basic_models()]
print("待测模型    :", len(models))
print()

PORT = 9011
import uvicorn
cfg = uvicorn.Config(mc.app, host="127.0.0.1", port=PORT, log_level="warning")
srv = uvicorn.Server(cfg)
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(60):
    time.sleep(0.2)
    if srv.started:
        break


def chat(model, stream=False, text="只回答两个字：可以"):
    body = {"model": model, "messages": [{"role": "user", "content": text}],
            "max_tokens": 40, "stream": stream}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


ok_n = 0
for m in models:
    proto = mc._protocol_for(m)
    st, body = chat(m, stream=False)
    if st == 200:
        try:
            d = json.loads(body)
            c = (d.get("choices") or [{}])[0]
            txt = (c.get("message") or {}).get("content") or ""
            txt = txt.replace("\n", " ")[:44]
            print(f"  [OK ] {m:32s} proto={proto:16s} → {txt}")
            ok_n += 1
        except Exception as e:
            print(f"  [BAD] {m:32s} proto={proto:16s} 解析失败 {e}: {body[:100]}")
    else:
        print(f"  [FAIL] {m:32s} proto={proto:16s} HTTP {st}: {body[:110]}")

print(f"\n非流式：{ok_n}/{len(models)} 通过")

print("\n--- 流式抽查（第一个 anthropic 模型 + qwen3.8-flash）---")
for m in models:
    if mc._protocol_for(m) == "anthropic":
        st, body = chat(m, stream=True, text="数到3")
        txt = ""
        for ln in body.splitlines():
            if ln.startswith("data:") and ln[5:].strip() != "[DONE]":
                try:
                    txt += (((json.loads(ln[5:].strip()).get("choices") or [{}])[0]
                             .get("delta") or {}).get("content")) or ""
                except Exception:
                    pass
        print(f"  [{'OK ' if st==200 and txt else 'FAIL'}] {m:32s} 流式: {txt[:60]!r}")
        break
st, body = chat("qwen3.8-flash", stream=True, text="数到3")
txt = ""
for ln in body.splitlines():
    if ln.startswith("data:") and ln[5:].strip() != "[DONE]":
        try:
            txt += (((json.loads(ln[5:].strip()).get("choices") or [{}])[0]
                     .get("delta") or {}).get("content")) or ""
        except Exception:
            pass
print(f"  [{'OK ' if st==200 and txt else 'FAIL'}] qwen3.8-flash 流式: {txt[:60]!r}")

srv.should_exit = True
time.sleep(0.3)
print("\nDONE")
