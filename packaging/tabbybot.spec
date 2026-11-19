# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TabbyBot (Mac .app / Windows .exe).

Never include private data: no config.yaml, settings.json, wallet, or ~/.tabbybot.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
VERSION = "0.2.0"
try:
    text = (ROOT / "tabbybot" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if match:
        VERSION = match.group(1)
except OSError:
    pass

datas = [
    (str(ROOT / "tabbybot" / "web"), "tabbybot/web"),
    (str(ROOT / "data" / "maps"), "data/maps"),
]
for path in sorted((ROOT / "tabbybot").glob("*.json")):
    datas.append((str(path), "tabbybot"))

hiddenimports = [
    "certifi",
    "mnemonic",
    "yaml",
    "websockets",
    "eth_account",
    "eth_account.account",
    "eth_account.messages",
    "eth_keyfile",
    "eth_utils",
    "eth_keys",
    "eth_hash.auto",
    "hexbytes",
    "cryptography",
    "cryptography.hazmat.primitives.kdf.scrypt",
    "cryptography.hazmat.primitives.ciphers.aead",
    "tabbybot",
    "tabbybot.app_main",
    "tabbybot.cli",
    "tabbybot.gui",
    "tabbybot.hunt",
    "tabbybot.client",
    "tabbybot.quests",
    "tabbybot.watchdog",
    "tabbybot.updater",
    "tabbybot.cluster",
    "tabbybot.navigation",
    "tabbybot.farm",
    "tabbybot.character",
    "tabbybot.market",
    "tabbybot.notifier",
    "tabbybot.keychain",
    "tabbybot.wallet",
    "tabbybot.protocol",
    "tabbybot.efficiency",
    "tabbybot.learned_nav",
    "tabbybot.loggingutil",
    "tabbybot.config",
    "tabbybot.constants",
]

a = Analysis(
    [str(ROOT / "tabbybot" / "app_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "matplotlib", "numpy", "pandas", "PIL", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

if sys.platform == "win32":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="TabbyBot",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="TabbyBot",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="TabbyBot",
    )
    app = BUNDLE(
        coll,
        name="TabbyBot.app",
        icon=None,
        bundle_identifier="meme.tabby.tabbybot",
        info_plist={
            "CFBundleName": "TabbyBot",
            "CFBundleDisplayName": "TABBY／維洛傳說",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSAppleEventsUsageDescription": "用來在瀏覽器開啟本機控制台。",
        },
    )
