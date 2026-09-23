"""Where the add-on's files live.

Slimmed from Aether Remote's paths.py for the Linux server side. No frozen
build, no tray, no supervisor - systemd keeps this alive. Two roots:

  ASSET_DIR - the program (html, js). Read-only, replaced by an update.
  DATA_DIR  - this install's state: auth.json, config.json, layout.json.
              Survives updates. AETHER_HL_DATA overrides it (the tests use a
              throwaway dir so they never touch a real install).
"""
import os

APP_NAME = "Aether Homelab"

ASSET_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    override = os.environ.get("AETHER_HL_DATA")
    if override:
        return os.path.abspath(override)
    # A systemd service usually runs from /opt or a home dir; keep state in a
    # sibling "data" folder so a git pull of the program never clobbers it.
    return os.path.join(ASSET_DIR, "data")


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
    """Return a data file, copying the shipped default in on first run."""
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


if __name__ == "__main__":
    print("assets:", ASSET_DIR)
    print("data  :", DATA_DIR)
