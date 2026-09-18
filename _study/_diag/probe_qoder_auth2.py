# -*- coding: utf-8 -*-
"""
实验 2：用 Local State 的 encrypted_key 解 auth.v1.dat（Chrome/Electron 标准方案）。

链路：
  Local State["os_crypt"]["encrypted_key"]
      = base64( "DPAPI" + CryptProtectData(AES-256 key) )
  auth.v1.dat
      = "v10" + nonce(12B) + ciphertext + tag(16B)   ← AES-256-GCM

安全：**只打印字段名/长度，绝不打印令牌值**。
"""
from __future__ import annotations

import base64
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
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ib = _blob(data)
    ob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(ib), None, None, None, None,
                                      0, ctypes.byref(ob)):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(ob.pbData, ob.cbData)
    finally:
        kernel32.LocalFree(ob.pbData)


def get_master_key(user_data_dir: Path) -> bytes:
    p = user_data_dir / "Local State"
    d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    enc = base64.b64decode(d["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise ValueError(f"encrypted_key 前缀不是 DPAPI: {enc[:5]!r}")
    return dpapi_unprotect(enc[5:])


def aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] != b"v10":
        raise ValueError(f"auth 前缀不是 v10: {blob[:3]!r}")
    body = blob[3:]
    nonce, ct = body[:12], body[12:]
    return AESGCM(key).decrypt(nonce, ct, None)


def describe(d: dict, prefix: str = "") -> None:
    for k, v in d.items():
        name = f"{prefix}{k}"
        if isinstance(v, str):
            print(f"    {name:26s} str  len={len(v):5d}")
        elif isinstance(v, dict):
            print(f"    {name:26s} dict keys={list(v.keys())}")
            describe(v, prefix=f"{name}.")
        elif isinstance(v, list):
            print(f"    {name:26s} list len={len(v)}")
        else:
            print(f"    {name:26s} {type(v).__name__} {v!r}")


def main() -> int:
    appdata = Path(os.environ["APPDATA"])
    targets = [
        ("国际版", appdata / "com.qoder.app.stable"),
        ("国内版", appdata / "com.qodercn.app.stable"),
    ]
    rc = 0
    for label, udd in targets:
        print(f"\n===== {label} ({udd.name}) =====")
        try:
            key = get_master_key(udd)
            print(f"  ✓ 主密钥解出: {len(key)} 字节")
        except Exception as e:
            print(f"  ✗ 主密钥失败: {type(e).__name__}: {e}")
            rc = 1
            continue

        auth = udd / "auth.v1.dat"
        if not auth.is_file():
            print("  ✗ 无 auth.v1.dat")
            rc = 1
            continue
        try:
            plain = aes_gcm_decrypt(key, auth.read_bytes())
        except Exception as e:
            print(f"  ✗ auth 解密失败: {type(e).__name__}: {e}")
            rc = 1
            continue

        print(f"  ✓ auth.v1.dat 解密成功！明文 {len(plain)} 字节")
        try:
            d = json.loads(plain.decode("utf-8"))
        except Exception as e:
            print(f"  ✗ JSON 解析失败: {e}")
            print(f"    明文头部: {plain[:80]!r}")
            rc = 1
            continue
        print(f"  顶层键: {list(d.keys())}")
        describe(d)
        print(f"  expiresAt = {d.get('expiresAt')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
