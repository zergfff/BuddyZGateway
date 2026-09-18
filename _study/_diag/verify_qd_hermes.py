"""回归：Qoder「程序位置」= IDE exe + Hermes 集成包含 Qoder。

覆盖：
  A) qd_path_as_exe()：CLI 脚本(.mjs) → IDE exe；已是 exe 原样保留；
     探测不到时**不把好值抹掉**；发生迁移时会落盘
  B) hermes_entries()：六个通道都在，端口/字段正确，buddyz-qd 的 key_env 规则正确
  C) hermes_sync()：**尊重 hermes_sync_<key> 勾选**（未勾选的通道既不写入、
     还会被清掉），写进临时 profile 后能往返读回，.env 占位 key 也写了
  D) 真机检查（只读）：本机 Hermes config.yaml 里确实有 buddyz-qd
  E) 源码不变量：`--hermes-sync` 必须读真实 settings（不能走 uismoke 的临时数据根，
     否则勾选全部退回默认 True —— Loomy 未勾选却被同步的坑）

用法：python _study/_diag/verify_qd_hermes.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "BuddyZGateway.py"

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
    spec = importlib.util.spec_from_file_location("bzg_qdh", MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bzg_qdh"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("=== Qoder exe 路径 + Hermes 集成回归 ===\n")
    bzg = load_bzg()
    src = MAIN.read_text(encoding="utf-8")

    # ------------------------------------------------------------------ A
    print("A) qd_path_as_exe()：程序位置必须是 IDE exe")
    fake_cli = (r"C:\Program Files\Qoder CN\resources\app.asar.unpacked"
                r"\node_modules\@qoder-ai\qoder-cn-agent-sdk\dist\_worker"
                r"\qoder-worker-runtime.obf.mjs")
    st = {"qd_path_cn": fake_cli}
    out = bzg.qd_path_as_exe(st, "cn")
    ok(out.lower().endswith(".exe") and "worker" not in out.lower(),
       f"CLI 脚本 → exe（{out}）")
    ok(st["qd_path_cn"] == out, "迁移结果写回了 settings 字典")
    # 已是 exe 原样保留（不能把用户手选的路径换掉）
    myexe = r"C:\Tools\MyQoder.exe"
    ok(bzg.qd_path_as_exe({"qd_path_cn": myexe}, "cn") == myexe,
       "已是 .exe 的值原样保留（不覆盖用户手选）")
    # 目录 → exe
    d = {"qd_path_cn": r"C:\Program Files\Qoder CN"}
    ok(bzg.qd_path_as_exe(d, "cn").lower().endswith(".exe"), "目录 → exe")
    # 迁移会调用 save
    calls = []
    bzg.qd_path_as_exe({"qd_path_cn": fake_cli}, "cn", save=lambda s: calls.append(1))
    ok(len(calls) == 1, "发生迁移时调用了 save_settings")
    # 探测不到时保留原值（别抹掉）
    saved = bzg.find_install
    bzg.find_install = lambda k, s=None: ""
    try:
        keep = {"qd_path_cn": "X:/nope.txt"}
        ok(bzg.qd_path_as_exe(keep, "cn") == "X:/nope.txt",
           "探测不到 exe 时保留原值（不清空）")
    finally:
        bzg.find_install = saved

    # ------------------------------------------------------------------ B
    print("\nB) hermes_entries()：六个通道都在，字段正确")
    st2 = {"qd_port": 9500, "qd_models": ["Qwen3.8-Flash"]}
    ent = bzg.hermes_entries(st2, "127.0.0.1")
    want = {"buddyz-cb", "buddyz-mc", "buddyz-ca",
            "buddyz-rc", "buddyz-lo", "buddyz-qd"}
    ok(want <= set(ent), f"providers 含全部 6 个（{sorted(ent)}）")
    exp_ports = {"buddyz-cb": 8787, "buddyz-mc": 9000, "buddyz-ca": 9100,
                 "buddyz-rc": 9200, "buddyz-lo": 9400, "buddyz-qd": 9500}
    bad = [p for p in want
           if ent[p]["base_url"] != f"http://127.0.0.1:{exp_ports[p]}/v1"]
    ok(not bad, f"每个 provider 的 base_url 端口正确（异常：{bad}）")
    q = ent["buddyz-qd"]
    ok(q["name"] == "buddyz-qd" and q["discover_models"] is True
       and q["model"] == "Qwen3.8-Flash" and q["models"].get("Qwen3.8-Flash") == {},
       "buddyz-qd 的 name/discover_models/model/models 正确")
    ok(q["key_env"] == bzg._buddyz_key_env("buddyz-qd")
       == "HERMES_CUSTOM_BUDDYZ_QD_API_KEY", f"key_env = {q['key_env']}")
    ok(all(ent[p]["key_env"] == bzg._buddyz_key_env(p) for p in want),
       "所有 key_env 与官方命名规则一致")

    # ------------------------------------------------------------------ C
    print("\nC) hermes_sync()：尊重勾选 + 往返写读")
    with tempfile.TemporaryDirectory(prefix="bzg_hermes_") as td:
        home = Path(td) / "hermes"
        home.mkdir(parents=True, exist_ok=True)
        # 预置一个"上一轮遗留"的 buddyz-lo，验证未勾选时会被清掉
        bzg.write_hermes_config(
            {"providers": {"buddyz-lo": {"name": "buddyz-lo"},
                           "keepme": {"name": "keepme"}}},
            home / "config.yaml")
        saved_homes = bzg.hermes_profile_homes
        bzg.hermes_profile_homes = lambda: [home]      # 测试缝：只写临时 profile
        try:
            s3 = {"qd_port": 9500, "qd_models": ["Qwen3.8-Flash"],
                  "hermes_sync_cb": True, "hermes_sync_mc": True,
                  "hermes_sync_ca": True, "hermes_sync_rc": True,
                  "hermes_sync_lo": False,      # ← 用户未勾选
                  "hermes_sync_qd": True}
            logs = []
            r = bzg.hermes_sync(s3, logs.append)
            back = bzg.read_hermes_config(home / "config.yaml") or {}
            prov = back.get("providers") or {}
            ok("buddyz-qd" in prov, "buddyz-qd 已写入")
            ok("buddyz-lo" not in prov, "未勾选的 buddyz-lo 被清除（尊重用户选择）")
            ok("buddyz-cb" in prov and "buddyz-rc" in prov, "已勾选的通道都在")
            ok("keepme" in prov, "非 buddyz-* 的 provider 不受影响")
            ok("Loomy" in " ".join(r.get("skipped") or [])
               or "Loomy" in " ".join(logs), "日志里说明了跳过 Loomy")
            env = (home / ".env").read_text(encoding="utf-8")
            ok("HERMES_CUSTOM_BUDDYZ_QD_API_KEY" in env, ".env 写了 qd 的占位 key")
            # 二次同步幂等：不重复追加 key
            bzg.hermes_sync(s3, logs.append)
            env2 = (home / ".env").read_text(encoding="utf-8")
            ok(env2.count("HERMES_CUSTOM_BUDDYZ_QD_API_KEY") == 1,
               ".env key 幂等（重复同步不重复追加）")
        finally:
            bzg.hermes_profile_homes = saved_homes

    # ------------------------------------------------------------------ D
    print("\nD) 真机检查（只读）：本机 Hermes 配置")
    homes = bzg.hermes_profile_homes()
    ok(bool(homes), f"找到 {len(homes)} 个 profile：{[h.name for h in homes]}")
    found_qd = False
    for h in homes:
        cfg = bzg.read_hermes_config(h / "config.yaml") or {}
        if "buddyz-qd" in (cfg.get("providers") or {}):
            found_qd = True
            url = cfg["providers"]["buddyz-qd"].get("base_url")
            ok(url == "http://127.0.0.1:9500/v1", f"{h.name}: buddyz-qd base_url={url}")
    ok(found_qd, "本机 Hermes config.yaml 里确实有 buddyz-qd")

    # ------------------------------------------------------------------ E
    print("\nE) 源码不变量")
    i = src.find('"--hermes-sync" in args')
    j = src.find('if "--serve" in args')
    seg = src[i:j] if i > 0 and j > i else ""
    ok("load_settings()" in seg, "--hermes-sync 读真实 settings")
    ok("hermes_sync(" in seg, "--hermes-sync 调用共享核心 hermes_sync()")
    # GUI 的条目构造必须委托模块级核心（否则 schema 两套会漂移）
    ok("return hermes_entries(_s, host)" in src,
       "GUI 的 _buddyz_hermes_entries 委托模块级 hermes_entries（无重复 schema）")
    ok(src.count('"discover_models": True') == 1,
       f"providers schema 只定义一处（'\"discover_models\": True' 出现 "
       f"{src.count(chr(34) + 'discover_models' + chr(34) + ': True')} 次）")
    # GUI 必须暴露 Qoder 勾选框
    ok('text="Qoder", variable=hr_sync_qd' in src, "Hermes 面板有 Qoder 勾选框")
    ok('"hermes_sync_qd": bool(hr_sync_qd.get())' in src, "勾选状态会落盘")

    print(f"\n=== 结果：{PASS} 通过 / {FAIL} 失败 ===")
    for m in FAILS:
        print(f"  FAIL: {m}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
