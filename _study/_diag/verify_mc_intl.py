# -*- coding: utf-8 -*-
"""verify_mc_intl — MonkeyCode 国际版/国内版识别回归。

背景
----
MonkeyCode 桌面端是**同一个 GUI**，国际版与国内版在同一目录、同一文件里切换：

    %APPDATA%\\com.chaitin.baizhi.monkeycode\\monkeycode-ohmyagent-key.json

    国内：{"server": "https://monkeycode-ai.com",
            "base_url": "https://proxy.monkeycode-ai.com/v1", ...}
    国际：{"server": "https://monkeycode-ai.net",
            "base_url": "https://proxy.monkeycode-ai.net/v1", ...}

（桌面端 exe 里两个 proxy 端点就是紧挨着硬编码的，切换时重写该文件。）

因此凡按 base_url 过滤「哪些条目属于官方代理」的地方都**不能写死 .com**：
写死会让国际版整张模型协议表为空 → 所有模型错走 /responses →
上游 404「路由不存在」（表现就是"国际版用不了"）。

本脚本在临时 APPDATA 里各造一棵 .com / .net 配置树，逐个跑
`auto_configure()`，检查：base_url、模型协议表（含 anthropic 分流）、
以及 mc_saas 的站点解析。

用法：
    python verify_mc_intl.py            # 校验当前 _study 源码（应全 PASS）
    python verify_mc_intl.py --old      # 复现修复前行为（把过滤条件换回 .com）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent           # _study/_diag/
REPO = HERE.parent.parent                        # 仓库根
RUNTIME = REPO / "_study" / "monkeycode2openai"

PROXY_COM = "https://proxy.monkeycode-ai.com/v1"
PROXY_NET = "https://proxy.monkeycode-ai.net/v1"
SITE_COM = "https://monkeycode-ai.com"
SITE_NET = "https://monkeycode-ai.net"

# 与真实桌面端 ohmyagent/settings.json 同形：
# 两条 anthropic + 一条 openai-responses，另加一条第三方 baizhi 干扰项。
SETTINGS_TMPL = {
    "default_model": "monkeycode-basic/qwen3.8-flash",
    "models": {
        "monkeycode-basic/qwen3.8-flash@monkeycode#d4fd": {
            "api_key": "oma_TEST", "base_url": "@PROXY@",
            "model": "monkeycode-basic/qwen3.8-flash",
            "supports_images": True, "type": "openai-responses",
        },
        "monkeycode-basic/deepseek-flash@monkeycode#c5a1": {
            "api_key": "oma_TEST", "base_url": "@PROXY@",
            "model": "monkeycode-basic/deepseek-flash",
            "supports_images": True, "type": "anthropic",
        },
        "monkeycode-basic/glm-5.3-flash@monkeycode#c790": {
            "api_key": "oma_TEST", "base_url": "@PROXY@",
            "model": "monkeycode-basic/glm-5.3-flash",
            "supports_images": True, "type": "anthropic",
        },
        # 干扰项：第三方 baizhi 端点，绝不该进协议表
        "glm-5@baizhi": {
            "api_key": "sk-x",
            "base_url": "https://ai-models.app.baizhi.cloud/api/anthropic",
            "model": "glm-5", "type": "anthropic",
        },
    },
    "signing_secret": "omas_TEST",
}

# 子进程探针：跑 auto_configure 并把结论打成一行 JSON
PROBE = r'''
import json, sys
sys.path.insert(0, r"{runtime}")
import monkeycode2openai as mc
logs = []
mc.auto_configure(log=lambda s: logs.append(s))
types = mc.CONFIG.get("model_types") or {{}}
print("RESULT" + json.dumps({{
    "mode": mc.CONFIG.get("mode"),
    "base_url": mc.CONFIG.get("base_url"),
    "types": types,
    "proto_deepseek": mc._protocol_for("monkeycode-basic/deepseek-flash"),
    "proto_qwen": mc._protocol_for("monkeycode-basic/qwen3.8-flash"),
    "default_model": mc.CONFIG.get("default_model"),
    "basic_models": mc._basic_models(),
}}, ensure_ascii=False))
'''

# 站点解析探针（mc_saas 跟随 server 字段）
# 必须判定 _req() 实际用哪个 base，而不是直接调 current_base_api()——修复前的
# 代码里 _req 走的是模块常量 BASE_API，直接调函数会把 bug 掩盖掉。
PROBE_SITE = r'''
import inspect, json, sys
sys.path.insert(0, r"{runtime}")
import mc_saas
src = inspect.getsource(mc_saas._req)
dynamic = "current_base_api()" in src
print("SITE" + json.dumps({{
    "dynamic": dynamic,
    "effective": mc_saas.current_base_api() if dynamic else mc_saas.BASE_API,
}}))
'''


def make_tree(proxy: str, site: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mc_intl_"))
    d = tmp / "com.chaitin.baizhi.monkeycode"
    (d / "ohmyagent").mkdir(parents=True, exist_ok=True)
    (d / "monkeycode-ohmyagent-key.json").write_text(json.dumps({
        "api_key": "oma_TESTKEY", "base_url": proxy, "server": site,
        "signing_secret": "omas_TESTSECRET", "transport": "deadbeef",
    }), encoding="utf-8")
    (d / "config.json").write_text(json.dumps({"models": [
        {"api_key": "", "base_url": "", "model": "monkeycode-basic/qwen3.8-flash"},
        {"api_key": "", "base_url": "", "model": "monkeycode-basic/deepseek-flash"},
        {"api_key": "sk-x",
         "base_url": "https://ai-models.app.baizhi.cloud/api/anthropic",
         "model": "glm-5"},
    ]}), encoding="utf-8")
    (d / "ohmyagent" / "settings.json").write_text(
        json.dumps(SETTINGS_TMPL, ensure_ascii=False).replace("@PROXY@", proxy),
        encoding="utf-8")
    return tmp


def _run(code: str, env: dict, marker: str):
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env)
    if p.returncode != 0:
        return None, p.stderr[-600:]
    for ln in p.stdout.splitlines():
        if ln.startswith(marker):
            return ln[len(marker):], ""
    return None, "no marker line: " + p.stdout[-300:]


def run_case(runtime: Path, proxy: str, site: str) -> dict:
    tmp = make_tree(proxy, site)
    try:
        env = dict(os.environ)
        env["APPDATA"] = str(tmp)
        env.pop("MC2_OPENAI_BASE_URL", None)
        env.pop("MC2_OPENAI_API_KEY", None)

        raw, err = _run(PROBE.format(runtime=str(runtime)), env, "RESULT")
        if err:
            return {"err": err}
        out = json.loads(raw)

        site_raw, serr = _run(PROBE_SITE.format(runtime=str(runtime)), env, "SITE")
        if serr:
            out["site"] = f"ERR {serr[:200]}"
            out["site_dynamic"] = None
        else:
            s = json.loads(site_raw)
            out["site"] = s.get("effective")
            out["site_dynamic"] = s.get("dynamic")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", action="store_true",
                    help="复现修复前行为：把过滤条件换回只认 .com")
    args = ap.parse_args()

    runtime = RUNTIME
    restore = None
    if args.old:
        # 复原两处修复前写法：
        #   monkeycode2openai.py  过滤条件写死 proxy.monkeycode-ai.com
        #   mc_saas.py            站点写死 https://monkeycode-ai.com
        reverts = {
            "monkeycode2openai.py": [(
                'if not _is_mc_proxy(str(v.get("base_url") or "")):',
                'if "proxy.monkeycode-ai.com" not in str(v.get("base_url") or ""):')],
            "mc_saas.py": [("    base = current_base_api()",
                            "    base = DEFAULT_BASE_API")],
        }
        tmpd = Path(tempfile.mkdtemp(prefix="mc_old_rt_"))
        for f in RUNTIME.iterdir():
            if f.suffix != ".py":
                continue
            text = f.read_text(encoding="utf-8")
            for old_s, new_s in reverts.get(f.name, []):
                if old_s not in text:
                    print(f"!! {f.name}: 未找到可替换片段，--old 无法忠实复现")
                    shutil.rmtree(tmpd, ignore_errors=True)
                    return 2
                text = text.replace(old_s, new_s)
            (tmpd / f.name).write_text(text, encoding="utf-8")
        runtime, restore = tmpd, tmpd
        print("== 修复前模拟（过滤条件写死 .com + 站点写死 .com）==")

    cases = (("国内版 .com", PROXY_COM, SITE_COM),
             ("国际版 .net", PROXY_NET, SITE_NET))
    results = {}
    ok = True
    try:
        for tag, proxy, site in cases:
            out = run_case(runtime, proxy, site)
            results[tag] = out
            print(f"\n=== {tag} ===")
            if "err" in out:
                ok = False
                print("   ERROR:", out["err"])
                continue
            types = out["types"]
            print(f"  mode            : {out['mode']}")
            print(f"  base_url        : {out['base_url']}")
            print(f"  协议表({len(types)})     : {types}")
            print(f"  deepseek 走协议 : {out['proto_deepseek']}")
            print(f"  qwen3.8 走协议  : {out['proto_qwen']}")
            print(f"  basic_models    : {out['basic_models']}")
            print(f"  mc_saas 站点    : {out['site']}"
                  f"{'' if out.get('site_dynamic') else '  (写死，不跟随版本)'}")

            checks = {
                "base_url 指向对应版本": out["base_url"] == proxy,
                "协议表 3 条（baizhi 未混入）": len(types) == 3
                and not any("baizhi" in k for k in types),
                "anthropic 模型走 /messages": out["proto_deepseek"] == "anthropic"
                and out["proto_qwen"] == "responses",
                "mc_saas 站点跟随版本": out["site"] == site
                and out.get("site_dynamic") is True,
            }
            for name, good in checks.items():
                if not good:
                    ok = False
                print(("  ✓ " if good else "  ✗ ") + name)
    finally:
        if restore is not None:
            shutil.rmtree(restore, ignore_errors=True)

    print("\n" + "=" * 60)
    if args.old:
        # 修复前：国际版协议表必为空（这就是 bug 的判据）
        intl = results.get("国际版 .net", {})
        reproduced = (not intl.get("types"))
        print("修复前行为复现：", "YES（国际版协议表为空 → 全错走 /responses）"
              if reproduced else "NO（未复现，请检查源码）")
        return 0 if reproduced else 1
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
