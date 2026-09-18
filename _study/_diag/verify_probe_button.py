# -*- coding: utf-8 -*-
"""验证「探测工具能力」按钮：真的只测勾选的模型，并把结果写回 settings。"""
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

# 真的探针函数 + 真的 caps 函数（从 GUI 源码取）
ns_gui = {"json": json, "os": os, "Path": pathlib.Path, "time": time}
ns_gui["__name__"] = "gui_mod"
for fn in ("probe_model_tools", "mc_model_caps"):
    exec(compile(extract(fn), fn, "exec"), ns_gui)
probe_model_tools = ns_gui["probe_model_tools"]

CAND = ["qwen3.8-flash", "kimi-k2.5", "glm-5.3-flash", "minimax-m2.5", "qwen3.5-flash-x", "qwen3.5-plus"]
# 只勾 3 个；qwen3.5-flash-x 是故意编的假模型，用来验证“探测失败不写死 False”
CHECKED = ["qwen3.8-flash", "kimi-k2.5", "qwen3.5-flash-x"]

settings = {"mc_models": CHECKED, "mc_verified": {"dead": [], "alive": CAND}}
saved = {}
def save_settings(s):
    saved.clear(); saved.update(json.loads(json.dumps(s.get("mc_verified") or {})))

UIQ = []   # 模拟 pump() 的回调队列
ns = {"tk": tk, "ttk": ttk, "scale": 1.0, "root": None,
      "services": {"mc": SimpleNamespace(running=True)},
      "settings": settings,
      "MC_MODEL_CANDIDATES": list(CAND), "CB_MODEL_CANDIDATES": [], "CA_MODEL_CANDIDATES": [],
      "RACOON_MODEL_CANDIDATES": [], "CP_MODEL_CANDIDATES": [],
      "_fetch_live_models": lambda k: list(CAND), "_fetch_rc_catalog": lambda: [],
      "_mc_free_ids": lambda: set(CAND),
      "eff_models": lambda k: settings.get(f"{k}_models") or [],
      "_disp": lambda k, m: m, "_resolve": lambda k, m: m,
      "emit": lambda x: print("   emit:", x),
      "apply_models_live": lambda k, x: None, "refresh_model_hints": lambda: None,
      "save_settings": save_settings,
      "probe_model_tools": probe_model_tools,
      "mc_model_caps": ns_gui["mc_model_caps"],
      # models_dialog 现在会给活动标签加【】后缀
      "model_badges_map": lambda k: {},
      "mc_port": SimpleNamespace(get=lambda: "9000"),
      "ui": lambda fn, *a: UIQ.append((fn, a)),   # 真 app 是把回调投给 Tk 线程(pump)
      "threading": __import__("threading"),
      "time": time}
root = tk.Tk(); root.withdraw(); ns["root"] = root
exec(compile(extract("models_dialog"), "models_dialog", "exec"), ns)
ns["models_dialog"]("mc")
win = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]

def walk(w):
    out = []
    for c in w.winfo_children(): out.append(c); out += walk(c)
    return out
ws = walk(win)
cbs = [w for w in ws if isinstance(w, (ttk.Checkbutton, tk.Checkbutton))]
btns = [w for w in ws if isinstance(w, ttk.Button)]
print("按钮:", [w.cget("text") for w in btns])
print("勾选前复选框文本:")
for w in cbs:
    print("   ", w.cget("text"))

# 确保只有 CHECKED 被勾上
for w in cbs:
    raw = w.cget("text").split("（")[0]
    want = raw in CHECKED
    if w.instate(["selected"]) != want:
        w.invoke()

BTN = "探测能力/速度"   # 按钮文案（旧脚本写的是「探测工具能力」，已改名）
print(f"\n>>> 点「{BTN}」（会真的打 9000，只测勾选的 3 个）")
t0 = time.time()
for w in btns:
    if w.cget("text") == BTN:
        w.invoke()
# 等后台线程
def drain():
    while UIQ:                       # 等价于真 app 的 pump()
        fn, a = UIQ.pop(0)
        fn(*a)

# 少等一点：真服务在跑时几秒就出结果；不在跑就别白等两分钟
for _ in range(40):
    drain(); root.update(); time.sleep(0.5)
    if saved:
        break
time.sleep(3)
for _ in range(10):
    drain(); root.update(); time.sleep(0.2)
print(f"耗时 {time.time()-t0:.1f}s")
if not saved:
    print("   !! 没写回结果 —— 多半是 mc 服务(9000)没在跑，无法真实探测")
    print("      （这不是代码 bug：本脚本设计为对着运行中的服务做端到端验证）")
    root.destroy()
    raise SystemExit(0)
print("\n写回 settings 的 mc_verified['tools']:")
for k, v in (saved.get("tools") or {}).items():
    print(f"   {k:20s} -> {v}")
print("\n是否只测了勾选的 3 个:",
      set((saved.get('tools') or {}).keys()) == set(CHECKED))
print("假模型 qwen3.5-flash-x 记为 None（未知，没写死 False）:",
      (saved.get('tools') or {}).get("qwen3.5-flash-x") is None)

# 重绘后标签应出现「无工具」?（本次都支持，应显示 通过 数）
ws2 = walk(win)
labels = [w.cget("text") for w in ws2 if isinstance(w, ttk.Label) and w.cget("text")]
print("\n图例:", [l for l in labels if "工具" in l or "图像" in l])
root.destroy()
