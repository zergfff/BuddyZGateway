# -*- coding: utf-8 -*-
"""
实验：解开 Qoder IDE 的 auth.v1.dat（Electron safeStorage / DPAPI）。

格式（从 IDE 主进程反编译确认）：
    const e = safeStorage.decryptString(fs.readFileSync(filePath));
    const A = JSON.parse(e);
    // {schemaVersion:1, token:"...", refreshToken:"...", expiresAt:"...",
    //  user:{id:"..."}, profileOverlay:...}

Windows 上 safeStorage 用的是 DPAPI(CryptProtectData)，产物前缀 "v10"。
本脚本只用 stdlib 的 ctypes 调 crypt32，不解密则不打印任何值。

安全：**只打印字段名 / 长度 / 类型，绝不打印令牌值**。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_unprotect(data: bytes) -> bytes:
    """CryptUnprotectData（当前用户上下文）"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    bin_blob = _blob(data)
    out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(bin_blob), None, None, None,
                                    None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def decrypt_safe_storage(path: Path) -> dict:
    raw = path.read_bytes()
    if raw[:3] != b"v10":
        raise ValueError(f"未知前缀 {raw[:3]!r}（期望 v10 = DPAPI）")
    plain = dpapi_unprotect(raw[3:])
    return json.loads(plain.decode("utf-8"))


def describe(d: dict, prefix: str = "") -> None:
    """只描述结构，不打印值"""
    for k, v in d.items():
        name = f"{prefix}{k}"
        if isinstance(v, str):
            print(f"    {name:24s} str   len={len(v):5d}  head={v[:6]!r}…")
        elif isinstance(v, dict):
            print(f"    {name:24s} dict  keys={list(v.keys())[:8]}")
            describe(v, prefix=f"{name}.")
        elif isinstance(v, (int, float, bool)) or v is None:
            print(f"    {name:24s} {type(v).__name__:5s} {v!r}")
        elif isinstance(v, list):
            print(f"    {name:24s} list  len={len(v)}")
        else:
            print(f"    {name:24s} {type(v).__name__}")


def main() -> int:
    appdata = os.environ.get("APPDATA", "")
    targets = [
        ("国际版", Path(appdata) / "com.qoder.app.stable" / "auth.v1.dat",
         Path(appdata) / "com.qoder.app.stable" / "auth.machine-id"),
        ("国内版", Path(appdata) / "com.qodercn.app.stable" / "auth.v1.dat",
         Path(appdata) / "com.qodercn.app.stable" / "auth.machine-id"),
    ]
    for label, p, mid in targets:
        print(f"\n===== {label} =====")
        print(f"  文件: {p}")
        if not p.is_file():
            print("  (不存在)")
            continue
        print(f"  大小: {p.stat().st_size}")
        if mid.is_file():
            v = mid.read_text(encoding="utf-8", errors="replace").strip()
            print(f"  machine-id: len={len(v)} head={v[:8]!r}…")
        try:
            d = decrypt_safe_storage(p)
        except Exception as e:
            print(f"  ✗ 解密失败: {type(e).__name__}: {e}")
            continue
        print(f"  ✓ 解密成功，顶层键: {list(d.keys())}")
        describe(d)
        # 明确回答：有没有能直接用的长寿命凭据
        for key in ("token", "refreshToken", "accessToken", "pat",
                    "personalAccessToken"):
            if key in d and isinstance(d[key], str):
                print(f"  → 发现 {key}: len={len(d[key])}")
        exp = d.get("expiresAt")
        if exp:
            print(f"  → expiresAt = {exp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
