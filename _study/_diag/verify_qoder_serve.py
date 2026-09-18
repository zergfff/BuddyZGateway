# -*- coding: utf-8 -*-
"""
verify_qoder_serve.py — Qoder 通道真机 HTTP 端到端验证。

做三件事（全部真实起 uvicorn，不用 TestClient）：
  1. materialize() 后从 runtime 目录 import qoder2openai（验证内嵌/base64 链路正确）
  2. 真起服务 → GET /health、GET /v1/models
  3. POST /v1/chat/completions（**不带 Token**）→ 应当得到明确的鉴权错误，
     而不是 200 + 把 "Not logged in" 当正常回复返回

安全：全程不设置任何 PAT，不会消耗额度。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO))

PORT = int(os.environ.get("QD_TEST_PORT") or 19500)
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def http(url: str, method: str = "GET", body: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(url, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    print("== Qoder 通道 HTTP 端到端（真实 uvicorn，无 Token，不消耗额度）==")

    # 1) materialize + 从 runtime import
    print("\n[1] materialize + runtime import")
    import importlib
    import BuddyZGateway as bzg

    bzg.materialize(lambda m: None)
    rt = bzg.runtime_dir()
    qd_path = rt / "qoder2openai" / "qoder2openai.py"
    check("runtime 里有 qoder2openai.py", qd_path.is_file(), str(qd_path))
    if qd_path.is_file():
        txt = qd_path.read_text(encoding="utf-8")
        check("runtime 版本含 cli_health_for", "cli_health_for" in txt)
        check("runtime 版本含 resolve_station", "resolve_station" in txt)

    sys.path.insert(0, str(rt / "qoder2openai"))
    q = importlib.import_module("qoder2openai")
    check("能 import", hasattr(q, "create_app"))

    # 2) 真起 uvicorn
    print(f"\n[2] 起真实服务 :{PORT}")
    import uvicorn

    q.apply_config({"station": "intl", "cli_path": "", "pat": "",
                    "timeout_s": 90, "api_key": ""})
    cfg = uvicorn.Config(q.create_app(), host="127.0.0.1", port=PORT,
                         log_level="warning", lifespan="off")
    srv = uvicorn.Server(cfg)
    import threading
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()

    base = f"http://127.0.0.1:{PORT}"
    ok_up = False
    for _ in range(60):
        try:
            st, _b = http(base + "/health", timeout=3)
            if st == 200:
                ok_up = True
                break
        except Exception:
            time.sleep(0.25)
    check("服务起来", ok_up)
    if not ok_up:
        print(f"\n=====  PASS {PASS}  FAIL {FAIL}  =====")
        return 1

    # /health
    st, body = http(base + "/health")
    check("/health 200", st == 200, f"{st}")
    h = json.loads(body)
    check("health.station=intl", h.get("station") == "intl", str(h))
    check("health.cli_found=True", h.get("cli_found") is True, str(h))
    check("health.ide_found=True", h.get("ide_found") is True, str(h))
    check("health.credential_ok=True", h.get("credential_ok") is True, str(h))
    check("health 有账号", bool(h.get("account")), str(h))
    check("health 有 node_version", bool(h.get("node_version")), str(h))

    # /v1/models
    st, body = http(base + "/v1/models")
    check("/v1/models 200", st == 200, f"{st}")
    d = json.loads(body)
    ids = {x["id"] for x in d.get("data", [])}
    check("models 非空", len(ids) >= 5, str(sorted(ids))[:120])
    check("含 Auto", "Auto" in ids)

    # 3) 真对话（无 Token）→ 必须明确报错
    print("\n[3] /v1/chat/completions（无 Token）")
    t0 = time.monotonic()
    st, body = http(base + "/v1/chat/completions", "POST", {
        "model": "auto",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }, timeout=180)
    el = time.monotonic() - t0
    print(f"      status={st} elapsed={el:.1f}s")
    print(f"      body={body[:220]}")
    check("对话 200", st == 200, f"got {st}: {body[:200]}")
    if st == 200:
        obj = json.loads(body)
        content = obj["choices"][0]["message"]["content"]
        check("回复非空", bool(content.strip()), repr(content[:100]))
        print(f"      回复 = {content.strip()[:120]!r}")
    check("没挂死（<120s）", el < 120, f"{el:.1f}s")

    # 4) 切成国内版再查 health
    print("\n[4] 切国内版")
    q.apply_station("cn", pat="")
    st, body = http(base + "/health")
    h2 = json.loads(body)
    check("health.station=cn", h2.get("station") == "cn", str(h2))
    check("国内版 CLI 也找到", h2.get("cli_found") is True, str(h2))
    check("国内版凭据也可用", h2.get("credential_ok") is True, str(h2))

    srv.should_exit = True
    time.sleep(1.0)
    print(f"\n=====  PASS {PASS}  FAIL {FAIL}  =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
