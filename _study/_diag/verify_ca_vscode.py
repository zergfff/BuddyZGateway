# -*- coding: utf-8 -*-
"""verify_ca_vscode — CodeArts 凭证来源扩展到 VS Code 插件后的回归。

背景
----
CodeArts 通道原本只认桌面端 Agent 的 session：
    %APPDATA%\\codearts-agent\\Local State + User\\globalStorage\\state.vscdb
但华为云 CodeArts 插件 `huaweicloud.vscode-codebot` 装在 VS Code 里登录后，
凭证躺在 VS Code 自己的库里（**同一个 v10 加密方案**）：
    %APPDATA%\\Code\\Local State + User\\globalStorage\\state.vscdb
    ItemTable key = secret://{"extensionId":"huaweicloud.vscode-codebot",
                              "key":"SYSTEM_HC_USER_INFO"}

两处登录的是同一华为云账号，凭证可互换，所以要一起扫。注意 VS Code 的
state.vscdb 被**所有扩展共用**，必须按插件 ID 精确匹配，不能像桌面端那样取
第一条 secret（会拿到别的扩展的秘密）。

用法：
    python verify_ca_vscode.py           # 离线：只验证探测与解密
    python verify_ca_vscode.py --live    # 另打一次 /v1/balance（只读，不轮转令牌）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CA_DIR = REPO / "_study" / "codearts2openai"
GATEWAY = REPO / "BuddyZGateway.py"


def load_gateway():
    """把 BuddyZGateway.py 当模块加载（跳过 main），用于测 GUI 侧探测逻辑。"""
    import types
    src = GATEWAY.read_text(encoding="utf-8").replace(
        'if __name__ == "__main__":', 'if False:')
    mod = types.ModuleType("bzg")
    mod.__file__ = str(GATEWAY)
    exec(compile(src, str(GATEWAY), "exec"), mod.__dict__)
    return mod


def load_ca():
    sys.path.insert(0, str(CA_DIR))
    import codearts2openai as ca
    return ca


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="额外真实调用一次 opengw 余额接口（只读，不轮转 refresh_token）")
    args = ap.parse_args()

    # 别碰真实 state：给个临时数据目录
    tmp_state = Path(tempfile.mkdtemp(prefix="ca_state_"))
    os.environ["BUDDYZ_DATA_DIR"] = str(tmp_state)

    ok = True
    ca = load_ca()
    bzg = load_gateway()

    print("=== 1) 凭证库探测 ===")
    stores = ca._session_stores()
    if not stores:
        print("  ✗ 没找到任何凭证库")
        return 1
    for s in stores:
        print(f"  · {s['label']:<24} match={s['match']:<8} {s['vscdb']}")
    labels = [s["label"] for s in stores]
    has_vscode = any("VS Code" in l for l in labels)
    print(("  ✓ " if has_vscode else "  ✗ ") + "识别到 VS Code 插件凭证库")
    ok &= has_vscode

    print("\n=== 2) 解密各库登录会话 ===")
    got = ca._bootstrap_all()
    if not got:
        print("  ✗ 一个都没解出来")
        return 1
    for label, sess in got:
        print(f"  · {label}")
        print(f"      refresh_token : {'有' if sess.get('refresh_token') else '无'}"
              f"（{len(sess.get('refresh_token') or '')} 字符）")
        print(f"      DPoP 私钥      : {'有' if sess.get('dpop_priv') else '无'}"
              f"  pub={'有' if sess.get('dpop_pub') else '无'}")
        print(f"      code_verifier : {'有' if sess.get('verifier') else '无'}")
        print(f"      直连 AK/SK     : {'有' if sess.get('access_key_id') else '无'}"
              f"  access_key_id={sess.get('access_key_id')}")
        print(f"      账号          : {sess.get('login_name')}  到期 {sess.get('expires_at')}")
    need = ("refresh_token", "dpop_priv", "verifier")
    good = all(s.get(k) for _l, s in got for k in need)
    print(("  ✓ " if good else "  ✗ ") + "refresh_token + DPoP 密钥 + verifier 齐备")
    ok &= good

    print("\n=== 3) 免轮转直连凭证（ticket 链） ===")
    tk = ca._store_ticket_creds()
    if tk:
        print(f"  ✓ 取到 AK/SK：{tk['access_key_id']}… 到期 {tk.get('expiration')}")
    else:
        print("  · 没取到（可能已过期）——DPoP 链仍可用")

    print("\n=== 4) 安装位置探测（桌面端优先，退 VS Code） ===")
    # 用假目录树验证优先级：两个都在时必须返回桌面端
    fake = Path(tempfile.mkdtemp(prefix="ca_install_"))
    try:
        desktop = fake / "codearts-agent"
        desktop.mkdir()
        (desktop / "codearts-agent.exe").write_bytes(b"MZ")
        vsc = fake / "Microsoft VS Code"
        vsc.mkdir()
        (vsc / "Code.exe").write_bytes(b"MZ")

        order = bzg._kind_profiles("ca")
        hits = []
        for hints, exes in order:
            h = bzg._scan_dirs_for([str(fake)], hints, exes, [])
            hits.append((hints[0], h))
        for h in hints if False else hits:
            print(f"  · hints={h[0]:<18} → {h[1] or '(未命中)'}")
        first = hits[0][1] if hits else ""
        prefer_ok = bool(first) and "codearts-agent" in first
        print(("  ✓ " if prefer_ok else "  ✗ ") + "两者都在时优先命中华为云桌面端")
        ok &= prefer_ok

        only_vsc = fake / "_only_vsc"
        only_vsc.mkdir()
        (only_vsc / "Microsoft VS Code").mkdir()
        (only_vsc / "Microsoft VS Code" / "Code.exe").write_bytes(b"MZ")
        h2 = ""
        for hints, exes in order:
            h2 = bzg._scan_dirs_for([str(only_vsc)], hints, exes, [])
            if h2:
                break
        vsc_ok = bool(h2) and h2.endswith("Code.exe")
        print(("  ✓ " if vsc_ok else "  ✗ ") + f"只有 VS Code 时命中 {h2 or '(未命中)'}")
        ok &= vsc_ok

        # 用户把 VS Code 文件夹粘进「程序位置」也要能定位到 exe
        r = bzg.resolve_install_input(str(vsc), "ca")
        r_ok = r.endswith("Code.exe")
        print(("  ✓ " if r_ok else "  ✗ ") + f"文件夹归一化 → {r}")
        ok &= r_ok
    finally:
        shutil.rmtree(fake, ignore_errors=True)

    print("\n=== 5) 本机真实探测 ===")
    real = bzg.find_install("ca")
    print(f"  find_install('ca') → {real or '(未命中)'}")
    if real:
        kind = "华为云桌面端" if "codearts" in Path(real).name.lower() else "VS Code 插件"
        print(f"  判定来源：{kind}")

    if args.live and tk:
        print("\n=== 6) 真实调用余额接口（只读） ===")
        try:
            import httpx
            url = ca.OPENGW + "/api/v1/user/tokens/balance"
            base = {"X-Security-Token": tk["security_token"],
                    "Content-Type": "application/json"}
            headers = ca._sign(tk["access_key_id"], tk["access_key_secret"]
                               if "access_key_secret" in tk else tk["secret_access_key"],
                               "GET", url, base, b"")
            with httpx.Client(timeout=20) as c:
                r = c.get(url, headers=headers)
            body = r.json()
            print(f"  HTTP {r.status_code}  error_code={body.get('error_code')}")
            if str(body.get("error_code")) == "0000":
                print("  ✓ 凭证可用（余额查询成功）")
            else:
                print(f"  ✗ 上游返回：{str(body)[:200]}")
                ok = False
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 调用失败：{e}")
            ok = False

    shutil.rmtree(tmp_state, ignore_errors=True)
    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
