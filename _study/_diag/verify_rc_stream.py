# -*- coding: utf-8 -*-
"""verify_rc_stream — 小浣熊（rc）通道流式/非流式的阻塞与增量性验证。

背景（修复前的两个真 bug）：
  ① `_do_chat` 用**同步 httpx** 直接在 async 路由里调用 → 卡住事件循环整段上游耗时。
  ② 上游请求没加 `stream=True` → 响应被 httpx 全量缓冲，路由再把整段 splitlines
     逐行吐出。表现：客户端在**整段生成完之前收不到任何字节**（看着像卡死），
     且所谓「流式」并不流。

本测试起一个**慢速假上游**（每个 chunk 间隔 0.3s，共 3 个），验证：
  · 首字节到达时间远早于总耗时  → 真增量透传
  · 非流式路径返回正常 JSON（且不阻塞事件循环 —— 用并发请求探测）
  · [DONE] 收尾

不依赖真实账号：`_get_token` 被替换为假 token。
用法：python verify_rc_stream.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
RC_DIR = REPO / "_study" / "raccoon2openai"

OK = True


def chk(name, cond, extra=""):
    global OK
    if not cond:
        OK = False
    print(("  ✓ " if cond else "  ✗ ") + name + (f"   {extra}" if extra else ""))


CHUNKS = ["Hello", " from", " fake-upstream"]
GAP = 0.30


class _FakeUpstream(BaseHTTPRequestHandler):
    """POST /chat/completions：慢速吐 3 个 SSE chunk，再发 [DONE]。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for c in CHUNKS:
                payload = json.dumps({"choices": [{"index": 0, "delta": {"content": c}}]})
                self._write_chunk(f"data: {payload}\n\n")
                time.sleep(GAP)
            self._write_chunk("data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            data = json.dumps({"id": "x", "object": "chat.completion", "model": body.get("model"),
                               "choices": [{"index": 0, "message": {"role": "assistant",
                                                                    "content": "pong"},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                         "total_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _write_chunk(self, text: str):
        b = text.encode()
        self.wfile.write(f"{len(b):X}\r\n".encode() + b + b"\r\n")
        self.wfile.flush()


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    up_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    for m in list(sys.modules):
        if m in ("raccoon2openai", "buddyzpool"):
            del sys.modules[m]
    sys.path.insert(0, str(RC_DIR))
    import raccoon2openai as rc

    rc.CONFIG["api_base"] = f"http://127.0.0.1:{up_port}"
    rc.CONFIG["timeout"] = 30
    rc._get_token = lambda: "FAKE-TOKEN"          # 不依赖真实 auth.json
    rc.refresh_token_locked = lambda: False

    # 必须起**真实 uvicorn**：fastapi.testclient 的 ASGI transport 会把整个
    # 响应体收完再交给调用方，根本测不出「增量」；只有真 HTTP 才能观察首字节。
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        app_port = s.getsockname()[1]

    import uvicorn
    import httpx
    server = uvicorn.Server(uvicorn.Config(rc.app, host="127.0.0.1", port=app_port,
                                          log_level="warning", lifespan="on"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{app_port}"
    for _ in range(120):
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        print("  ✗ 本地服务未起来")
        return 1
    print(f"  本地服务就绪 :{app_port}，假上游 :{up_port}")

    print("\n=== 1) 流式：增量透传（首字节须远早于总耗时） ===")
    t0 = time.time()
    first_at = None
    texts, done = [], False
    with httpx.stream("POST", base + "/v1/chat/completions",
                      json={"model": "raccoon-x", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]},
                      timeout=30) as r:
        chk("HTTP 200", r.status_code == 200, str(r.status_code))
        chk("content-type SSE", "text/event-stream" in r.headers.get("content-type", ""),
            r.headers.get("content-type", ""))
        for line in r.iter_lines():
            line = (line or "").strip()
            if not line.startswith("data:"):
                continue
            if first_at is None:
                first_at = time.time() - t0
            p = line[5:].strip()
            if p == "[DONE]":
                done = True
                break
            try:
                texts.append(json.loads(p)["choices"][0]["delta"]["content"])
            except Exception:
                pass
    total = time.time() - t0

    chk("收到全部 3 段文本", texts == CHUNKS, str(texts))
    chk("以 [DONE] 收尾", done)
    # 关键判据：3 段 × 0.3s 间隔 → 总耗时 ≥0.9s；若整体缓冲，首字节≈总耗时
    chk("首字节显著早于总耗时（真增量）",
        first_at is not None and total >= 0.8 and first_at <= total - 0.4,
        f"首字节 {first_at:.2f}s / 总 {total:.2f}s")

    print("\n=== 2) 非流式：正常返回 JSON ===")
    r = httpx.post(base + "/v1/chat/completions",
                   json={"model": "raccoon-x", "stream": False,
                         "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
    j = r.json()
    chk("HTTP 200 + OpenAI 形状",
        r.status_code == 200 and j["choices"][0]["message"]["content"] == "pong",
        f"{r.status_code} {str(j)[:80]}")

    print("\n=== 3) 事件循环不被阻塞（并发请求能穿插） ===")
    # 假上游每个流式响应要 0.9s。若事件循环被阻塞，非流式请求会被排在后面。
    import concurrent.futures as _cf

    def _stream_call():
        with httpx.stream("POST", base + "/v1/chat/completions",
                          json={"model": "raccoon-x", "stream": True,
                                "messages": [{"role": "user", "content": "hi"}]},
                          timeout=30) as rr:
            for _ln in rr.iter_lines():
                pass
        return 200

    def _plain_call():
        return httpx.post(base + "/v1/chat/completions",
                          json={"model": "raccoon-x", "stream": False,
                                "messages": [{"role": "user", "content": "hi"}]},
                          timeout=30).status_code

    t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(_stream_call)] + [ex.submit(_plain_call) for _ in range(2)]
        codes = [f.result() for f in futs]
    conc = time.time() - t0
    chk("并发 3 个请求全部成功", all(c == 200 for c in codes), str(codes))
    chk("未串行化（总耗时 < 2×单流式耗时）", conc < 1.8, f"{conc:.2f}s")

    if hasattr(server, "should_exit"):
        server.should_exit = True
    srv.shutdown()
    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
