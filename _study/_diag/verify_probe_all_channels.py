# -*- coding: utf-8 -*-
"""验证「探测工具能力」按钮在所有通道都存在，且按通道取正确的本地地址、写对 key。"""
import json, os, pathlib, ast, textwrap, time, tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

RP = pathlib.Path(r"C:\Users\ASUS\vino\RP")
src = (RP / "BuddyZGateway.py").read_text(encoding="utf-8")
lines = src.splitlines()
tree = ast.parse(src)
def extract(name):
    n = next(x for x in ast.walk(tree) if isinstance(x, ast.FunctionDef) and x.name == name)
    return textwrap.dedent("\n".join(lines[n.lineno - 1: n.end_lineno]))

class Ent:
    def __init__(self, v): self._v = v
    def get(self): return self._v

# ---- 1) _local_chat_base 逐通道取址 ----
# 当前通道是 cb/mc/ca/rc/lo/qd（cp=CatPaw 已废弃）；_local_chat_base 依赖各 *_port
nsb = {"mc_port": Ent("9000"), "ca_port": Ent("abc"), "rc_port": Ent(" 9200 "),
       "lo_port": Ent("9400"), "qd_port": Ent("9500"),
       "cb_url": Ent("http://127.0.0.1:8787/v1/")}
exec(compile(extract("_local_chat_base"), "_local_chat_base", "exec"), nsb)
f = nsb["_local_chat_base"]
print("_local_chat_base 逐通道：")
for k in ("mc", "ca", "rc", "lo", "qd", "cb"):
    print(f"   {k:3s} -> {f(k)}")
print("   非法端口回退默认:", "ca" and f("ca"))

# ---- 2) 五个通道的对话框都要有按钮 ----
ns_gui = {"json": json, "os": os, "Path": pathlib.Path}
exec(compile(extract("mc_model_caps"), "x", "exec"), ns_gui)
CANDS = {"mc": ["qwen3.8-flash","kimi-k2.5"], "cb": ["GLM-5.2","hy3"],
         "ca": ["glm-5.3-flash"], "rc": ["rc-1","rc-2"], "lo": ["spark-x"],
         "qd": ["auto","lite","ultimate"]}

print("\n各通道对话框的按钮：")
for key in ("mc", "cb", "ca", "rc", "lo", "qd"):
    settings = {f"{key}_models": list(CANDS[key]), f"{key}_verified": {"dead": []}}
    probed = {"calls": None}
    def fake_probe(base, model, _p=probed):
        _p["calls"] = _p.get("calls") or []
        _p["calls"].append((base, model))
        return True
    def save_settings(s, _k=key, _p=probed):
        _p["saved"] = json.loads(json.dumps(s.get(f"{_k}_verified") or {}))
    UIQ = []
    ns = {"tk": tk, "ttk": ttk, "scale": 1.0, "root": None,
          "services": {key: SimpleNamespace(running=True)}, "settings": settings,
          "MC_MODEL_CANDIDATES": CANDS["mc"], "CB_MODEL_CANDIDATES": CANDS["cb"],
          "CA_MODEL_CANDIDATES": CANDS["ca"], "RACOON_MODEL_CANDIDATES": CANDS["rc"],
          "LO_MODEL_CANDIDATES": CANDS["lo"], "QD_MODEL_CANDIDATES": CANDS["qd"],
          "_fetch_live_models": lambda k, _c=CANDS: list(_c.get(k, [])),
          "_fetch_rc_catalog": lambda: [],
          "_mc_free_ids": lambda: set(CANDS["mc"]),
          # 默认参数绑定当前值：闭包直接引用循环变量会被下一轮改写
          "eff_models": lambda k, _s=settings: _s.get(f"{k}_models") or [],
          "_disp": lambda k, m: m, "_resolve": lambda k, m: m,
          "emit": lambda x: print("      emit:", x),
          "apply_models_live": lambda k, x: None, "refresh_model_hints": lambda: None,
          "save_settings": save_settings,
          "probe_model_tools": fake_probe,
          # models_dialog 现在还会测速；harness 里也要提供（否则探测直接 NameError）
          "probe_model_speed": lambda base, model, **kw: {
              "first": 0.1, "tps": 42.0, "tokens": 8, "elapsed": 0.2,
              "estimated": True, "error": None},
          "mc_model_caps": ns_gui["mc_model_caps"],
          # models_dialog 现在会给活动标签加【】后缀
          "model_badges_map": lambda k: {},
          # models_dialog 的 qd 分支会调 _latest_internals（跟随站点取模型表）
          "_latest_internals": lambda k, _c=CANDS: list(_c.get(k) or []),
          "_local_chat_base": lambda k: f"http://127.0.0.1:<{k}>/v1",
          "mc_port": Ent("9000"), "threading": __import__("threading"), "time": time,
          "ui": lambda fn, *a, _q=UIQ: _q.append((fn, a))}
    root = tk.Tk(); root.withdraw(); ns["root"] = root
    # _fmt_speed 是纯格式化函数，直接抽真的进来（比打桩更贴近实际）
    exec(compile(extract("_fmt_speed"), "_fmt_speed", "exec"), ns)
    exec(compile(extract("models_dialog"), "models_dialog", "exec"), ns)
    ns["models_dialog"](key)
    win = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    def walk(w):
        out = []
        for c in w.winfo_children(): out.append(c); out += walk(c)
        return out
    ws = walk(win)
    btns = [w.cget("text") for w in ws if isinstance(w, ttk.Button)]
    cbs = [w for w in ws if isinstance(w, (ttk.Checkbutton, tk.Checkbutton))]
    has_probe = "探测能力/速度" in btns
    # 全勾选后点按钮
    for w in cbs:
        if not w.instate(["selected"]):
            w.invoke()
    for w in ws:
        if isinstance(w, ttk.Button) and w.cget("text") == "探测能力/速度":
            w.invoke()
    for _ in range(60):
        while UIQ:
            fn, a = UIQ.pop(0); fn(*a)
        root.update(); time.sleep(0.1)
        if probed.get("saved"):
            break
    while UIQ:
        fn, a = UIQ.pop(0); fn(*a)
    root.update()
    sv = probed.get("saved") or {}
    print(f"   {key:3s} 有探测按钮={has_probe}  按钮={btns}")
    assert has_probe, f"{key} 缺少探测按钮"
    print(f"        探测调用={probed.get('calls')}")
    print(f"        写入 {key}_verified['tools']={sv.get('tools')}")
    root.destroy()
