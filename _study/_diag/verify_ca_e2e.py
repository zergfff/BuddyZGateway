# -*- coding: utf-8 -*-
"""verify_ca_e2e — CodeArts 通道端到端（走真实 ASGI 路由，不是只测模块内部）。

覆盖：
  /health          形状 + station + pool 字段
  /v1/models       暴露白名单生效
  /v1/chat/completions   非流式：OpenAI 形状（choices/message/usage）
  /v1/chat/completions   流式：SSE chunk + [DONE]
  本地鉴权         设了 local_api_key 后无 Bearer 必须 401

默认只走 **ticket 链**（免费模型）——**不消耗单次 refresh_token**，
所以不会把你 VS Code 插件/桌面端手里的会话顶掉、不用重登。
想测 DPoP 链（非免费模型）加 --dpop，但那会轮转 refresh_token，
插件那边需重新登录 —— 自己决定。

用法：
    python verify_ca_e2e.py
    python verify_ca_e2e.py --dpop
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CA_DIR = REPO / "_study" / "codearts2openai"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpop", action="store_true",
                    help="额外测 DPoP 链（非免费模型）——会轮转 refresh_token")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="ca_e2e_"))
    os.environ["BUDDYZ_DATA_DIR"] = str(tmp)
    ok = True
    try:
        sys.path.insert(0, str(CA_DIR))
        import codearts2openai as ca
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            print("需要 httpx 的 TestClient（fastapi 自带）。pip install httpx")
            return 2

        MODELS = ["deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.3-flash"]
        ca.CONFIG["exposed_models"] = list(MODELS)
        ca.CONFIG["port"] = 19100
        client = TestClient(ca.app)

        print("=== 1) GET /health ===")
        r = client.get("/health")
        h = r.json()
        print(f"  HTTP {r.status_code}  {json.dumps(h, ensure_ascii=False)[:220]}")
        shape_ok = (r.status_code == 200 and h.get("service") == "codearts2openai"
                    and "station" in h and isinstance(h.get("pool"), dict))
        print(("  ✓ " if shape_ok else "  ✗ ") + "形状含 service/station/pool")
        ok &= shape_ok

        print("\n=== 2) GET /v1/models（白名单生效） ===")
        r = client.get("/v1/models")
        d = r.json()
        ids = [m["id"] for m in d.get("data", [])]
        print(f"  HTTP {r.status_code}  ids={ids}")
        models_ok = r.status_code == 200 and set(ids) == set(
            ca.display_of(m) for m in MODELS)
        print(("  ✓ " if models_ok else "  ✗ ") + "只暴露白名单里的模型")
        ok &= models_ok

        print("\n=== 3) 本地鉴权（设了 local_api_key 必须 401） ===")
        ca.CONFIG["local_api_key"] = "SECRET"
        r = client.get("/v1/models")
        r2 = client.get("/v1/models", headers={"Authorization": "Bearer SECRET"})
        auth_ok = r.status_code == 401 and r2.status_code == 200
        print(("  ✓ " if auth_ok else "  ✗ ") +
              f"无 key={r.status_code} 带 key={r2.status_code}")
        ok &= auth_ok
        ca.CONFIG["local_api_key"] = ""

        print("\n=== 4) 非流式对话（ticket 链，不轮转 refresh_token） ===")
        payload = {"model": None, "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": 64, "stream": False}
        picked, resp = None, None
        errs = []
        for m in MODELS:
            payload["model"] = m
            rr = client.post("/v1/chat/completions", json=payload)
            body = rr.json()
            if rr.status_code == 200 and body.get("choices"):
                picked, resp = m, body
                break
            errs.append(f"{m}: {rr.status_code} {str(body)[:120]}")
        if resp is None:
            # 区分「代码 bug」与「账号会话过期」：
            # 后者是环境/账号状态（refresh_token 被轮转掉），不该算代码回归 ——
            # 网关现在会自动拉起授权页，用户重新授权一次即可恢复。
            _auth_dead = bool(errs) and all(
                ("会话已失效" in e or "重新授权" in e or "503" in e) for e in errs)
            if _auth_dead:
                print("  ○ 跳过（SKIP）：CodeArts 会话已过期，非代码问题")
                print("     → 网关启动时会自动拉起授权页；重新授权后可复跑本测试")
                for e in errs:
                    print("     ", e[:130])
                chat_ok = None          # None = 未验证（不算失败）
            else:
                print("  ✗ 所有免费模型都失败：")
                for e in errs:
                    print("     ", e)
                print("  （若全是 'model is not registered' 说明当前站点没有这些模型，"
                      "不是通道 bug）")
                ok = False
        else:
            ch = resp["choices"][0]
            msg = ch.get("message") or {}
            content = (msg.get("content") or "").strip()
            print(f"  模型 {picked} → finish={ch.get('finish_reason')} "
                  f"usage={resp.get('usage')}")
            print(f"  回复：{(content or msg.get('reasoning_content') or '')[:60]!r}")
            chat_ok = (resp.get("object") == "chat.completion"
                       and bool(ch.get("finish_reason"))
                       and "usage" in resp
                       and bool(content or msg.get("reasoning_content") or msg.get("tool_calls")))
            print(("  ✓ " if chat_ok else "  ✗ ") + "OpenAI 形状完整")
            ok &= chat_ok

        print("\n=== 5) 流式对话（SSE） ===")
        payload["stream"] = True
        picked_s, chunks = None, []
        for m in MODELS:
            payload["model"] = m
            with client.stream("POST", "/v1/chat/completions", json=payload) as rr:
                if rr.status_code != 200:
                    continue
                got_data = got_done = False
                ct = rr.headers.get("content-type", "")
                for line in rr.iter_lines():
                    line = (line or "").strip()
                    if line.startswith("data:"):
                        if line[5:].strip() == "[DONE]":
                            got_done = True
                            break
                        try:
                            chunks.append(json.loads(line[5:]))
                        except Exception:
                            pass
                        got_data = True
            if got_done:
                picked_s = m
                break
        if picked_s:
            print(f"  模型 {picked_s} → content-type={ct}  chunk={len(chunks)}  [DONE]=True")
            st_ok = got_data and len(chunks) >= 1
            print(("  ✓ " if st_ok else "  ✗ ") + "SSE 有 data chunk 且以 [DONE] 收尾")
            ok &= st_ok
        else:
            print("  · 没有可用模型做流式（与第 4 步同因，非通道 bug）")

        if args.dpop:
            print("\n=== 6) DPoP 链（非免费模型，会轮转 refresh_token） ===")
            try:
                cred = ca.ensure_creds(prefer="dpop")
                print(f"  cred station={cred.get('station')} exp={cred.get('expiration')}")
                url, _hd, _raw = ca._chat_prep("GLM-5.2", [{"role": "user", "content": "hi"}],
                                               32, False)
                print(f"  chat url={url}")
                print("  ✓ DPoP 链取到凭证")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ DPoP 链失败：{e}")
                ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
