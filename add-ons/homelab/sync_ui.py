"""Copy the shared phone app from Aether Remote into the add-on.

The homelab serves the SAME phone app as the PC (tile board, editor, themes,
PIN, PC switcher). It ships inside the add-on because an install or update
only copies add-ons/homelab. Run this after changing the PC app's ui.html,
app.js or login.html:

    python3 add-ons/homelab/sync_ui.py            (from the repo)
    python3 sync_ui.py --check                    (exit 1 if out of date)
"""
import filecmp
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["ui.html", "app.js", "login.html"]
ICONS = ["icon-16.png", "icon-32.png", "icon-48.png", "icon-180.png",
         "icon-192.png", "icon-512.png"]


def pairs(root):
    dst = os.path.join(HERE, "web", "shared")
    for f in FILES:
        yield os.path.join(root, f), os.path.join(dst, f)
    for f in ICONS:
        yield os.path.join(root, "static", f), os.path.join(dst, "static", f)


def _same(src, dst):
    """Byte-equal for the icons; line-ending-blind for the text files (the
    PC's checkout on Windows has CRLF, the add-on always ships LF)."""
    if not os.path.isfile(dst):
        return False
    if src.endswith(".png"):
        return filecmp.cmp(src, dst, shallow=False)
    with open(src, "rb") as a, open(dst, "rb") as b:
        return a.read().replace(b"\r\n", b"\n") == b.read().replace(b"\r\n", b"\n")


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if src.endswith(".png"):
        shutil.copyfile(src, dst)
    else:
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read().replace(b"\r\n", b"\n"))


def main():
    args = sys.argv[1:]
    root = os.path.abspath(os.path.join(HERE, "..", ".."))
    if "--from" in args:
        root = os.path.abspath(args[args.index("--from") + 1])
    check = "--check" in args
    stale = []
    for src, dst in pairs(root):
        if not os.path.isfile(src):
            sys.exit("missing %s - run this from the aether-remote repo" % src)
        if _same(src, dst):
            continue
        stale.append(os.path.relpath(dst, HERE))
        if not check:
            _copy(src, dst)
    if check:
        if stale:
            print("OUT OF DATE: " + ", ".join(stale))
            sys.exit(1)
        print("shared UI is in sync")
    else:
        print("copied: " + (", ".join(stale) or "nothing - already in sync"))


if __name__ == "__main__":
    main()
