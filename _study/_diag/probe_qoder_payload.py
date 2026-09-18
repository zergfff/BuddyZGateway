# -*- coding: utf-8 -*-
"""
实验 4：把 IDE 的 accessToken 通过 QODER_SDK_AUTH_PAYLOAD_FILE 喂给 CLI。

原理（从官方 SDK 反编译确认）：
  SDK 启动 CLI 前会写一个 payload.json，并用环境变量
  QODER_SDK_AUTH_PAYLOAD_FILE 把路径传给 CLI。
  payload 支持 {type:"accessToken", accessToken:"<值>"}。
  这条路径与 QODER_PERSONAL_ACCESS_TOKEN（PAT，走 /jobToken/exchange 校验）
  **不是同一条校验链路** —— IDE 解出来的正是 accessToken。

只跑 `--list-models`（零模型额度，纯鉴权 + 列模型）。
安全：不打印任何令牌值。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(r"C:\Users\ASUS\vino\RP")
sys.path.insert(0, str(REPO / "_study" / "_diag"))
from probe_qoder_auth2 import get_master_key, aes_gcm_decrypt  # noqa: E402

STATION = {
    "intl": ("com.qoder.app.stable",
             Path(r"C:\Users\ASUS\AppData\Local\Programs\Qoder\resources\app.asar.unpacked"
                  r"\node_modules\@qoder-ai\qoder-agent-sdk\dist\_worker\qoder-worker-runtime.obf.mjs")),
    "cn": ("com.qodercn.app.stable",
           Path(r"C:\Program Files\Qoder CN\resources\app.asar.unpacked"
                r"\node_modules\@qoder-ai\qoder-cn-agent-sdk\dist\_worker\qoder-worker-runtime.obf.mjs")),
}


def ide_token(udd: Path) -> str:
    key = get_master_key(udd)
    d = json.loads(aes_gcm_decrypt(key, (udd / "auth.v1.dat").read_bytes()).decode())
    return d["token"]


def run(cli: Path, env_extra: dict, label: str) -> None:
    env = os.environ.copy()
    env.pop("QODER_PERSONAL_ACCESS_TOKEN", None)
    env.pop("QODERCN_PERSONAL_ACCESS_TOKEN", None)
    env.update(env_extra)
    r = subprocess.run(["node", str(cli), "--list-models"],
                       capture_output=True, env=env, timeout=120)
    out = (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")).strip()
    print(f"  [{label}] exit={r.returncode}")
    for line in out.splitlines()[:6]:
        print(f"      {line[:160]}")


def main() -> int:
    print("== 实验：IDE accessToken → QODER_SDK_AUTH_PAYLOAD_FILE ==")
    appdata = Path(os.environ["APPDATA"])
    for st, (udd_name, cli) in STATION.items():
        print(f"\n===== {st} =====")
        udd = appdata / udd_name
        try:
            tok = ide_token(udd)
        except Exception as e:
            print(f"  ✗ 取 token 失败: {type(e).__name__}: {e}")
            continue
        print(f"  IDE token: len={len(tok)}")
        if not cli.is_file():
            print(f"  ✗ CLI 不存在: {cli}")
            continue

        # 基线：不给任何凭据
        run(cli, {}, "基线 无凭据")

        # 形态 A：payload 文件 type=accessToken
        tmp = Path(tempfile.mkdtemp(prefix="qd-probe-"))
        pf = tmp / "payload.json"
        pf.write_text(json.dumps({"type": "accessToken", "accessToken": tok}),
                      encoding="utf-8")
        run(cli, {"QODER_SDK_AUTH_PAYLOAD_FILE": str(pf)}, "payload accessToken")

        # 形态 B：payload type=qodercli（用 CLI 自身登录态）
        pf2 = tmp / "payload2.json"
        pf2.write_text(json.dumps({"type": "qodercli"}), encoding="utf-8")
        run(cli, {"QODER_SDK_AUTH_PAYLOAD_FILE": str(pf2)}, "payload qodercli")

        # 形态 C：PAT 环境变量塞 IDE token（已知会被拒，作对照）
        run(cli, {"QODER_PERSONAL_ACCESS_TOKEN": tok}, "env PAT(对照)")
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
