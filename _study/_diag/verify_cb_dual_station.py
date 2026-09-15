# -*- coding: utf-8 -*-
"""验证 WorkBuddy 国内/国际**双版本共存**的凭据选择逻辑。

覆盖：
  1) find_auth_file("cn"/"intl") 精确取各自文件；auto 取 mtime 最新
  2) **回归旧 bug**：绝不靠 sorted(glob) 选文件
     （`workbuddy-desktop-ai.2026-…Z.<uuid>.info` 因 '-'<' .' 会排到前面，
      曾让程序选到"已登出账号的旧备份"）
  3) backend_host 域名映射：.ai → codebuddy.ai，.cn → copilot.tencent.com
  4) CredentialManager 报出正确 station / backend / auth_file
  5) list_stations() 反映两版登录可用性
  6) GUI 侧的 find_cb_auth_file / read_cb_summary（从 BuddyZGateway.py 抽函数执行）

全程**只读本地假 auth 文件**，不发任何网络请求（WorkBuddy 易封号）。
用法：python verify_cb_dual_station.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "_study" / "codebuddy2openai"))

RESULTS: list = []


def chk(name, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra else ""))
    return cond


def make_auth(dirp: Path, fname: str, domain: str, nick: str, age_s: float = 0.0):
    """造一个最小可用的 auth 文件（token 是假值，永不出网）。"""
    p = dirp / fname
    data = {
        "account": {"uid": f"uid-{nick}", "nickname": nick, "enterpriseName": "-"},
        "auth": {"accessToken": "FAKE-TOKEN-" + nick,
                 "refreshToken": "FAKE-REFRESH-" + nick,
                 "domain": domain,
                 "expiresAt": int(time.time() * 1000) + 3_600_000,
                 "refreshExpiresAt": int(time.time() * 1000) + 7_200_000},
    }
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if age_s:                      # 把 mtime 拨老，用于测 auto 选最新
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


def build_fake_env(tmp: Path) -> Path:
    """造一个假的 LOCALAPPDATA，含国内+国际两个登录 + 一个时间戳备份。"""
    d = tmp / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    d.mkdir(parents=True, exist_ok=True)
    make_auth(d, "workbuddy-desktop.info", "www.workbuddy.cn", "cnUser", age_s=600)
    make_auth(d, "workbuddy-desktop-ai.info", "www.workbuddy.ai", "intlUser", age_s=10)
    make_auth(d, "workbuddy-desktop-ai.2026-09-14T13-38-59-332Z.9160.abc.info",
              "www.workbuddy.ai", "staleBackup", age_s=99999)
    os.environ["LOCALAPPDATA"] = str(tmp)
    return d


# --------------------------------------------------------------------------
def test_core(d: Path):
    print("########## 1) converter 核心：站点精确选择 ##########")
    import converter as cv

    p_cn = cv.find_auth_file("cn")
    p_intl = cv.find_auth_file("intl")
    chk("station='cn' → workbuddy-desktop.info",
        p_cn is not None and p_cn.name == "workbuddy-desktop.info",
        str(p_cn.name if p_cn else None))
    chk("station='intl' → workbuddy-desktop-ai.info",
        p_intl is not None and p_intl.name == "workbuddy-desktop-ai.info",
        str(p_intl.name if p_intl else None))

    print("\n########## 2) auto 取最近活跃（mtime 最新） ##########")
    cv.CONFIG["station"] = None
    p_auto = cv.find_auth_file()
    chk("auto 选中国际版（mtime 更新：10s vs 600s）",
        p_auto is not None and p_auto.name == "workbuddy-desktop-ai.info",
        str(p_auto.name if p_auto else None))

    print("\n########## 3) 回归旧 bug：绝不能选到时间戳备份 ##########")
    chk("选中的永不是备份文件（名字里带时间戳）",
        "2026-09-14T" not in (p_auto.name if p_auto else ""),
        p_auto.name if p_auto else "")
    # 直接验证旧算法的确会选错（证明本测试有意义）
    old_pick = sorted(d.glob("*.info"))[0].name
    chk("旧 sorted(glob)[0] 确实会选到备份（证明这个测试有价值）",
        "2026-09-14T" in old_pick, f"旧算法选中: {old_pick[:44]}…")

    print("\n########## 4) backend 必须跟随账号 domain ##########")
    chk("  .cn → copilot.tencent.com",
        cv.backend_host("www.workbuddy.cn") == "https://copilot.tencent.com")
    chk("  .ai → www.codebuddy.ai",
        cv.backend_host("www.workbuddy.ai") == "https://www.codebuddy.ai")
    chk("  空 domain 兜底国内",
        cv.backend_host("") == "https://copilot.tencent.com")

    print("\n########## 5) CredentialManager 报出 station/backend/auth_file ##########")
    for st, want_dom, want_back in (
            ("cn", "www.workbuddy.cn", "https://copilot.tencent.com"),
            ("intl", "www.workbuddy.ai", "https://www.codebuddy.ai")):
        af = cv.find_auth_file(st)
        s = cv.CredentialManager(af).summary()
        chk(f"[{st}] domain/station/backend 一致",
            s.get("station") == st and s.get("domain") == want_dom
            and s.get("backend") == want_back,
            f"station={s.get('station')} backend={s.get('backend')}")

    print("\n########## 6) list_stations() ##########")
    avail = cv.list_stations()
    chk("两版都报可用", avail.get("cn") and avail.get("intl"), str(avail))

    print("\n########## 7) station_of_file 反查 ##########")
    chk("由 cn 文件反查 = cn", cv.station_of_file(p_cn) == "cn")
    chk("由 intl 文件反查 = intl", cv.station_of_file(p_intl) == "intl")
    chk("备份文件反查 = None（不误判）",
        cv.station_of_file(d / "workbuddy-desktop-ai.2026-09-14T13-38-59-332Z.9160.abc.info") is None)


def test_gui_helpers(d: Path):
    """把 BuddyZGateway.py 里的 find_cb_auth_file / read_cb_summary 抽出来跑。"""
    print("\n########## 8) GUI 侧 find_cb_auth_file / read_cb_summary ##########")
    src = (REPO / "BuddyZGateway.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"find_cb_auth_file", "read_cb_summary"}
    ns: dict = {"Path": Path, "os": os, "json": json, "time": time}
    found = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            exec(compile(ast.Module(body=[n], type_ignores=[]), "<x>", "exec"), ns)
            found.add(n.name)
    chk("成功抽取两个函数", found == want, str(sorted(found)))

    f = ns["find_cb_auth_file"]
    r = ns["read_cb_summary"]
    chk("GUI find('cn') → 国内文件", (f("cn") or Path()).name == "workbuddy-desktop.info")
    chk("GUI find('intl') → 国际文件", (f("intl") or Path()).name == "workbuddy-desktop-ai.info")
    chk("GUI find(None) 兼容旧调用（优先国际再退回国内）",
        f(None) is not None and "ai" in f(None).name, str(f(None).name))

    sc = r("cn")
    si = r("intl")
    chk("GUI summary(cn): 昵称/domain/station 正确",
        sc.get("nickname") == "cnUser" and sc.get("domain") == "www.workbuddy.cn"
        and sc.get("station") == "cn", str({k: sc.get(k) for k in ("nickname", "domain", "station")}))
    chk("GUI summary(intl): 昵称/domain/station 正确",
        si.get("nickname") == "intlUser" and si.get("domain") == "www.workbuddy.ai"
        and si.get("station") == "intl", str({k: si.get(k) for k in ("nickname", "domain", "station")}))
    chk("GUI summary 不再误选备份账号（旧行为会返回 staleBackup）",
        sc.get("nickname") != "staleBackup" and si.get("nickname") != "staleBackup")


def test_missing_station(d: Path):
    print("\n########## 9) 某站点未登录时的行为 ##########")
    import converter as cv
    gone = d / "workbuddy-desktop.info"
    bak = gone.read_bytes()
    gone.unlink()
    try:
        chk("cn 未登录 → find_auth_file('cn') 返回 None",
            cv.find_auth_file("cn") is None)
        chk("cn 未登录 → intl 仍可用",
            (cv.find_auth_file("intl") or Path()).name == "workbuddy-desktop-ai.info")
        chk("list_stations：cn=False intl=True",
            cv.list_stations() == {"cn": False, "intl": True},
            str(cv.list_stations()))
        # GUI 侧同样应报告 found=False
        src = (REPO / "BuddyZGateway.py").read_text(encoding="utf-8")
        ns: dict = {"Path": Path, "os": os, "json": json, "time": time}
        for n in ast.parse(src).body:
            if isinstance(n, ast.FunctionDef) and n.name in ("find_cb_auth_file", "read_cb_summary"):
                exec(compile(ast.Module(body=[n], type_ignores=[]), "<x>", "exec"), ns)
        chk("GUI：cn 未登录时 read_cb_summary('cn') 报 found=False",
            ns["read_cb_summary"]("cn").get("found") is False,
            str(ns["read_cb_summary"]("cn")))
    finally:
        gone.write_bytes(bak)


def test_install_detect(dummy: Path):
    """真机探测：找得到国内版(WorkBuddy.exe) 与 国际版(WorkBuddyAI.exe) 两个安装。

    这是**静态文件系统探测**，不发任何网络请求。
    """
    print("\n########## 10) 真机安装探测（两版应各找到各自的 exe） ##########")
    src = (REPO / "BuddyZGateway.py").read_text(encoding="utf-8")
    ns: dict = {"os": os, "Path": Path, "sys": sys}
    want_assign = {"_INSTALL_PROFILES", "WB_STATION_EXES", "WB_STATION_LABEL"}
    want_func = {"_kind_profiles", "_kind_exe_names", "_scan_dirs_for",
                 "_scan_registry_for", "find_install"}
    for n in ast.parse(src).body:
        tgt = None
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            tgt = n.targets[0]
        elif isinstance(n, ast.AnnAssign):      # 带类型注解的赋值也算
            tgt = n.target
        if isinstance(tgt, ast.Name) and tgt.id in want_assign:
            exec(compile(ast.Module(body=[n], type_ignores=[]), "<x>", "exec"), ns)
        elif isinstance(n, ast.FunctionDef) and n.name in want_func:
            exec(compile(ast.Module(body=[n], type_ignores=[]), "<x>", "exec"), ns)
    chk("成功抽取探测函数与表", want_func <= set(ns) and want_assign <= set(ns),
        str(sorted(set(ns) & (want_func | want_assign))))

    fi = ns["find_install"]
    cn = fi("wb", "cn")
    intl = fi("wb", "intl")
    print(f"    国内 → {cn or '(未找到)'}")
    print(f"    国际 → {intl or '(未找到)'}")
    if cn:
        chk("国内版路径 exe 名正确（WorkBuddy.exe，不带 AI）",
            Path(cn).name == "WorkBuddy.exe", Path(cn).name)
        chk("国内版 exe 真实存在", Path(cn).is_file())
    if intl:
        chk("国际版路径 exe 名正确（WorkBuddyAI.exe）",
            Path(intl).name == "WorkBuddyAI.exe", Path(intl).name)
        chk("国际版 exe 真实存在", Path(intl).is_file())
    if cn and intl:
        chk("两版解析到**不同**目录（不会互相覆盖）",
            str(Path(cn).parent).lower() != str(Path(intl).parent).lower(),
            f"{Path(cn).parent} vs {Path(intl).parent}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cb_dual_"))
    d = build_fake_env(tmp)
    print(f"假 LOCALAPPDATA: {tmp}\n")
    test_core(d)
    test_gui_helpers(d)
    test_missing_station(d)
    test_install_detect(d)
    print("\n" + "=" * 62)
    bad = RESULTS.count(False)
    print(f"RESULT: {'ALL PASS' if bad == 0 else f'FAIL ({bad} 项)'}  ({len(RESULTS)} 项断言)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
