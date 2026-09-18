"""The single entry point for the packaged app.

A friend installing this has no Python, so the tray, the server and the
supervisor cannot be three scripts - they are one exe told apart by a flag:

    AetherRemote.exe                the tray app (what the installer runs,
                                    and what starts at logon)
    AetherRemote.exe --server       the http server
    AetherRemote.exe --supervise    keeps the tray and the server alive;
                                    this is what the scheduled task runs

Running from source, `python launch.py --server` behaves identically, so
there is only one code path to reason about in either build.
"""
import sys

import paths


def tell(text):
    """Say something to whoever ran us.

    The packaged exe is windowed, so it has no stdout at all - sys.stdout is
    None and a bare print() raises. Write the text where it can be read
    afterwards instead.

    Deliberately NOT a message box. These flags get run by the installer and
    by scripts, and MessageBoxW blocks until someone clicks it - which hung
    a test run, and would hang an unattended install exactly the same way.
    Anyone who just double-clicks the exe gets the tray, not this.
    """
    try:
        if sys.stdout is not None:
            print(text)
            return
    except Exception:
        pass

    try:
        with open(paths.data("last-message.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def main():
    argv = sys.argv[1:]
    mode = argv[0] if argv and argv[0].startswith("--") else ""

    if mode in ("--help", "-h", "/?"):
        tell(__doc__.strip())
        return 0

    if mode == "--version":
        tell("Aether Remote\nassets: %s\ndata  : %s"
             % (paths.ASSET_DIR, paths.DATA_DIR))
        return 0

    # The sub-programs parse their own arguments, so hide the mode flag from
    # them - remote.py's argparse would reject it outright.
    if mode:
        sys.argv = [sys.argv[0]] + argv[1:]

    if mode == paths.MODE_SERVER:
        import remote
        return remote.main()

    if mode == paths.MODE_SUPERVISE:
        import supervise
        return supervise.main()

    if mode:
        tell("Unknown option: %s\n\nRun with --help to see what it accepts."
             % mode)
        return 2

    import tray
    return tray.main()


if __name__ == "__main__":
    sys.exit(main() or 0)
