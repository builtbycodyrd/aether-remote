# -*- mode: python ; coding: utf-8 -*-
#
# One exe, three modes (see launch.py). One-folder rather than one-file: it
# starts faster, and the supervisor spawns the server constantly - a one-file
# build would unpack itself to a temp folder on every single launch.
#
# Only the PROGRAM goes in here. Nothing the user owns - no auth.json, no
# config.json, no layout.json - or an update would ship one person's secret
# to everyone. config.default.json is the seed; paths.seed() copies it on
# first run.

import os

HERE = os.path.abspath(SPECPATH)

# Served straight off disk by the http server, so they must be real files in
# the bundle rather than imported modules.
WEB = [
    "ui.html", "login.html", "pc.html", "setup.html",
    "app.js", "pc.js", "setup.js",
    "config.default.json", "aether.ico",
]

datas = [(os.path.join(HERE, f), ".") for f in WEB if os.path.isfile(os.path.join(HERE, f))]

static = os.path.join(HERE, "static")
if os.path.isdir(static):
    for name in sorted(os.listdir(static)):
        p = os.path.join(static, name)
        if os.path.isfile(p):
            datas.append((p, "static"))

# launch.py imports these inside functions; name them so the analysis cannot
# miss one and produce an exe that works in three of its four modes.
hiddenimports = [
    "paths", "remote", "tray", "supervise",
    "auth", "layout", "library", "icons", "stream", "sysctl",
    "update", "wol", "tls", "secondfactor", "webauthn",
    # passkey (Face ID) signature checks
    "cryptography.hazmat.primitives.asymmetric.ec",
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.hazmat.primitives.asymmetric.padding",
    "cryptography.hazmat.primitives.serialization",
    "qrcode", "qrcode.image.pil", "PIL", "PIL.Image",
]

a = Analysis(
    [os.path.join(HERE, "launch.py")],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here needs a scientific stack; leaving them out keeps the
    # download something a friend will actually wait for.
    excludes=["numpy", "scipy", "pandas", "matplotlib", "pytest",
              "PIL.ImageQt", "PyQt5", "PySide2", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AetherRemote",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console: this is a tray app, and the server and supervisor are
    # background processes. A console would flash up on every restart.
    console=False,
    icon=os.path.join(HERE, "aether.ico"),
    # Company, product and version in the file's properties. A binary with
    # none of that reads as anonymous to both people and heuristics.
    version=os.path.join(HERE, "version-app.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AetherRemote",
)
