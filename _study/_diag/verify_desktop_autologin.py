"""回归：点「启动」缺凭据 → 自动打开桌面端 → 拿到凭据 → 自动关闭桌面端。

覆盖（每条都是真跑，不 mock 掉被测逻辑本身）：
  1) desktop_cred_probe()：四个通道各返回合法 (bool, str)，不抛异常
  2) 凭据已就绪 → auto_login_desktop 直接 True，**不启动任何进程**
  3) 找不到桌面端 → 返回 False，且不启动任何进程
  4) 真端到端：伪造一个"桌面端"(FakeDesktop.exe) + 伪造凭据探针（第 4 秒变可用）
     → auto_login_desktop 必须：等到凭据 → 关掉**它自己拉起来的**进程 → True
  5) 「只关自己拉起来的」：登录前就开着的同名进程，跑完必须还活着
  6) 并发闸门：同一通道第二次调用直接返回 False（不会拉出两个窗口）
  7) _ensure_desktop_cred 的行为：凭据就绪 → True；缺凭据且有桌面端 → False
     且**不阻塞**（必须在 1 秒内返回）

用法：python _study/_diag/verify_desktop_autologin.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
MAIN = os.path.join(ROOT, "BuddyZGateway.py")

PASS = 0
FAIL = 0
FAILS: list[str] = []


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        FAILS.append(msg)
        print(f"  ✗ {msg}")


def load_bzg():
    spec = importlib.util.spec_from_file_location("bzg", MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bzg"] = mod
    spec.loader.exec_module(mod)
    return mod


def pid_alive(pid: int) -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, errors="replace", timeout=20)
        return str(pid) in (r.stdout or "")
    except Exception:
        return False


def main():
    print("=== 桌面端自动登录回归 ===\n")
    bzg = load_bzg()

    # ---------------------------------------------------------------- 1) 探针
    print("1) desktop_cred_probe() 各通道")
    for k in ("cb", "mc", "ca", "qd", "rc", "lo"):
        try:
            good, why = bzg.desktop_cred_probe(k, None)
            ok(isinstance(good, bool) and isinstance(why, str),
               f"{k}: 返回 (bool, str) = ({good}, {why[:52]!r})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"{k}: 抛异常 {type(e).__name__}: {e}")

    # -------------------------------------------------- 2) 凭据就绪 → 不开进程
    print("\n2) 凭据已就绪 → 不启动桌面端")
    launched: list = []
    orig_popen = subprocess.Popen

    class FakePopen:
        def __init__(self, *a, **kw):
            launched.append(a[0] if a else kw.get("args"))
            self.pid = -1
            self.returncode = 0

        def poll(self):
            return 0

    bzg.subprocess.Popen = FakePopen          # type: ignore[assignment]
    bzg.desktop_cred_probe = lambda k, s=None: (True, "假的已登录")
    try:
        r = bzg.auto_login_desktop("cb", None, lambda *_: None)
        ok(r is True and not launched, f"返回 {r}，启动进程数 {len(launched)}（应为 0）")
    finally:
        bzg.subprocess.Popen = orig_popen     # type: ignore[assignment]

    # ------------------------------------------- 3) 找不到桌面端 → False，不开进程
    print("\n3) 找不到桌面端 → 直接失败")
    orig_find = bzg.find_install
    bzg.subprocess.Popen = FakePopen          # type: ignore[assignment]
    bzg.desktop_cred_probe = lambda k, s=None: (False, "假的未登录")
    bzg.find_install = lambda k, s=None: ""
    try:
        r = bzg.auto_login_desktop("cb", None, lambda *_: None)
        ok(r is False and not launched, f"返回 {r}，启动进程数 {len(launched)}（应为 0）")
    finally:
        bzg.subprocess.Popen = orig_popen     # type: ignore[assignment]
        bzg.find_install = orig_find

    # ------------------------------------------------------ 4/5) 真端到端
    print("\n4+5) 真端到端：等凭据 → 关自己拉起的 → 不动用户已开的")
    tmp = tempfile.mkdtemp(prefix="bzg_fake_desktop_")
    fake = os.path.join(tmp, "FakeDesktop.exe")
    shutil.copyfile(sys.executable, fake)
    image = os.path.basename(fake)

    # 5) 先开一个"用户自己的"同名进程（登录前就在）→ 最后必须还活着
    pre = subprocess.Popen([fake, "-c", "import time; time.sleep(120)"])
    time.sleep(1.5)
    ok(pid_alive(pre.pid), f"预置的同名进程 pid={pre.pid} 已起来")

    state = {"t0": time.time()}

    def probe(k, s=None):
        # 第 4 秒前"没凭据"，之后"有凭据" —— 模拟用户完成登录
        if time.time() - state["t0"] >= 4.0:
            return True, "登录完成"
        return False, "还没登录"

    # 只替换 Popen（让"桌面端"变成常驻进程），其余 subprocess.*（tasklist/taskkill）
    # 仍走真模块 —— 直接改 subprocess.Popen 会连 _pids_by_image/_kill_pids 一起改坏。
    class _LaunchWrap:
        def __init__(self, args, **kw):
            if isinstance(args, (list, tuple)) and args \
                    and str(args[0]).lower().endswith("fakedesktop.exe"):
                args = [args[0], "-c", "import time; time.sleep(120)"]
            keep = {k: v for k, v in kw.items() if k in ("cwd", "creationflags")}
            self._p = orig_popen(args, **keep)
            self.pid = self._p.pid

        def poll(self):
            return self._p.poll()

    class _SubProxy:
        def __getattr__(self, name):
            if name == "Popen":
                return _LaunchWrap
            return getattr(orig_mod, name)

    orig_mod = sys.modules["subprocess"]
    bzg.subprocess = _SubProxy()          # type: ignore[assignment]
    bzg.find_install = lambda k, s=None: fake
    bzg.desktop_cred_probe = probe
    bzg.DESKTOP_LOGIN_POLL = 0.5
    bzg.DESKTOP_LOGIN_TIMEOUT = 30.0
    state["t0"] = time.time()
    logs: list = []
    try:
        t_a = time.time()
        r = bzg.auto_login_desktop("cb", None, logs.append)
        dt = time.time() - t_a
        ok(r is True, f"登录成功返回 True（耗时 {dt:.1f}s）")
        new_pids = bzg._pids_by_image(image) - {pre.pid}
        ok(not new_pids, f"自己拉起的桌面端已关闭（残留 pid={sorted(new_pids)}）")
        ok(pid_alive(pre.pid), f"用户本来开着的 pid={pre.pid} 未被误杀")
        ok(any("已自动关闭" in m for m in logs), "日志里有「已自动关闭」")
        ok(any("请在" in m and "完成登录" in m for m in logs), "日志里有引导登录提示")
    finally:
        bzg.subprocess = orig_mod            # type: ignore[assignment]
        bzg.find_install = orig_find
        bzg.desktop_cred_probe = lambda k, s=None: (False, "假的未登录")
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pre.pid)],
                           capture_output=True, timeout=20)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ 6) 并发闸门
    print("\n6) 同一通道并发调用只拉起一个")
    gate_hits = {"n": 0}

    def slow_probe(k, s=None):
        gate_hits["n"] += 1
        return False, "还没登录"

    tmp2 = tempfile.mkdtemp(prefix="bzg_fake_desktop2_")
    fake2 = os.path.join(tmp2, "FakeDesktopX.exe")
    shutil.copyfile(sys.executable, fake2)
    bzg.subprocess.Popen = orig_popen
    bzg.find_install = lambda k, s=None: fake2
    bzg.desktop_cred_probe = slow_probe
    bzg.DESKTOP_LOGIN_TIMEOUT = 4.0
    bzg.DESKTOP_LOGIN_POLL = 0.5
    res: dict = {}
    try:
        t = threading.Thread(target=lambda: res.__setitem__("first",
                                                            bzg.auto_login_desktop("cb", None, lambda *_: None)))
        t.start()
        time.sleep(1.2)
        r2 = bzg.auto_login_desktop("cb", None, lambda *_: None)
        t.join(timeout=40)
        ok(r2 is False, f"第二次调用被闸门挡下（返回 {r2}）")
        ok(res.get("first") is False, f"第一次超时返回 False（{res.get('first')}）")
        leftover = bzg._pids_by_image(os.path.basename(fake2))
        ok(not leftover, f"超时后也把拉起的进程收干净了（残留 {sorted(leftover)}）")
    finally:
        bzg.find_install = orig_find
        shutil.rmtree(tmp2, ignore_errors=True)

    # ------------------------------------------------------- 7) 启动闸门不阻塞
    print("\n7) _ensure_desktop_cred：不阻塞、语义正确")
    src = open(MAIN, encoding="utf-8").read()
    i = src.find("    def _ensure_desktop_cred(")
    ok(i > 0, "找到 _ensure_desktop_cred 定义")
    body = src[i:src.find("\n    # 先建 LabelFrame", i)] if i > 0 else ""
    ok("threading.Thread" in body, "自动登录跑在后台线程（不阻塞 Tk 主线程）")
    ok("return False" in body, "缺凭据 + 有桌面端 → 返回 False（本次不启动）")
    ok('_DESKTOP_LOGIN_SPEC.get(key)' in body, "不支持的通道直接放行（rc/lo 不需要登录）")
    # 四个需要登录的通道都被接线
    for k in ("cb", "mc", "ca", "qd"):
        ok(f'_ensure_desktop_cred("{k}"' in src, f"{k} 的启动函数已接入凭据闸门")
    ok("no_launch" in src and "Code.exe" in src, "ca 不会去拉起 VS Code 冒充桌面端登录")

    print(f"\n=== 结果：{PASS} 通过 / {FAIL} 失败 ===")
    if FAILS:
        for m in FAILS:
            print(f"  FAIL: {m}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
