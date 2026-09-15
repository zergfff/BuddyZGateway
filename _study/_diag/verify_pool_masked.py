# -*- coding: utf-8 -*-
"""验证「号池功能屏蔽」（POOL_FEATURE=False）真的生效。

做法是**投毒**：临时数据目录里写一份把号池全部打开的 settings.json
（mc/ca 的 pool_enabled 都是 true，还塞了池条目），然后：
  1) 静态检查总开关存在且为 False
  2) 起 GUI（--uismoke）→ 断言 UI 里**找不到**任何"号池"入口、两个开关恒 False
  3) 起 --serve（无头）→ 查 /health，断言 pool.enabled 被强制关闭
  4) 断言引擎代码仍在（恢复只需把 POOL_FEATURE 改成 True，不是删代码）

用法：python verify_pool_masked.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BZG = REPO / "BuddyZGateway.py"
RESULTS: list = []


def chk(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra else ""))
    return cond


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def prep_env(tmp: Path):
    """建隔离的 LOCALAPPDATA + 投毒 settings.json。"""
    root = tmp / "BuddyZGateway"
    (root / "data").mkdir(parents=True, exist_ok=True)
    ports = {k: free_port() for k in
             ("cb_port", "mc_port", "ca_port", "rc_port", "lo_port")}
    settings = dict(ports)
    settings.update({
        # ↓↓↓ 投毒：把号池全部打开并塞条目 ↓↓↓
        "mc_pool_enabled": True,
        "mc_pool": [{"base_url": "https://poison.mc/v1", "api_key": "POISON-KEY",
                     "enabled": True, "label": "毒条1"}],
        "mc_pool_cfg": {"strategy": "weighted"},
        "ca_pool_enabled": True,
        "ca_pool_cfg": {"strategy": "least_busy"},
        "autostart": False,
    })
    (root / "data" / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(tmp)          # 整个 app_root 被重定向到临时目录
    return env, ports


def test_static():
    print("########## 1) 静态：总开关存在且为 False ##########")
    src = BZG.read_text(encoding="utf-8")
    chk("源码定义 POOL_FEATURE = False", "POOL_FEATURE = False" in src)
    chk("引擎文件仍在（恢复=改 1 行，不是删代码）",
        (REPO / "_study" / "buddyzpool.py").exists())

    print("\n########## 1b) AST：每处 pool_enabled 赋值都必须有守卫 ##########")
    import ast
    tree = ast.parse(src)
    assigns = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            t = n.targets[0]
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "pool_enabled"):
                seg = (ast.get_source_segment(src, n) or "").replace("\n", " ")
                assigns.append(seg)
    chk("确实找到 pool_enabled 赋值点", len(assigns) >= 3, f"{len(assigns)} 处")
    # 关键风险：**直接读 settings 文件**的那些（绕过 GUI 变量）必须带 POOL_FEATURE
    from_settings = [s for s in assigns if "_s.get(" in s or "settings.get(" in s]
    chk("存在直读 settings 的赋值点（--serve 路径）", len(from_settings) >= 2,
        f"{len(from_settings)} 处")
    unguarded = [s for s in from_settings if "POOL_FEATURE" not in s]
    chk("直读 settings 的赋值全部带 POOL_FEATURE 守卫", not unguarded,
        str(unguarded)[:120])
    # GUI 路径：从 BooleanVar 取值（变量在 POOL_FEATURE=False 时恒为 False）
    others = [s for s in assigns if s not in from_settings]
    var_based = [s for s in others if "pool_on.get()" in s]
    const_based = [s for s in others if s.rstrip().endswith("True")]
    unexplained = [s for s in others if s not in var_based and s not in const_based]
    chk("GUI 赋值点要么取 pool_on 变量、要么是对话框常量",
        not unexplained, str(unexplained)[:130])
    chk("对话框里的常量赋值受 POOL_FEATURE 早退保护（第二道防线）",
        "if not POOL_FEATURE:" in src and "不提供管理界面" in src)


def test_gui(tmp: Path):
    print("\n########## 2) GUI：投毒 settings 下 UI 不应出现号池入口 ##########")
    env, _ = prep_env(tmp)
    r = subprocess.run([sys.executable, "-u", str(BZG), "--uismoke"], env=env,
                       capture_output=True, text=True, timeout=240)
    out = (r.stdout or "") + (r.stderr or "")
    chk("--uismoke 退出码 0", r.returncode == 0, f"rc={r.returncode}")
    chk("UI 里找不到号池入口", "UISMOKE POOL OFF (UI 无号池入口)" in out,
        [ln for ln in out.splitlines() if "POOL" in ln][:1])
    chk("没有泄漏告警（若 UI 里出现“号池”就是漏屏蔽）",
        "UISMOKE POOL LEAK" not in out)
    chk("两个开关恒为 False（即便 settings 里是 true）",
        "UISMOKE POOL VARS mc=False ca=False" in out)
    chk("屏蔽下对话框代码仍可构建（不腐烂）",
        "UISMOKE DIALOG OK mc" in out and "UISMOKE DIALOG OK ca" in out)
    chk("--uismoke 总体 OK", "UISMOKE OK" in out)


def _deps_ok() -> tuple:
    """本机是否具备跑 --serve 依赖（缺 pystray 等时 --serve 会卡在装依赖上）。"""
    import importlib.util
    need = ("fastapi", "uvicorn", "httpx", "pystray")
    missing = [m for m in need if importlib.util.find_spec(m) is None]
    return (not missing), missing


def test_serve(tmp: Path):
    print("\n########## 3) --serve（无头）：pool.enabled 必须被强制关闭 ##########")
    ok_deps, missing = _deps_ok()
    if not ok_deps:
        print(f"  ⤵ SKIP（本机缺 {missing}，--serve 会卡在自动装依赖；"
              f"该路径已由 1b 的 AST 守卫断言覆盖）")
        return
    env, ports = prep_env(tmp)
    logf = tmp / "serve.log"
    with open(logf, "w", encoding="utf-8") as fh:
        p = subprocess.Popen([sys.executable, "-u", str(BZG), "--serve"], env=env,
                             stdout=fh, stderr=subprocess.STDOUT)
    try:
        def health(port, tries=60):
            for _ in range(tries):
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except Exception:  # noqa: BLE001
                    time.sleep(0.25)
            return None

        h = health(ports["mc_port"])
        chk("mc /health 可用", isinstance(h, dict))
        if isinstance(h, dict):
            chk("mc pool.enabled 被强制关闭",
                not (h.get("pool") or {}).get("enabled"),
                f"pool={str(h.get('pool'))[:70]}")
        hc = health(ports["ca_port"])
        chk("ca /health 可用", isinstance(hc, dict))
        if isinstance(hc, dict):
            chk("ca pool.enabled 被强制关闭",
                not (hc.get("pool") or {}).get("enabled"),
                f"pool={str(hc.get('pool'))[:70]}")
    finally:
        p.kill()
        try:
            p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def test_engine_alive(tmp: Path):
    print("\n########## 4) 引擎与通道接线仍在（可随时恢复） ##########")
    sys.path.insert(0, str(REPO / "_study"))
    import buddyzpool
    chk("buddyzpool 引擎可导入", hasattr(buddyzpool, "PoolEngine"))
    os.environ["BUDDYZ_DATA_DIR"] = str(tmp / "mods")
    sys.path.insert(0, str(REPO / "_study" / "monkeycode2openai"))
    import monkeycode2openai as mc
    chk("mc 仍提供 pool_probe/pool_targets/pool_set_config",
        all(hasattr(mc, n) for n in ("pool_probe", "pool_targets", "pool_set_config")))
    sys.path.insert(0, str(REPO / "_study" / "codearts2openai"))
    import codearts2openai as ca
    chk("ca 仍提供 pool_probe/pool_targets/pool_set_config",
        all(hasattr(ca, n) for n in ("pool_probe", "pool_targets", "pool_set_config")))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pool_mask_"))
    print(f"临时 LOCALAPPDATA: {tmp}\n")
    test_static()
    test_gui(tmp)
    test_serve(tmp)
    test_engine_alive(tmp)
    print("\n" + "=" * 60)
    bad = RESULTS.count(False)
    print(f"RESULT: {'ALL PASS' if bad == 0 else f'FAIL ({bad} 项)'}  "
          f"({len(RESULTS)} 项断言)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
