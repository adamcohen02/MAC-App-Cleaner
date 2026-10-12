# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for "MacBook Optimizer.app". Build with ./build_app.sh
# (the permission descriptions below are what macOS shows in its prompts).

a = Analysis(
    ["macbook_optimizer.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/broom.png", "assets")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MacBook Optimizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MacBook Optimizer",
)

app = BUNDLE(
    coll,
    name="MacBook Optimizer.app",
    icon="assets/AppIcon.icns",
    bundle_identifier="com.macbookoptimizer.app",
    info_plist={
        "CFBundleName": "MacBook Optimizer",
        "CFBundleDisplayName": "MacBook Optimizer",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "10.13",
        "NSAppleEventsUsageDescription":
            "MacBook Optimizer uses Finder to move files to the Trash and System Events "
            "to list your Login Items.",
        "NSDesktopFolderUsageDescription":
            "MacBook Optimizer scans this folder for large and duplicate files when you ask it to.",
        "NSDocumentsFolderUsageDescription":
            "MacBook Optimizer scans this folder for large and duplicate files when you ask it to.",
        "NSDownloadsFolderUsageDescription":
            "MacBook Optimizer scans this folder for large and duplicate files when you ask it to.",
    },
)
