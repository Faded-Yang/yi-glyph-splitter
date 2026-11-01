# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone glyph-library manager (YiGlyphMgr.exe).

Build:  python -m PyInstaller --noconfirm --clean packaging/glyphmgr.spec
Output: dist/YiGlyphMgr/YiGlyphMgr.exe  (+ dist/YiGlyphMgr/runtime/)
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(SPECPATH))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

a = Analysis(
    [os.path.join(ROOT, "glyphmgr", "app_entry.py")],
    pathex=[ROOT, os.path.join(ROOT, "tools")],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "glyphmgr", "static"), os.path.join("glyphmgr", "static")),
        (os.path.join(ROOT, "data", "glyphbank"), os.path.join("data", "glyphbank")),
    ],
    hiddenimports=[
        "glyphmgr.core", "glyphmgr.server",
        "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
        "fastapi", "starlette", "starlette.staticfiles",
        "PIL", "PIL.Image", "numpy", "cv2",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YiGlyphMgr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YiGlyphMgr",
    contents_directory="runtime",
)
