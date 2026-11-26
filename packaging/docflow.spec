# -*- mode: python ; coding: utf-8 -*-
"""DocFlow 桌面版打包配置（PyInstaller --onedir，无控制台窗口）。

在项目根目录执行：
    python -m PyInstaller packaging/docflow.spec --noconfirm
产物：dist/DocFlow/DocFlow.exe（旁边 _internal/ 是内置运行时与前端资源）

要点
- desktop_app.py 是同进程起 uvicorn + pywebview 原生窗口的入口；backend 整包收集。
- frontend/dist 作为数据打进 _internal，config.RES_DIR 在打包后正好指向那里。
- OCR（rapidocr + onnxruntime + opencv）与 PDF（PyMuPDF）都打进包里，装完即用，不联网下载。
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.dirname(SPECPATH))  # SPECPATH = packaging/
APP_NAME = "DocFlow"
VERSION = "2.4.0"

hiddenimports = [m for m in collect_submodules("backend") if "tests" not in m]
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "pysqlite2",
    "websockets",
    "pythonnet",
    "clr",
]
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("rapidocr_onnxruntime")

datas = [(os.path.join(ROOT, "frontend", "dist"), os.path.join("frontend", "dist"))]
datas += collect_data_files("rapidocr_onnxruntime")      # *.onnx 模型 + default_config.yaml
datas += collect_data_files("webview")                   # win32 JS/C# 资源
datas += collect_data_files("pythonnet")
datas += collect_data_files("pymupdf")
binaries = collect_dynamic_libs("pythonnet")

excludes = [
    "pytest", "_pytest", "tkinter", "IPython", "notebook", "matplotlib",
    "scipy", "pandas", "PyQt5", "PyQt6", "PySide2", "PySide6", "setuptools",
    "backend.tests", "tests",
]

a = Analysis(
    [os.path.join(ROOT, "desktop_app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                       # 桌面形态：不弹黑框
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "packaging", "app.ico"),
    version=os.path.join(ROOT, "packaging", "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)