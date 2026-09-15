# -*- coding: utf-8 -*-
"""验证「选了国内就真的走国内」—— 版本切换必须立刻生效。

背景：用户选「国内」后仍走国际。原因有两个，都在这里钉住：
  1) 切换回调只是打了句"重启后生效"，**没有把新站点推给运行中的服务**；
  2) 状态行算"当前生效版本"时，auto 的推测逻辑优先级压过了用户的选择。

本脚本用**假 auth 文件**（国内/国际各一份）离线验证 converter.apply_station()：
切 cn → 凭据文件必须是 workbuddy-desktop.info、后端必须是 copilot.tencent.com。

不发任何网络请求（WorkBuddy 易封号）。
用法：python verify_station_switch_live.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS: list = []


def chk(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra else ""))
    return cond


def make_auth(p: Path, domain: str, nick: str, age_s: float = 0.0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "account": {"uid": f"uid-{nick}", "nickname": nick},
        "auth": {"accessToken": "FAKE-" + nick, "refreshToken": "FAKE-R-" + nick,
                 "domain": domain,
                 "expiresAt": int(time.time() * 1000) + 3_600_000},
    }), encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cb_switch_"))
    # converter.auth_dirs() 在 Windows 读的是 **LOCALAPPDATA**（不是 APPDATA），
    # 必须覆盖它，否则读到的是本机真实登录文件（齐增路/qzlsdb）。
    localapp = tmp / "Local"
    auth = localapp / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    # 国际版 mtime 更新（模拟"最近活跃是国际"）—— 若实现里 auto 优先级压过
    # 用户选择，选国内时会错拿国际，正好被这条数据抓到。
    make_auth(auth / "workbuddy-desktop.info", "www.workbuddy.cn", "国内用户", age_s=600)
    make_auth(auth / "workbuddy-desktop-ai.info", "www.workbuddy.ai", "国际用户", age_s=10)
    os.environ["LOCALAPPDATA"] = str(localapp)
    print(f"假 LOCALAPPDATA: {localapp}\n")

    sys.path.insert(0, str(REPO / "_study" / "codebuddy2openai"))
    import converter as cv

    print("########## 1) 显式选「国内」必须走国内（即使国际文件更新）##########")
    s = cv.apply_station("cn")
    chk("凭据文件 = 国内文件", s.get("auth_file") == "workbuddy-desktop.info",
        str(s.get("auth_file")))
    chk("domain = www.workbuddy.cn", s.get("domain") == "www.workbuddy.cn",
        str(s.get("domain")))
    chk("后端 = copilot.tencent.com", s.get("backend") == "https://copilot.tencent.com",
        str(s.get("backend")))
    chk("账号 = 国内用户", s.get("nickname") == "国内用户", str(s.get("nickname")))

    print("\n########## 2) 显式选「国际」必须走国际 ##########")
    s = cv.apply_station("intl")
    chk("凭据文件 = 国际文件", s.get("auth_file") == "workbuddy-desktop-ai.info",
        str(s.get("auth_file")))
    chk("后端 = www.codebuddy.ai", s.get("backend") == "https://www.codebuddy.ai",
        str(s.get("backend")))
    chk("账号 = 国际用户", s.get("nickname") == "国际用户", str(s.get("nickname")))

    print("\n########## 3) 回到自动：取 mtime 最新（应为国际）##########")
    s = cv.apply_station("auto")
    chk("auto 取最近活跃（国际）", s.get("auth_file") == "workbuddy-desktop-ai.info",
        str(s.get("auth_file")))
    chk("auto 时 CONFIG['station'] 为 None", cv.CONFIG.get("station") is None,
        str(cv.CONFIG.get("station")))

    print("\n########## 4) 切换后 CONFIG['cred'] 真的换了（服务无需重启）##########")
    cv.apply_station("cn")
    cred_cn = cv.CONFIG["cred"]
    cv.apply_station("intl")
    cred_intl = cv.CONFIG["cred"]
    chk("两次 cred 是不同对象", cred_cn is not cred_intl)
    chk("cn 的 cred 指向国内文件",
        Path(cred_cn.path).name == "workbuddy-desktop.info", Path(cred_cn.path).name)
    chk("intl 的 cred 指向国际文件",
        Path(cred_intl.path).name == "workbuddy-desktop-ai.info",
        Path(cred_intl.path).name)

    print("\n########## 5) 无效值应安全回落为 auto（不崩、不乱选）##########")
    s = cv.apply_station("bogus")
    chk("无效值 → station=None", cv.CONFIG.get("station") is None,
        str(cv.CONFIG.get("station")))
    chk("仍能取到凭据", s.get("nickname") in ("国内用户", "国际用户"), str(s.get("nickname")))

    print("\n" + "=" * 62)
    bad = RESULTS.count(False)
    print(f"RESULT: {'ALL PASS' if bad == 0 else f'FAIL ({bad} 项)'}  "
          f"({len(RESULTS)} 项断言)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
