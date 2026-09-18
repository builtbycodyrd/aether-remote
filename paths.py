"""Where the app's files live.

Two roots, and the difference matters once this is installed rather than
just run out of a folder:

  ASSET_DIR - the program. Read-only. html, js, icons, the .ico. Replaced
              wholesale by an update, so nothing of the user's may live here.
  DATA_DIR  - the install's own state. auth.json, layout.json, config.json,
              the icon cache, the log. Survives updates; an uninstall that
              keeps it keeps the user's remote exactly as they left it.

Running from source the two are the same folder, so nothing changes for a
checkout that already works. Frozen by PyInstaller, DATA_DIR moves to
%LOCALAPPDATA%\\Aether Remote, which is writable without admin - the whole
reason this installs per-user and never shows a UAC prompt.

AETHER_DATA overrides DATA_DIR, which is how the tests get a throwaway
install without touching the real one.
"""
import os
import sys

APP_NAME = "Aether Remote"

FROZEN = getattr(sys, "frozen", False)


def _asset_dir():
    if FROZEN:
        # One-file builds unpack to _MEIPASS; one-folder builds sit beside
        # the exe. This covers both without caring which we shipped.
        return getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    override = os.environ.get("AETHER_DATA")
    if override:
        return os.path.abspath(override)
    if FROZEN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    return _asset_dir()


ASSET_DIR = _asset_dir()
DATA_DIR = _data_dir()

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass


def asset(*parts):
    """A file that ships with the program."""
    return os.path.join(ASSET_DIR, *parts)


def data(*parts):
    """A file belonging to this install."""
    return os.path.join(DATA_DIR, *parts)


def seed(name, default_name=None):
    """Return a data file, copying the shipped default in on first run.

    Used for config.json, so a fresh install starts from the bundled
    defaults instead of from nothing - and so a user who has edited theirs
    never has it overwritten by an update.
    """
    target = data(name)
    if os.path.isfile(target):
        return target
    src = asset(default_name or (name.rsplit(".", 1)[0] + ".default.json"))
    if os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(target):
        try:
            with open(src, "rb") as a, open(target, "wb") as b:
                b.write(a.read())
        except Exception:
            pass
    return target


# The three things this program can be. Frozen, they are all one exe told
# apart by a flag; from source they are three scripts. Everything that spawns
# a copy of us goes through child() so that difference lives in exactly one
# place.
MODE_TRAY = ""
MODE_SERVER = "--server"
MODE_SUPERVISE = "--supervise"

_SCRIPTS = {
    MODE_TRAY: "tray.py",
    MODE_SERVER: "remote.py",
    MODE_SUPERVISE: "supervise.py",
}


def child(mode=MODE_TRAY):
    """The command line that launches another copy of us in `mode`."""
    if FROZEN:
        return [sys.executable] + ([mode] if mode else [])

    exe = sys.executable or "python"
    # pythonw so no console window ever flashes up.
    quiet = exe.replace("python.exe", "pythonw.exe")
    if os.path.isfile(quiet):
        exe = quiet
    return [exe, asset(_SCRIPTS[mode])]


if __name__ == "__main__":
    print("frozen   :", FROZEN)
    print("assets   :", ASSET_DIR)
    print("data     :", DATA_DIR)
    print("same     :", os.path.abspath(ASSET_DIR) == os.path.abspath(DATA_DIR))
