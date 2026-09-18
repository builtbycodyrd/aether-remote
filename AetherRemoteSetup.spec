# -*- mode: python ; coding: utf-8 -*-
#
# The installer. One file a friend downloads and double-clicks.
#
# It carries the whole built app inside it as "payload", so there is nothing
# to unzip and no second download. Build AetherRemote.spec first - this spec
# reads its output.

import os

HERE = os.path.abspath(SPECPATH)
APP = os.path.join(HERE, "build", "dist", "AetherRemote")

if not os.path.isdir(APP):
    raise SystemExit(
        "build AetherRemote.spec first - no app found at %s" % APP)

# Everything the app build produced, kept in the same shape under payload/.
datas = []
for root, _, files in os.walk(APP):
    rel = os.path.relpath(root, APP)
    dest = "payload" if rel == "." else os.path.join("payload", rel)
    for f in files:
        datas.append((os.path.join(root, f), dest))

# The two elevated setup scripts live beside the app, not inside _internal,
# because the installer passes their paths to an elevated PowerShell.
for f in ("setup-firewall.ps1", "setup-task.ps1", "watchdog.vbs"):
    p = os.path.join(HERE, f)
    if os.path.isfile(p):
        datas.append((p, "payload"))

print("installer payload: %d files" % len(datas))

a = Analysis(
    [os.path.join(HERE, "installer.py")],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=["tkinter", "tkinter.font"],
    excludes=["numpy", "scipy", "pandas", "matplotlib", "pytest",
              "PIL", "qrcode", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="AetherRemoteSetup",
    debug=False,
    strip=False,
    upx=False,
    # One file: a download that is a single exe, not a folder.
    onefile=True,
    console=False,
    icon=os.path.join(HERE, "aether.ico"),
    version=os.path.join(HERE, "version-setup.txt"),
)
