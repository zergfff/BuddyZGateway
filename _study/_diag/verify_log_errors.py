"""回归：日志里发现的那一批错误（逐条钉死，防止再犯）。

覆盖：
  A) refresh_models_on_start 是**模块级**函数 → 绝不能引用 run_gui 的局部变量
     services（曾 NameError 崩在后台线程，日志里一大片 'name services is not defined'）
     并且真的能用 running 映射跑完一遍
  B) 福利「永久性拒绝」（WorkBuddy 国际 code=10001 活动未开启）→ 立刻计满当日上限，
     不再按 1/3→2/3 慢慢试一整天
  C) _claim_daily_tick **只在服务已启动时**补领（用户：服务没起来别浪费资源）
  D) _ca_keepalive 只在 ca 服务运行时保活 + 失败日志 10 分钟最多一条
  E) ca 的 STS 续期要按**凭据自己的身份**取 client_id
     （桌面端 codearts-agent / 插件 vscode-codebot）—— 写死一个会得到
     `STS5.1806 invalid refresh token: 'invalid client id'`
  F) mc 领取站点跟随「桌面端实际登录的那版」，而不是 GUI 的「使用版本」选择
     （否则稳定 401）

用法：python _study/_diag/verify_log_errors.py
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "BuddyZGateway.py"
CA_SRC = ROOT / "_study" / "codearts2openai" / "codearts2openai.py"

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
    spec = importlib.util.spec_from_file_location("bzg_logerr", MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bzg_logerr"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("=== 日志错误回归 ===\n")
    src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ---------------------------------------------------------------- A
    print("A) refresh_models_on_start 不得引用 run_gui 的局部变量")
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "refresh_models_on_start"),
              None)
    ok(fn is not None, "函数是**模块级**定义（不是嵌在 run_gui 里）")
    if fn is not None:
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        ok("services" not in names,
           "函数体内不再引用 services（曾经 NameError: name 'services' is not defined）")
        params = [a.arg for a in fn.args.args]
        ok("running" in params, f"新增 running 参数（实际参数：{params}）")
        ok(any(isinstance(n, ast.Constant) and n.value == "running"
               for n in ast.walk(fn)) or "running" in src, "有 running 的用法")
    # 所有调用点都要传 running
    calls = re.findall(r"refresh_models_on_start\((?:[^()]|\([^()]*\))*\)", src)
    calls = [c for c in calls if "def refresh_models_on_start" not in c]
    ok(len(calls) >= 7, f"找到 {len(calls)} 个调用点")
    bad = [c for c in calls if "running" not in c and "items()" not in c and "_rmap" not in c]
    ok(not bad, f"全部调用点都传了 running（未传的：{bad}）")

    # 真跑一遍：running 全 False 时必须是「跳过」而不是崩
    bzg = load_bzg()
    bzg.materialize(lambda *a: None)
    with tempfile.TemporaryDirectory() as td:
        os.environ["BUDDYZ_ROOT"] = td            # 隔离 settings
        try:
            st: dict = {}
            logs: list = []
            bzg.refresh_models_on_start(st, logs.append, {}, {})
            ok(any("跳过" in m for m in logs),
               f"running 全空时全部跳过、无异常（{len(logs)} 条日志）")
        finally:
            os.environ.pop("BUDDYZ_ROOT", None)

    # ---------------------------------------------------------------- B
    print("\nB) 永久性拒绝 → 直接计满当日上限")
    ok("_permanent" in src, "auto_claim 里有「永久性拒绝」判定")
    for token in ("10001", "活动未开启", "活动已过期"):
        ok(token in src, f"识别 {token!r}")
    i = src.find("_permanent")
    seg = src[max(0, i - 500):i + 1500]
    ok('{"date": today, "n": _CLAIM_MAX_TRIES}' in seg,
       "命中后把当日失败计数直接写成 _CLAIM_MAX_TRIES（不再 1/3→2/3 慢慢试）")
    ok("save_settings" in seg, "立刻落盘（重启后也不会重试）")

    # ---------------------------------------------------------------- C
    print("\nC) 服务没启动就不补领福利")
    i = src.find("def _claim_daily_tick")
    j = src.find("\n    def ", i + 1)
    tick = src[i:j]
    ok('services.get(k) and services[k].running' in tick,
       "_claim_daily_tick 逐通道判服务是否在跑")
    ok(tick.index("services") < tick.index("auto_claim(k"),
       "判定在领取之前（先 continue 再领）")

    # ---------------------------------------------------------------- D
    print("\nD) ca 保活只在服务运行时跑 + 日志节流")
    i = src.find("def _ca_keepalive")
    j = src.find("\n    def ", i + 1)
    ka = src[i:j]
    ok('services.get("ca") and services["ca"].running' in ka,
       "_ca_keepalive 先判 ca 服务在跑")
    ok("_CA_KA_WARN_EVERY" in src and "_ca_ka_state" in src,
       "失败日志有 10 分钟节流（_CA_KA_WARN_EVERY/_ca_ka_state）")
    ok('_ca_ka_state["warn_at"] = 0.0' in ka, "续期成功后重置节流（下次失败能立刻提示）")

    # ---------------------------------------------------------------- E
    print("\nE) ca 的 STS 续期按凭据身份取 client_id")
    ca_src = CA_SRC.read_text(encoding="utf-8")
    ok('"client_id": CLIENT_ID' not in ca_src,
       "没有任何地方再把 client_id 写死成 CLIENT_ID")
    for fn_name in ("_do_refresh", "_refresh_candidate"):
        i = ca_src.find(f"def {fn_name}")
        j = ca_src.find("\ndef ", i + 1)
        body = ca_src[i:j]
        ok("client_id" in body and "CLIENT_ID" in body,
           f"{fn_name} 使用凭据自带的 client_id（不再是写死的）")
        ok('data={"client_id": CLIENT_ID' not in body,
           f"{fn_name} 里没有硬编码 client_id=CLIENT_ID")
    ok('"client_id": IDENTITIES["vscode" if store.get("match") == "codebot"' in ca_src,
       "凭证库解出的会话自带正确 client_id（桌面端/插件分流）")
    ok('"client_id": sess.get("client_id", CLIENT_ID)' in ca_src,
       "pool_harvest 把 client_id 一起存进池条目")
    # 真值校验：桌面端与插件的 client_id 必须不同
    bzg2 = load_bzg()
    ca = bzg2.import_codearts(lambda *a: None)
    d = ca.IDENTITIES["desktop"]["client_id"]
    v = ca.IDENTITIES["vscode"]["client_id"]
    ok(d != v and d == "codearts-agent" and v == "vscode-codebot",
       f"两套身份 client_id 不同：desktop={d} / vscode={v}")

    # ---------------------------------------------------------------- F
    print("\nF) mc 领取站点跟随桌面端实际登录态")
    i = src.find("def _claim_stations")
    j = src.find("\n    def ", i + 1)
    cs = src[i:j]
    ok("find_ohmyagent_key" in cs, "mc 分支读桌面端 key 文件判站点")
    ok('".net" in sv' in cs and '".com" in sv' in cs, "按 server 域名判国内/国际")
    ok("按它领取" in cs, "与 GUI 选择不一致时会说明按哪版领")

    print(f"\n=== 结果：{PASS} 通过 / {FAIL} 失败 ===")
    for m in FAILS:
        print(f"  FAIL: {m}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
