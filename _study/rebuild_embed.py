# -*- coding: utf-8 -*-
"""
rebuild_embed.py — 把 _study/ 下的反代源码重新内嵌回 BuddyZGateway.py。

为什么需要它：BuddyZGateway.py 的 _EMBEDDED 里存的才是运行时真正物化的源码
（materialize() 每次启动都会用 _EMBEDDED 覆盖 runtime/）。只改 _study/*.py
而不重新内嵌，等于没改——启动时会被打回原样。

旧的 build_gui.py 是从一份过期 TEMPLATE 整文件重生成，会把你当前 GUI 里
较新的模块（codearts/raccoon/loomy/mc_saas 等）全部丢掉，已删除该脚本。本脚本只做
「就地替换 _EMBEDDED 条目」，不碰 GUI 代码。

用法：
    python rebuild_embed.py            # 用 _study 覆盖 BuddyZGateway.py 的内嵌
    python rebuild_embed.py --check    # 只报告差异，不写入
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # _study/
REPO = HERE.parent                              # 仓库根
TARGET = REPO / "BuddyZGateway.py"

# 内嵌清单 = 目标文件现有条目（以它为准，避免漏模块）
CHUNK = 100


def load_manifest(text: str) -> tuple[dict[str, list[str]], int, int]:
    """返回 ({key: [chunk...]}, start_idx, end_idx)，按行索引。"""
    lines = text.split("\n")
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("_EMBEDDED"):
            start = i
            break
    if start is None:
        raise SystemExit("找不到 _EMBEDDED 定义")
    end = start + 1
    while end < len(lines) and not lines[end].startswith("}"):
        end += 1
    body = lines[start:end + 1]

    man: dict[str, list[str]] = {}
    cur = None
    key_re = re.compile(r'^\s*"([^"]+)"\s*:\s*$')
    chunk_re = re.compile(r'^\s*"([A-Za-z0-9+/=]*)"\s*,?\s*$')
    for ln in body:
        m = key_re.match(ln)
        if m:
            cur = m.group(1)
            man[cur] = []
            continue
        m = chunk_re.match(ln)
        if m and cur is not None:
            man[cur].append(m.group(1))
    return man, start, end


def render_entry(key: str, b64: str, indent: str) -> list[str]:
    chunks = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)] or [""]
    out = [f'{indent}"{key}":']
    for i, c in enumerate(chunks):
        sep = "," if i == len(chunks) - 1 else ""
        out.append(f'    "{c}"{sep}')
    return out


# 这些不内嵌：构建脚本自身、下划线开头的目录（_diag 等）
SKIP_TOP = {"rebuild_embed.py"}


def discover_new(man: dict) -> list:
    """_study 下「应该内嵌但 manifest 里还没有」的文件（rel 路径）。

    规则与现有清单一致：
      _study/<模块>/<文件>.py  →  键 "<模块>/<文件>.py"
      _study/<文件>.py         →  键 "<文件>.py"（放 runtime 根，供各模块共用）
    没有这步，新加的共用模块永远进不了内嵌清单、rebuild 也刷不进 BuddyZGateway.py。
    """
    extra = []
    for p in sorted(HERE.glob("*.py")):
        if p.name in SKIP_TOP:
            continue
        if p.name not in man:
            extra.append(p.name)
    for sub in sorted(HERE.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        for p in sorted(sub.glob("*.py")):
            rel = f"{sub.name}/{p.name}"
            if rel not in man:
                extra.append(rel)
    return extra


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告差异")
    args = ap.parse_args()

    raw = TARGET.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)

    man, start, end = load_manifest(raw)
    print(f"{TARGET.name} 内嵌条目 {len(man)} 个")

    changed = []
    for key in list(man):
        src = HERE / key
        if not src.is_file():
            print(f"  !! 缺少源文件，跳过：{src}")
            continue
        old = base64.b64decode("".join(man[key])) if man[key] else b""
        new = src.read_bytes()
        if old == new:
            print(f"  = {key}  ({len(new)} 字节，未变)")
            continue
        changed.append(key)
        print(f"  * {key}  {len(old)} -> {len(new)} 字节")

    extra = discover_new(man)
    for key in extra:
        src = HERE / key
        print(f"  + {key}  (新增内嵌，{src.stat().st_size} 字节)"
              if src.is_file() else f"  !! 新增条目源文件缺失：{src}")
    changed += [k for k in extra if (HERE / k).is_file()]

    if not changed:
        print("没有需要更新的条目。")
        return 0
    if args.check:
        print(f"[--check] 需要更新 {len(changed)} 个条目，未写入。")
        return 0

    key_order = list(man)
    spans = {}
    # 重新按行扫描定位每条 key 的行区间
    key_re = re.compile(r'^\s*"([^"]+)"\s*:\s*$')
    chunk_re = re.compile(r'^\s*"([A-Za-z0-9+/=]*)"\s*,?\s*$')
    i = start + 1
    while i <= end:
        m = key_re.match(lines[i])
        if m:
            k = m.group(1)
            j = i + 1
            while j <= end and chunk_re.match(lines[j]):
                j += 1
            spans[k] = (i, j)
            i = j
            continue
        i += 1

    # ① 先替换既有条目（从后往前，避免行号漂移）
    for key in reversed(key_order):
        src = HERE / key
        if not src.is_file():
            continue
        b64 = base64.b64encode(src.read_bytes()).decode("ascii")
        a, b = spans[key]
        indent = lines[a][: len(lines[a]) - len(lines[a].lstrip())]
        lines[a:b] = render_entry(key, b64, indent)

    # ② 再追加新条目（插到 _EMBEDDED 的收尾 } 之前）。
    #    既有条目的行区间都在 } 之前，所以上一步不会影响这里。
    if extra:
        close = None
        for k in range(start + 1, len(lines)):
            if lines[k].startswith("}"):
                close = k
                break
        if close is None:
            raise SystemExit("找不到 _EMBEDDED 的收尾 }")

        def _one_comma(idx):
            """确保该行以**恰好一个**逗号结尾（chunk 行本来就可能有逗号）。"""
            s = lines[idx].rstrip()
            lines[idx] = (s[:-1].rstrip() if s.endswith(",") else s) + ","

        # 收尾 } 之前可能有空行，要往前找到真正的最后一个 chunk 行
        tail = close - 1
        while tail > start and not lines[tail].strip():
            tail -= 1
        _one_comma(tail)

        indent = "    "
        for n, key in enumerate(extra):
            src = HERE / key
            if not src.is_file():
                continue
            b64 = base64.b64encode(src.read_bytes()).decode("ascii")
            entry = render_entry(key, b64, indent)
            lines[close:close] = entry
            close += len(entry)
            if n < len(extra) - 1:          # 后面还有条目才需要逗号
                _one_comma(close - 1)

    TARGET.write_text(nl.join(lines), encoding="utf-8", newline="")
    print(f"已更新 {len(changed)} 个条目 -> {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
