# -*- coding: utf-8 -*-
"""
实验 5：用 IDE 的 accessToken 铸 jobToken，再喂给 CLI。

端点（从 IDE 主进程反编译确认）：
    POST {openApiBaseUrl}/api/v1/me/jobToken
    Authorization: Bearer <accessToken>
    {"clientId": "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa"}

CLI 侧的候选入口（从 CLI bundle 常量表确认 Ur("JOB_TOKEN") 会拼成
QODER_JOB_TOKEN / QODERCN_JOB_TOKEN）：
    · 环境变量 QODER_JOB_TOKEN
    · payload 文件 {type:"jobToken", jobToken:"<值>"}

全部只跑 `--list-models`（零模型额度）。
安全：不打印任何令牌值。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO / "_study" / "_diag"))
from probe_qoder_auth2 import get_master_key, aes_gcm_decrypt  # noqa: E402

CLIENT_ID = "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa"

STATION = {
    "intl": {
        "udd": "com.qoder.app.stable",
        "base": "https://openapi.qoder.sh",
        "pat_env": "QODER_PERSONAL_ACCESS_TOKEN",
        "job_env": "QODER_JOB_TOKEN",
        "cli": Path(r"C:\Users\ASUS\AppData\Local\Programs\Qoder\resources\app.asar.unpacked"
                    r"\node_modules\@qoder-ai\qoder-agent-sdk\dist\_worker"
                    r"\qoder-worker-runtime.obf.mjs"),
    },
    "cn": {
        "udd": "com.qodercn.app.stable",
        # 国内版的 openApiBaseUrl 待确认；先用 qoder.com.cn 试
        "base": "https://openapi.qoder.com.cn",
        "pat_env": "QODERCN_PERSONAL_ACCESS_TOKEN",
        "job_env": "QODERCN_JOB_TOKEN",
        "cli": Path(r"C:\Program Files\Qoder CN\resources\app.asar.unpacked"
                    r"\node_modules\@qoder-ai\qoder-cn-agent-sdk\dist\_worker"
                    r"\qoder-worker-runtime.obf.mjs"),
    },
}


def ide_auth(udd: Path) -> dict:
    key = get_master_key(udd)
    return json.loads(aes_gcm_decrypt(key, (udd / "auth.v1.dat").read_bytes()).decode())


def mint_job_token(base: str, access_token: str) -> tuple[bool, str]:
    """返回 (ok, 说明或 token)"""
    url = f"{base}/api/v1/me/jobToken"
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps({"clientId": CLIENT_ID}).encode())
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            return True, body
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def run_cli(cli: Path, extra: dict, label: str) -> None:
    env = os.environ.copy()
    for k in ("QODER_PERSONAL_ACCESS_TOKEN", "QODERCN_PERSONAL_ACCESS_TOKEN",
              "QODER_JOB_TOKEN", "QODERCN_JOB_TOKEN",
              "QODER_SDK_AUTH_PAYLOAD_FILE"):
        env.pop(k, None)
    env.update(extra)
    try:
        r = subprocess.run(["node", str(cli), "--list-models"],
                           capture_output=True, env=env, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"  [{label}] 异常: {e}")
        return
    out = (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")).strip()
    first = out.splitlines()[0][:150] if out else "(空)"
    print(f"  [{label}] exit={r.returncode}  {first}")
    if r.returncode == 0 and out:
        print(f"       ↑ 成功！前 3 行：")
        for line in out.splitlines()[:3]:
            print(f"         {line[:140]}")


def main() -> int:
    print("== 实验：IDE accessToken → jobToken → CLI ==")
    appdata = Path(os.environ["APPDATA"])
    for st, cfg in STATION.items():
        print(f"\n===== {st} =====")
        udd = appdata / cfg["udd"]
        try:
            d = ide_auth(udd)
        except Exception as e:
            print(f"  ✗ 读取 IDE 凭据失败: {type(e).__name__}: {e}")
            continue
        at = d["token"]
        print(f"  accessToken len={len(at)}  expiresAt={d.get('expiresAt')}")

        ok, res = mint_job_token(cfg["base"], at)
        if not ok:
            print(f"  ✗ 铸 jobToken 失败: {res}")
            # 也试 refreshToken 走 /api/v1/jobToken/refresh
            rt = d.get("refreshToken") or ""
            if rt:
                url = f"{cfg['base']}/api/v1/jobToken/refresh"
                req = urllib.request.Request(url, method="POST",
                                             data=json.dumps({"refresh_token": rt}).encode())
                req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        body = r.read().decode()
                    print(f"  · refresh 端点倒是通了（body {len(body)} 字节）")
                except Exception as e2:  # noqa: BLE001
                    print(f"  · refresh 端点也不行: {type(e2).__name__}")
            continue

        print(f"  ✓ 铸 jobToken 成功（响应 {len(res)} 字节）")
        try:
            jt = json.loads(res)
            print(f"   响应键: {list(jt.keys())}")
            for k, v in jt.items():
                if isinstance(v, str):
                    print(f"     {k}: str len={len(v)}")
                elif isinstance(v, dict):
                    print(f"     {k}: dict keys={list(v.keys())}")
                else:
                    print(f"     {k}: {type(v).__name__} {str(v)[:60]}")
        except Exception:
            print(f"   非 JSON 响应: {res[:200]}")
            continue

        # 找出真正的 jobToken 值
        tok = None
        for k in ("token", "jobToken", "job_token", "accessToken", "data"):
            v = jt.get(k)
            if isinstance(v, str) and v:
                tok = v
                break
            if isinstance(v, dict):
                for k2 in ("token", "jobToken", "job_token"):
                    if isinstance(v.get(k2), str):
                        tok = v[k2]
                        break
            if tok:
                break
        if not tok:
            print("  ✗ 响应里找不到 token 字段")
            continue

        print(f"  jobToken len={len(tok)}")
        cli = cfg["cli"]
        print("  --- 喂给 CLI 的三种方式 ---")
        run_cli(cli, {cfg["job_env"]: tok}, f"env {cfg['job_env']}")
        tmp = Path(tempfile.mkdtemp(prefix="qd-jt-"))
        pf = tmp / "payload.json"
        pf.write_text(json.dumps({"type": "jobToken", "jobToken": tok}), encoding="utf-8")
        run_cli(cli, {"QODER_SDK_AUTH_PAYLOAD_FILE": str(pf)}, "payload jobToken")
        run_cli(cli, {cfg["pat_env"]: tok}, f"env {cfg['pat_env']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
