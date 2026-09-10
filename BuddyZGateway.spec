# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 打包 BuddyZGateway 单文件 exe（无控制台窗口）。

用法：
    pyinstaller BuddyZGateway.spec
"""
import os

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    'converter', 'desensitize', 'monkeycode2openai', 'codearts2openai',
    'sqlite3', 'yaml', 'pystray.win32', 'pystray._util.win32',
]
for pkg in ('fastapi', 'starlette', 'uvicorn', 'httpx', 'httpcore', 'anyio',
            'pydantic', 'pydantic_core', 'dotenv', 'h11', 'httptools',
            'websockets', 'pystray', 'PIL'):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]

# 内嵌模块解包目录（首次运行自动写入，此处仅作 import 搜索路径）
_RUNTIME = os.path.join(
    os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
    'BuddyZGateway', 'runtime', 'codebuddy2openai')

a = Analysis(
    ['BuddyZGateway.py'],
    pathex=[_RUNTIME],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pip', 'setuptools', 'unittest', 'pydoc_data', 'pytest', 'uvloop', 'xmlrpc', 'pygments'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BuddyZGateway',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
