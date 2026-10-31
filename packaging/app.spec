# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: onedir build.

dist/YiGlyphSplit/
    YiGlyphSplit.exe   <- small launcher
    runtime/           <- python runtime + libs + assets
"""
import os

ROOT = os.path.abspath(os.getcwd())

from PyInstaller.utils.hooks import collect_data_files, collect_submodules  # noqa: E402

hidden = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "yi_core", "yi_core.common", "yi_core.segment_v1", "yi_core.export", "yi_core.qc",
    "app", "app.run", "app.server", "app.store", "app.jobs", "app.pipeline",
    "app.config", "app.paths",
    # native window: pywebview -> pythonnet -> WebView2
    "webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
    "clr", "pythonnet", "clr_loader",
]

# pywebview ships WebView2 interop DLLs; pythonnet needs its runtime DLLs
try:
    _wv_data = collect_data_files("webview")
except Exception:
    _wv_data = []
try:
    _wv_hidden = collect_submodules("webview")
except Exception:
    _wv_hidden = []
hidden = list(dict.fromkeys(hidden + _wv_hidden))

datas = [
    (os.path.join(ROOT, "app", "static"), os.path.join("app", "static")),
    (os.path.join(ROOT, "tools"), "tools"),
    (os.path.join(ROOT, "data", "glyphbank"), os.path.join("data", "glyphbank")),
] + list(_wv_data)

a = Analysis(
    [os.path.join(ROOT, "app", "run.py")],
    pathex=[ROOT, os.path.join(ROOT, "tools")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "pandas", "torch", "tkinter.test", "test", "unittest",
              "pytest", "setuptools", "pip", "notebook", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YiGlyphSplit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    contents_directory="runtime",
    icon=os.path.join(ROOT, "packaging", "app.ico") if os.path.exists(os.path.join(ROOT, "packaging", "app.ico")) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YiGlyphSplit",
)
