# -*- coding: utf-8 -*-
"""验证 ca OAuth 换票的**轮询**语义（对齐华为云官方 VS Code 插件）。

背景：portal 重定向回本地回调那一刻，ticket 常常尚未就绪。官方插件
（WebLoginStrategy.queryAuth）是 setInterval 每 2s 重试、最长 3 分钟，
期间 400 `无效ticketId`(TM.00001001) 属于"还没就绪"而不是失败。
本模块原先只换一次就报错 —— 也就是用户实际遇到的
「ticket 换票失败(400): {'error_code': 'TM.00001001', 'error_msg': '无效ticketId: ...'}」。

用法：
    python verify_ca_ticket_poll.py            # 验证修复后：
                                                 #   轮询重试 / 致命码立即终止 /
                                                 #   超时 / 回调不阻塞浏览器
    python verify_ca_ticket_poll.py --repro-old  # 复现修复前：首个 400 即失败
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "_study"))
sys.path.insert(0, str(REPO / "_study" / "codearts2openai"))

RESULTS: list = []


def chk(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra else ""))
    return cond


# --------------------------------------------------------------------------
# 假上游：/snap-manager/v1/login/ticket
#   fail_first 次返回 400 无效ticketId，之后返回 200 credential
#   若 fatal_code 设置，则一直返回该 error_code
# --------------------------------------------------------------------------
class FakeSnap:
    def __init__(self, fail_first=3, fatal_code=None):
        self.fail_first = fail_first
        self.fatal_code = fatal_code
        self.hits = 0
        self.seen_ids: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                outer.seen_ids.append(self.path)
                if outer.fatal_code:
                    payload = {"error_code": outer.fatal_code,
                               "error_msg": "code=" + outer.fatal_code}
                    code = 400
                elif outer.hits <= outer.fail_first:
                    payload = {"error_code": "TM.00001001",
                               "error_msg": "无效ticketId: "
                                            + ("b5" * 32)[:64]}
                    code = 400
                else:
                    payload = {"credential": {
                        "access": "AK-FAKE", "secret": "SK-FAKE",
                        "securitytoken": "ST-FAKE", "token": "TK-FAKE",
                        "expires_at": "2026-12-31T00:00:00Z"}}
                    code = 200
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json;charset=UTF-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def load_ca(tmp: Path):
    os.environ["BUDDYZ_DATA_DIR"] = str(tmp)
    for m in [m for m in sys.modules if m.startswith("codearts2openai")]:
        del sys.modules[m]
    import codearts2openai as ca
    return ca


# --------------------------------------------------------------------------
def test_repro_old(tmp: Path) -> int:
    """--repro-old：模拟修复前的"只换一次"。"""
    print("########## 复现修复前（单次换票） ##########")
    ca = load_ca(tmp)
    srv = FakeSnap(fail_first=3)
    try:
        ca._snap = lambda station=None: srv.base
        cred, code, msg = ca._ticket_get("t" * 64, "SECRET", "china")
        chk("首次换票收到 400 无效ticketId", cred is None and code == "TM.00001001",
            f"code={code}")
        chk("修复前行为：只试 1 次就放弃（上游只被打了 1 次）",
            srv.hits == 1, f"hits={srv.hits}")
        chk("→ 这就是用户看到的报错：换票失败(400) 无效ticketId",
            cred is None)
    finally:
        srv.stop()
    return 0


def test_poll_ok(tmp: Path) -> int:
    print("\n########## 1) 轮询重试：第 1..3 次 400，第 4 次成功 ##########")
    ca = load_ca(tmp)
    ca.TICKET_POLL_INTERVAL = 0.05        # 测试加速（语义不变）
    ca.TICKET_POLL_TIMEOUT = 30.0
    srv = FakeSnap(fail_first=3)
    try:
        ca._snap = lambda station=None: srv.base
        t0 = time.time()
        cred = ca._exchange_ticket("a" * 64, "SECRET", "china",
                                   log=lambda m: print("    log:", m))
        el = time.time() - t0
        chk("最终换到了凭证", isinstance(cred, dict) and cred.get("access") == "AK-FAKE",
            str(cred)[:70])
        chk("上游确实被打到第 4 次才成功", srv.hits == 4, f"hits={srv.hits}")
        chk("重试有间隔（不是死循环猛打）", el >= 3 * 0.05, f"{el:.2f}s")
    finally:
        srv.stop()

    print("\n########## 2) AUTH.9022 是致命错：立即终止，不重试 ##########")
    srv2 = FakeSnap(fatal_code="AUTH.9022")
    try:
        ca._snap = lambda station=None: srv2.base
        err = ""
        try:
            ca._exchange_ticket("b" * 64, "SECRET", "china")
        except RuntimeError as e:
            err = str(e)
        chk("抛错且消息含 AUTH.9022", "AUTH.9022" in err, err[:70])
        chk("只打了一次就放弃（不浪费时间重试）", srv2.hits == 1, f"hits={srv2.hits}")
    finally:
        srv2.stop()

    print("\n########## 3) 一直不就绪 → 到超时点才放弃，并给出清晰错误 ##########")
    srv3 = FakeSnap(fail_first=10 ** 9)
    try:
        ca._snap = lambda station=None: srv3.base
        ca.TICKET_POLL_TIMEOUT = 0.3
        err = ""
        t0 = time.time()
        try:
            ca._exchange_ticket("c" * 64, "SECRET", "china")
        except RuntimeError as e:
            err = str(e)
        el = time.time() - t0
        chk("超时报错含“超时”与最后一次原因",
            "超时" in err and "TM.00001001" in err, err[:90])
        chk("在超时窗口内结束（未无限轮询）", 0.3 <= el <= 3.0, f"{el:.2f}s")
        chk("确实重试了多次", srv3.hits >= 3, f"hits={srv3.hits}")
    finally:
        srv3.stop()
    return 0


def test_callback_nonblocking(tmp: Path) -> int:
    """回调必须立即返回（后台线程去轮询），且最终把凭证存进 state。"""
    print("\n########## 4) 回调不阻塞浏览器 + 后台轮询最终落盘 ##########")
    import socket
    import urllib.request

    ca = load_ca(tmp)
    ca.TICKET_POLL_INTERVAL = 0.1
    ca.TICKET_POLL_TIMEOUT = 20.0
    srv = FakeSnap(fail_first=5)          # 故意让前 5 次失败
    try:
        ca._snap = lambda station=None: srv.base
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        ca.CONFIG["port"] = port
        tid = "d" * 64
        ca._OAUTH_PENDING[tid] = {"verifier": "v", "at": time.time(),
                                  "station": "china"}

        import uvicorn
        cfg = uvicorn.Config(ca.app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(cfg)
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        for _ in range(100):
            if getattr(server, "started", False):
                break
            time.sleep(0.05)
        chk("测试用 uvicorn 已就绪", getattr(server, "started", False))

        # 回调：必须**秒回**，绝不等待 3 分钟轮询
        t0 = time.time()
        url = (f"http://127.0.0.1:{port}/oauth/callback?secret=SECRET-XYZ"
               f"&ticket_id={tid}")
        with urllib.request.urlopen(url, timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
        el = time.time() - t0
        chk("回调立即返回（<2s，不阻塞浏览器）", el < 2.0, f"{el:.2f}s")
        chk("页面告知正在后台换取凭证", "正在换取凭证" in html, html[:60].strip())

        # 后台轮询应最终成功并落盘
        ok = False
        for _ in range(120):
            st = ca._load_state()
            tk = st.get("ticket") or {}
            if tk.get("access") == "AK-FAKE":
                ok = True
                break
            time.sleep(0.1)
        chk("后台轮询换到凭证并写入 state.ticket", ok,
            str((ca._load_state().get("ticket") or {}))[:70])
        chk("换票进度已记录", (ca._AUTH_EXCHANGE.get("state") == "ok"),
            str(ca._AUTH_EXCHANGE))

        # /v1/auth/status 应暴露 exchange 进度
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/auth/status",
                                    timeout=10) as r:
            js = json.loads(r.read().decode())
        chk("/v1/auth/status 暴露 exchange 进度", "exchange" in js,
            str(js.get("exchange"))[:80])
        server.should_exit = True
        time.sleep(0.2)
    finally:
        srv.stop()
    return 0


def test_authorize_url(tmp: Path) -> int:
    print("\n########## 5) 授权 URL 与官方插件的参数一致 ##########")
    ca = load_ca(tmp)
    info = ca.build_authorize_url(9100, "china")
    url = info["url"]
    for p in ("auth_callback_url=", "plugin-name=", "plugin-version=",
              "code_challenge_method=S256", "ticket_id=",
              "auth_callback_url=http%3A%2F%2F127.0.0.1%3A9100%2Foauth%2Fcallback"):
        chk(f"含 {p[:34]}", p in url)
    chk("国内站不带 quickcompfalg", "quickcompfalg" not in url)
    intl = ca.build_authorize_url(9100, "international")["url"]
    chk("国际站带 quickcompfalg=1（插件同款）", "quickcompfalg=1" in intl)
    chk("国际站 portal 域名正确", "ap-southeast-1" in intl, intl[:58])
    return 0


def main() -> int:
    args = set(sys.argv[1:])
    tmp = Path(tempfile.mkdtemp(prefix="ca_ticket_"))
    print(f"临时数据目录: {tmp}\n")
    if "--repro-old" in args:
        test_repro_old(tmp)
    else:
        test_repro_old(tmp)
        test_poll_ok(tmp)
        test_callback_nonblocking(tmp)
        test_authorize_url(tmp)
    print("\n" + "=" * 60)
    bad = RESULTS.count(False)
    print(f"RESULT: {'ALL PASS' if bad == 0 else f'FAIL ({bad} 项)'}  ({len(RESULTS)} 项断言)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
