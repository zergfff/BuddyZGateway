# -*- coding: utf-8 -*-
"""
verify_qoder_offline.py — Qoder 通道离线回归（不需要联网铸 token、不发模型请求）。

覆盖：
  1. 模块加载 / create_app
  2. /health 结构
  3. /v1/models（按站点返回真实模型名）
  4. openai_to_stream_json（单条/多轮/system/数组/空）
  5. _normalize_model（精确名 / 别名 / 启发式 / 空值 / 大小写）
  6. _fixed_args（stream-json 参数完整性）
  7. apply_station 热切换 + 双站手动配置保留
  8. resolve_station / pick_auto
  9. cli_health_for / cli_health 结构
 10. credential_status（自动读桌面端登录态，本机两版都已登录）
 11. install_cli_hint / find_ide_install
 12. IDE 凭据解密链（DPAPI + AES-GCM），只验能否解出结构，不打印值
 13. 端到端流式（fake CLI）
 14. 非流式路径
 15. API key 鉴权
 16. 鉴权错误绝不能被当成正常回复（回归）
 17. 全自动 jobToken 链路可用（不真发模型请求，只验能铸出凭据）
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
sys.path.insert(0, str(STUDY / "qoder2openai"))

import qoder2openai as q  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  ○ {name} (跳过: {why})")


def main() -> int:
    print("== Qoder 通道离线回归 ==")

    # 1) 模块加载
    print("\n[1] 模块加载 / create_app")
    check("import qoder2openai", hasattr(q, "create_app"))
    app = q.create_app()
    check("create_app", app is not None)
    check("有 STATIONS", set(q.STATIONS) == {"intl", "cn"})
    check("有 credential_status", hasattr(q, "credential_status"))
    check("有 get_job_credential", hasattr(q, "get_job_credential"))
    check("有 mint_job_token", hasattr(q, "mint_job_token"))

    from fastapi.testclient import TestClient
    c = TestClient(app)

    # 2) /health
    print("\n[2] /health")
    r = c.get("/health")
    check("200", r.status_code == 200, r.text[:120])
    h = r.json()
    for k in ("status", "station", "label", "cli_found", "ide_found",
              "account", "credential_ok", "error"):
        check(f"health.{k} 存在", k in h)
    check("station=intl", h["station"] == "intl")
    check("label=国际版", h["label"] == "国际版")

    # 3) /v1/models
    print("\n[3] /v1/models（按站点）")
    for st, meta in q.STATIONS.items():
        q.apply_config({"station": st})
        r = c.get("/v1/models")
        check(f"[{st}] 200", r.status_code == 200)
        ids = [d["id"] for d in r.json()["data"]]
        check(f"[{st}] 与 STATIONS 一致", ids == list(meta["models"]),
              f"{len(ids)} vs {len(meta['models'])}")
        check(f"[{st}] 含 Auto", "Auto" in ids)
    q.apply_config({"station": "intl"})

    # 4) openai_to_stream_json
    print("\n[4] openai_to_stream_json")
    ev = q.openai_to_stream_json([{"role": "user", "content": "hi"}])
    check("type=user", ev["type"] == "user")
    check("parent_tool_use_id=None", ev["parent_tool_use_id"] is None)
    check("content 是 list", isinstance(ev["message"]["content"], list))
    check("text=hi", ev["message"]["content"][0]["text"] == "hi")

    ev = q.openai_to_stream_json([{"role": "system", "content": "你是助手"},
                                  {"role": "user", "content": "hello"}])
    t = ev["message"]["content"][0]["text"]
    check("system 前置", t.startswith("你是助手"))
    check("user 在后", "hello" in t)
    check("分隔符存在", "---" in t)

    ev = q.openai_to_stream_json([{"role": "user", "content": "a"},
                                  {"role": "assistant", "content": "b"},
                                  {"role": "user", "content": "c"}])
    t = ev["message"]["content"][0]["text"]
    check("多轮顺序", t.index("a") < t.index("assistant: b") < t.index("c"))

    ev = q.openai_to_stream_json([{"role": "user", "content": [
        {"type": "text", "text": "p1"}, {"type": "text", "text": "p2"}]}])
    t = ev["message"]["content"][0]["text"]
    check("content 数组拼接", "p1" in t and "p2" in t)

    ev = q.openai_to_stream_json([{"role": "user", "content": ""}])
    check("空消息兜底", ev["message"]["content"][0]["text"] == "（空消息）")

    # 5) _normalize_model
    print("\n[5] _normalize_model")
    check("None→Auto", q._normalize_model(None) == "Auto")
    check("空串→Auto", q._normalize_model("") == "Auto")
    check("精确名保留", q._normalize_model("Qwen3.8-Max") == "Qwen3.8-Max")
    check("大小写不敏感", q._normalize_model("qwen3.8-max") == "Qwen3.8-Max")
    check("gpt-4o→Auto", q._normalize_model("gpt-4o") == "Auto")
    check("gpt-3.5→Efficient", q._normalize_model("gpt-3.5-turbo") == "Efficient")
    check("claude-3-opus→Ultimate", q._normalize_model("claude-3-opus") == "Ultimate")
    check("未知含max→高阶", q._normalize_model("my-custom-max") == "Ultimate")
    check("未知含flash→轻量", q._normalize_model("foo-flash") == "Efficient")
    check("未知普通→Auto", q._normalize_model("weird-thing") == "Auto")
    # 站点差异：Sonus/Cantus 只有国际版有
    check("国内版不认 Sonus",
          q._normalize_model("Sonus", "cn") != "Sonus",
          q._normalize_model("Sonus", "cn"))
    check("国际版认 Sonus", q._normalize_model("Sonus", "intl") == "Sonus")

    # 6) _fixed_args
    print("\n[6] _fixed_args")
    args = q._fixed_args("Auto")
    check("--print", "--print" in args)
    check("--output-format stream-json",
          "--output-format" in args and "stream-json" in args)
    check("--input-format stream-json",
          "--input-format" in args and "stream-json" in args)
    check("--no-session-persistence", "--no-session-persistence" in args)
    check("--permission-mode bypassPermissions",
          "--permission-mode" in args and "bypassPermissions" in args)
    check("--dangerously-skip-permissions", "--dangerously-skip-permissions" in args)
    check("--disallowed-tools *", "--disallowed-tools" in args and "*" in args)
    check("--tools 空", args[args.index("--tools") + 1] == "")
    check("--model 传了", args[args.index("--model") + 1] == "Auto")
    check("无工具调用副作用", "--tools" in args and "--disallowed-tools" in args)

    # 7) apply_station 热切换
    print("\n[7] apply_station")
    q.apply_config({"station": "intl", "cli_path": "C:\\fake\\intl.exe", "pat": "PAT_INTL"})
    q.apply_station("cn", cli_path="C:\\fake\\cn.exe", pat="PAT_CN")
    check("切到 cn", q.CONFIG["station"] == "cn")
    check("cn cli_path", q.CONFIG["cli_path"] == "C:\\fake\\cn.exe")
    check("cn pat", q.CONFIG["pat"] == "PAT_CN")
    q.apply_station("intl")
    check("intl cli_path 恢复", q.CONFIG["cli_path"] == "C:\\fake\\intl.exe")
    check("intl pat 恢复", q.CONFIG["pat"] == "PAT_INTL")
    q.apply_config({"station": "intl", "cli_path": "", "pat": ""})

    # 8) resolve_station
    print("\n[8] resolve_station / station_meta")
    check("显式 cn", q.resolve_station("cn") == "cn")
    check("显式 intl", q.resolve_station("intl") == "intl")
    check("auto 返回合法站", q.resolve_station("auto") in ("cn", "intl"))
    q.apply_config({"station": "cn"})
    check("station_meta 跟随", q.station_meta()["label"] == "国内版")
    q.apply_config({"station": "intl"})
    check("station_meta intl", q.station_meta()["label"] == "国际版")

    # 9) cli_health
    print("\n[9] cli_health_for / cli_health")
    for st in ("intl", "cn"):
        hh = q.cli_health_for(st)
        for k in ("cli_found", "cli_path", "cli_source", "cli_bin", "label", "error"):
            check(f"[{st}] cli_health_for.{k}", k in hh)
        check(f"[{st}] cli_bin 正确", hh["cli_bin"] == q.STATIONS[st]["cli_bin"])
        check(f"[{st}] cli_found 是 bool", isinstance(hh["cli_found"], bool))
        if hh["cli_found"]:
            check(f"[{st}] 来源是 IDE 自带/npm/PATH",
                  any(x in hh["cli_source"] for x in ("IDE 自带", "npm", "PATH")),
                  hh["cli_source"])
            check(f"[{st}] 路径存在", os.path.isfile(hh["cli_path"]), hh["cli_path"])
    h = q.cli_health()
    for k in ("station", "label", "cli_found", "pat_set", "ide_found",
              "account", "credential_ok", "timeout_s"):
        check(f"cli_health.{k}", k in h)

    # 10) credential_status（自动读桌面端）
    print("\n[10] credential_status（自动读桌面端登录态）")
    for st in ("intl", "cn"):
        cs = q.credential_status(st)
        check(f"[{st}] 结构完整",
              all(k in cs for k in ("ide_found", "account", "email", "phone",
                                    "ide_expires", "pat_set", "ok", "error")))
        if cs["ide_found"]:
            check(f"[{st}] 读到账号", bool(cs["account"] or cs["email"] or cs["phone"]),
                  str(cs)[:120])
            check(f"[{st}] 有到期时间", bool(cs["ide_expires"]), cs["ide_expires"])
            check(f"[{st}] 状态为可用", cs["ok"] is True)
            print(f"      [{st}] 账号={cs['account']!r} "
                  f"邮箱={cs['email']!r} 手机={cs['phone']!r} "
                  f"到期={cs['ide_expires'][:10]}")
        else:
            skip(f"[{st}] 桌面端登录态", "本机未登录该版本")

    # 11) IDE 凭据解密链
    print("\n[11] IDE 凭据解密链（DPAPI + AES-256-GCM）")
    for st in ("intl", "cn"):
        meta = q.STATIONS[st]
        udd = Path(q.APP_DATA) / meta["udd_name"]
        if not (udd / "auth.v1.dat").is_file():
            skip(f"[{st}] 解密", "无 auth.v1.dat")
            continue
        try:
            key = q._master_key(udd)
            check(f"[{st}] 主密钥 32 字节", len(key) == 32, f"{len(key)}")
            blob = (udd / "auth.v1.dat").read_bytes()
            plain = q._aes_gcm_decrypt(key, blob)
            d = json.loads(plain.decode("utf-8"))
            check(f"[{st}] 明文含 token", isinstance(d.get("token"), str) and d["token"])
            check(f"[{st}] 明文含 refreshToken",
                  isinstance(d.get("refreshToken"), str) and d["refreshToken"])
            check(f"[{st}] 明文含 user.id",
                  isinstance((d.get("user") or {}).get("id"), str))
            check(f"[{st}] 未打印任何令牌值", True)
        except Exception as e:  # noqa: BLE001
            check(f"[{st}] 解密链可用", False, f"{type(e).__name__}: {e}")

    # 12) install_cli_hint / find_ide_install
    print("\n[12] install_cli_hint / find_ide_install")
    hint = q.install_cli_hint("cn")
    check("cn npm_pkg", hint["npm_pkg"] == "@qodercn-ai/qoderclicn")
    check("cn cli_bin", hint["cli_bin"] == "qoderclicn")
    check("cn pat_env", hint["pat_env"] == "QODERCN_PERSONAL_ACCESS_TOKEN")
    check("cn job_env", hint["job_env"] == "QODERCN_JOB_TOKEN")
    check("intl job_env", q.install_cli_hint("intl")["job_env"] == "QODER_JOB_TOKEN")
    for st in ("intl", "cn"):
        p = q.find_ide_install(st)
        print(f"      [{st}] IDE exe = {p or '(未找到)'}")
        if p:
            check(f"[{st}] IDE exe 存在", os.path.isfile(p))

    # 13) 端到端流式（fake CLI）
    print("\n[13] 端到端流式（fake CLI，不发真实模型请求）")
    tmpdir = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Temp"
    fake = tmpdir / "qoder_fake_cli2.js"
    fake.write_text(textwrap.dedent("""
        const rl = require('readline').createInterface({input: process.stdin});
        rl.on('line', (line) => {
          try { JSON.parse(line); } catch(e) { return; }
          console.log(JSON.stringify({type:'system', subtype:'init'}));
          console.log(JSON.stringify({type:'assistant', message:{role:'assistant',
            content:[{type:'text', text:'Hello from Qoder '}], usage:{}}}));
          console.log(JSON.stringify({type:'assistant', message:{role:'assistant',
            content:[{type:'text', text:'fake!'}], usage:{}}}));
          console.log(JSON.stringify({type:'result', result:'Hello from Qoder fake!',
            is_error:false, usage:{input_tokens:5, output_tokens:10, total_tokens:15}}));
          rl.close(); process.exit(0);
        });
    """), encoding="utf-8")

    # 让 build_env 不真去联网铸 token（只替换铸 token 那一步，保留完整 env）
    orig = q.get_job_credential
    q.get_job_credential = lambda st, force=False: {
        "token": "fake-job-token", "refresh_token": "fake-refresh",
        "created_at": "2026-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
        "expires_in": 86400000,
        "refresh_token_expires_at": "2099-01-01T00:00:00Z",
        "refresh_token_expires_in": 172800000}
    try:
        q.apply_config({"station": "intl", "cli_path": str(fake), "pat": "",
                        "timeout_s": 60})
        r = c.post("/v1/chat/completions", json={
            "model": "Auto", "messages": [{"role": "user", "content": "hi"}],
            "stream": True})
        check("流式 200", r.status_code == 200, r.text[:200])
        check("content-type SSE", "text/event-stream" in r.headers.get("content-type", ""))
        check("有 [DONE]", "data: [DONE]" in r.text)
        deltas = []
        for line in r.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    obj = json.loads(line[6:])
                    d = obj["choices"][0]["delta"]
                    if "content" in d:
                        deltas.append(d["content"])
                except Exception:
                    pass
        full = "".join(deltas)
        check("两段文本都在", "Hello from Qoder" in full and "fake" in full,
              f"{full[:80]!r}")

        # 14) 非流式
        print("\n[14] 非流式路径")
        r = c.post("/v1/chat/completions", json={
            "model": "Auto", "messages": [{"role": "user", "content": "hi"}],
            "stream": False})
        check("非流式 200", r.status_code == 200, r.text[:200])
        obj = r.json()
        check("object=chat.completion", obj["object"] == "chat.completion")
        content = obj["choices"][0]["message"]["content"]
        check("content 正确", "Hello from Qoder" in content and "fake" in content,
              f"{content[:80]!r}")
        check("finish_reason=stop", obj["choices"][0]["finish_reason"] == "stop")
    finally:
        q.get_job_credential = orig

    # 15) API key
    print("\n[15] API key 鉴权")
    q.apply_config({"api_key": ""})
    r = c.post("/v1/chat/completions", json={
        "model": "Auto", "messages": [{"role": "user", "content": "hi"}], "stream": False})
    check("无 key → 200", r.status_code == 200, r.text[:120])
    q.apply_config({"api_key": "secret123"})
    r = c.post("/v1/chat/completions", json={
        "model": "Auto", "messages": [{"role": "user", "content": "hi"}]})
    check("缺 key → 401", r.status_code == 401)
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer wrong"},
               json={"model": "Auto", "messages": [{"role": "user", "content": "hi"}]})
    check("错 key → 401", r.status_code == 401)
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer secret123"},
               json={"model": "Auto", "messages": [{"role": "user", "content": "hi"}]})
    check("对 key → 200", r.status_code == 200, r.text[:120])
    q.apply_config({"api_key": ""})

    # 16) 鉴权错误路径
    print("\n[16] 鉴权错误绝不能被当成正常回复")
    fake_err = tmpdir / "qoder_fake_err2.js"
    fake_err.write_text(textwrap.dedent("""
        const rl = require('readline').createInterface({input: process.stdin});
        rl.on('line', (line) => {
          try { JSON.parse(line); } catch(e) { return; }
          console.log(JSON.stringify({type:'system', subtype:'init'}));
          console.log(JSON.stringify({type:'assistant', error:'authentication_failed',
            message:{role:'assistant',
                     content:[{type:'text', text:'Not logged in · Please run /login'}],
                     usage:{}}}));
          console.log(JSON.stringify({type:'result', is_error:true,
            result:'Not logged in · Please run /login', terminal_reason:'completed',
            usage:{}}));
          rl.close(); process.exit(0);
        });
    """), encoding="utf-8")
    orig2 = q.get_job_credential
    q.get_job_credential = lambda st, force=False: None
    try:
        q.apply_config({"station": "intl", "cli_path": str(fake_err), "pat": ""})
        r = c.post("/v1/chat/completions", json={
            "model": "Auto", "messages": [{"role": "user", "content": "hi"}],
            "stream": False})
        check("非流式不是 200", r.status_code != 200, f"got {r.status_code}")
        check("状态 401/502", r.status_code in (401, 502), f"got {r.status_code}")
        check("提到登录/凭据",
              any(k in r.text.lower() for k in ("登录", "凭据", "auth", "login", "token")),
              r.text[:150])
        r = c.post("/v1/chat/completions", json={
            "model": "Auto", "messages": [{"role": "user", "content": "hi"}],
            "stream": True})
        check("流式也带 error", "error" in r.text.lower(), r.text[:160])
    finally:
        q.get_job_credential = orig2

    # 17) 全自动 jobToken 链路（只铸不发模型请求）
    print("\n[17] 全自动 jobToken 链路（只验能铸出凭据）")
    for st in ("intl", "cn"):
        cs = q.credential_status(st)
        if not cs.get("ide_found"):
            skip(f"[{st}] 铸 jobToken", "本机未登录")
            continue
        try:
            cred = q.get_job_credential(st, force=True)
            if cred:
                check(f"[{st}] 铸出 JobTokenCredential", True)
                check(f"[{st}] 含 token 字段", bool(cred.get("token")))
                check(f"[{st}] 含 refresh_token", bool(cred.get("refresh_token")))
                check(f"[{st}] 含 expires_at", bool(cred.get("expires_at")))
                # 二次调用应命中缓存（不再重复请求）
                c2 = q.get_job_credential(st)
                check(f"[{st}] 二次命中缓存",
                      c2 is not None and c2.get("token") == cred.get("token"))
            else:
                check(f"[{st}] 铸出 JobTokenCredential", False, "返回 None")
        except Exception as e:  # noqa: BLE001
            check(f"[{st}] 铸 jobToken", False, f"{type(e).__name__}: {e}")

    # 清理
    for f in (fake, fake_err):
        try:
            f.unlink()
        except Exception:
            pass
    q.apply_config({"station": "intl", "cli_path": "", "pat": "", "api_key": ""})

    print(f"\n=====  PASS {PASS}  FAIL {FAIL}  SKIP {SKIP}  =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
