# -*- coding: utf-8 -*-
"""验证三通道「使用版本」切换（国内/国际）真的作用到了核心。

  1) MonkeyCode：站点快照（两版共存）+ apply_station + mc_saas 钱包/签到跟随
  2) CodeArts ：CONFIG["station"] 强制 portal/snap/STS 站点
  3) WorkBuddy：见 verify_cb_dual_station.py（auth 文件按站点精确选择）

全程**只读/只写临时目录**，不发任何网络请求（WorkBuddy 易封号，一并避免）。
用法：python verify_station_switch.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS: list = []


def chk(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra else ""))
    return cond


def fresh(mods: tuple, paths: list):
    for m in [m for m in sys.modules if m in mods]:
        del sys.modules[m]
    for p in paths:
        if p not in sys.path:
            sys.path.insert(0, p)


# --------------------------------------------------------------------------
def test_mc(tmp: Path):
    print("\n########## 1) MonkeyCode：站点快照 + 切换 ##########")
    appdata = tmp / "Roaming"
    cfg = appdata / "com.chaitin.baizhi.monkeycode"
    cfg.mkdir(parents=True, exist_ok=True)
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(appdata)
    os.environ["BUDDYZ_DATA_DIR"] = str(data)

    # 当前桌面端登录的是【国内版】
    (cfg / "monkeycode-ohmyagent-key.json").write_text(json.dumps({
        "api_key": "oma_cn_FAKE", "signing_secret": "omas_cn_FAKE",
        "base_url": "https://proxy.monkeycode-ai.com/v1",
        "server": "https://monkeycode-ai.com",
    }), encoding="utf-8")

    fresh(("monkeycode2openai", "mc_saas", "buddyzpool"),
          [str(REPO / "_study"), str(REPO / "_study" / "monkeycode2openai")])
    import monkeycode2openai as mc

    logs: list = []
    mc.auto_configure(log=logs.append)
    chk("auto_configure 认到国内站点",
        mc.CONFIG["base_url"].endswith("monkeycode-ai.com/v1"), mc.CONFIG["base_url"])
    snaps = mc.load_accounts()
    chk("自动记录下【国内】凭据快照", "cn" in snaps,
        f"快照站点={list(snaps)}")
    chk("快照内容含 base_url/api_key/server",
        bool(snaps.get("cn", {}).get("api_key")) and bool(snaps["cn"].get("base_url")),
        str({k: (v[:12] + "…" if isinstance(v, str) and len(v) > 12 else v)
             for k, v in snaps.get("cn", {}).items()}))

    print("\n  ── 手动塞一份【国际】快照（模拟用户已在国际版登录过）──")
    acc = mc.load_accounts()
    acc["intl"] = {"base_url": "https://proxy.monkeycode-ai.net/v1",
                   "api_key": "oma_intl_FAKE", "signing_secret": "omas_intl_FAKE",
                   "server": "https://monkeycode-ai.net"}
    (data / "mc_accounts.json").write_text(json.dumps(acc), encoding="utf-8")

    chk("apply_station('intl') 返回 True", mc.apply_station("intl", logs.append) is True)
    chk("切到国际：base_url 用国际快照",
        mc.CONFIG["base_url"].endswith("monkeycode-ai.net/v1"), mc.CONFIG["base_url"])
    chk("切到国际：api_key 也换成国际那套",
        mc.CONFIG["api_key"] == "oma_intl_FAKE", mc.CONFIG["api_key"])
    chk("切到国际：signing_secret 同步",
        mc.CONFIG["signing_secret"] == "omas_intl_FAKE")

    import mc_saas
    chk("钱包/签到站点同步为 intl（避免对话走国际、钱包走国内）",
        mc_saas.STATION == "intl" and mc_saas.current_base_api() == "https://monkeycode-ai.net",
        f"STATION={mc_saas.STATION} base={mc_saas.current_base_api()}")

    chk("apply_station('cn') 返回 True", mc.apply_station("cn", logs.append) is True)
    chk("切回国内：base_url/api_key 都是国内那套",
        mc.CONFIG["base_url"].endswith("monkeycode-ai.com/v1")
        and mc.CONFIG["api_key"] == "oma_cn_FAKE", mc.CONFIG["base_url"])
    chk("钱包/签到站点同步回 cn",
        mc_saas.current_base_api() == "https://monkeycode-ai.com")

    chk("切回自动：不再强制站点", mc.apply_station("auto", logs.append) is True
        and mc.CONFIG["station"] is None)
    import mc_saas as _s2
    chk("自动模式钱包跟随文件（读回 .com）",
        _s2.STATION is None and _s2.current_base_api() == "https://monkeycode-ai.com",
        _s2.current_base_api())

    print("\n  ── 没有该站点快照时：明确报错而不是静默用错凭据 ──")
    acc.pop("intl", None)
    (data / "mc_accounts.json").write_text(json.dumps(acc), encoding="utf-8")
    ok = mc.apply_station("intl", logs.append)
    chk("缺快照 → 返回 False 且给出可操作提示",
        ok is False and any("还没有该站点的快照" in m for m in logs),
        logs[-1][:78] if logs else "")


def test_ca(tmp: Path):
    print("\n########## 2) CodeArts：强制站点（portal/snap 全套跟随）##########")
    fresh(("codearts2openai", "buddyzpool"),
          [str(REPO / "_study"), str(REPO / "_study" / "codearts2openai")])
    import codearts2openai as ca

    ca.CONFIG["station"] = "cn"
    chk("station=cn → _current_station()=china", ca._current_station() == "china")
    chk("station=cn → snap 走 cn-north-4",
        "cn-north-4" in ca._snap(), ca._snap())
    chk("station=cn → portal 走 codearts.huaweicloud.com",
        ca._portal().endswith("codearts.huaweicloud.com"), ca._portal())

    ca.CONFIG["station"] = "intl"
    chk("station=intl → _current_station()=international",
        ca._current_station() == "international")
    chk("station=intl → snap 走 ap-southeast-1",
        "ap-southeast-1" in ca._snap(), ca._snap())
    chk("station=intl → portal 走 ap-southeast-1",
        "ap-southeast-1" in ca._portal(), ca._portal())

    ca.CONFIG["station"] = None
    auto = ca._current_station()
    chk("station=None → 自动判定仍然可用（不崩）",
        auto in ("china", "international"), auto)

    print("\n  ── 两站点端点必须完全不同（否则就会互相冲突）──")
    ca.CONFIG["station"] = "cn"
    snap_cn, portal_cn = ca._snap(), ca._portal()
    ca.CONFIG["station"] = "intl"
    snap_intl, portal_intl = ca._snap(), ca._portal()
    chk("snap 端点国内≠国际", snap_cn != snap_intl, f"{snap_cn} vs {snap_intl}")
    chk("portal 端点国内≠国际", portal_cn != portal_intl, f"{portal_cn} vs {portal_intl}")
    ca.CONFIG["station"] = None


def test_authorize_url_per_station(tmp: Path):
    print("\n########## 3) CodeArts 授权 URL 也要按站点（否则国际账号跳国内门户）##########")
    import codearts2openai as ca
    u_cn = ca.build_authorize_url(9100, "china")["url"]
    u_intl = ca.build_authorize_url(9100, "international")["url"]
    chk("国内授权 URL 打国内门户", "codearts.huaweicloud.com" in u_cn
        and "ap-southeast-1" not in u_cn)
    chk("国际授权 URL 打国际门户", "ap-southeast-1" in u_intl)
    chk("国际站带 quickcompfalg=1", "quickcompfalg=1" in u_intl)
    chk("两者互不相同", u_cn != u_intl)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="station_sw_"))
    print(f"临时目录: {tmp}")
    test_mc(tmp)
    test_ca(tmp)
    test_authorize_url_per_station(tmp)
    print("\n" + "=" * 62)
    bad = RESULTS.count(False)
    print(f"RESULT: {'ALL PASS' if bad == 0 else f'FAIL ({bad} 项)'}  ({len(RESULTS)} 项断言)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
