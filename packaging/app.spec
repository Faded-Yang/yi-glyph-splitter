# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-file windowed exe for the Yi glyph segmenter UI."""
import os

ROOT = os.path.abspath(os.getcwd())

hidden = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "yi_core", "yi_core.common", "yi_core.segment_v1", "yi_core.export", "yi_core.qc",
    "app", "app.run", "app.server", "app.store", "app.jobs", "app.pipeline",
    "app.ai_refine", "app.config", "app.paths",
]

datas = [
    (os.path.join(ROOT, "app", "static"), os.path.join("app", "static")),
    (os.path.join(ROOT, "tools"), "tools"),
]

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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="YiGlyphSplit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon=os.path.join(ROOT, "packaging", "app.ico") if os.path.exists(os.path.join(ROOT, "packaging", "app.ico")) else None,
)
