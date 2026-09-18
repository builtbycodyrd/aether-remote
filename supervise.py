# supervise.py - keep remote.py alive.
#
# WHY THIS EXISTS
# The server died silently on 2026-09-17 when the Claude desktop app
# restarted at 02:46. It had been started from a shell spawned by the Claude
# bridge, so it lived inside that process tree and was torn down with it.
# Nothing was logged because nothing crashed - it was killed.
#
# This supervisor is launched at logon from the Startup folder, so it belongs
# to Explorer's tree and is independent of Claude, Termius, or any shell.
# It restarts the server within seconds of any death, for any reason.

import os
import socket
import subprocess
import sys
import threading
import time

import paths

HERE = paths.ASSET_DIR
APP = paths.asset("remote.py")
LOG = paths.data("supervise.log")

PORT = 8787
MIN_BACKOFF = 3
MAX_BACKOFF = 60
CREATE_NO_WINDOW = 0x08000000


def log(msg):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        # Keep it small - this runs forever.
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
            with open(LOG, "r", encoding="utf-8") as f:
                tail = f.readlines()[-400:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def port_taken():
    s = socket.socket()
    s.settimeout(0.6)
    try:
        # The server binds the tailscale address, so probing 127.0.0.1 is not
        # enough - check whether anything at all holds the port.
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    except Exception:
        return False
    finally:
        s.close()


def python_exe():
    exe = sys.executable or ""
    # Prefer pythonw so no console window ever appears.
    cand = exe.replace("python.exe", "pythonw.exe")
    return cand if os.path.exists(cand) else (exe or "python")


def tray_running():
    """Is the tray app up? It holds a named mutex for exactly this reason.

    Checking the mutex rather than scanning the process list works the same
    whether the tray is a script or the frozen exe.
    """
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenMutexW(
            SYNCHRONIZE, False, "AetherRemoteTraySingleton")
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def keep_tray_alive():
    """The tray is the only way in for someone who does not use a terminal -
    pairing a phone, adding an app, finishing setup. If it is not running,
    nothing is visibly wrong and yet the app is unusable, so bring it back."""
    while True:
        try:
            if not tray_running():
                subprocess.Popen(
                    paths.child(paths.MODE_TRAY), cwd=HERE,
                    creationflags=CREATE_NO_WINDOW,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                log("tray started")
        except Exception as e:
            log("could not start tray: %s" % e)
        time.sleep(60)


def claim_singleton():
    """Refuse to run twice.

    This is what lets the scheduled task fire every 5 minutes and simply
    launch us, with no cleverness about whether we are already up. The old
    watchdog tried to answer that from netstat output and got it wrong: a
    socket in TIME_WAIT still shows :8787 for minutes after the server dies,
    so the watchdog decided all was well at precisely the moment it was not.
    """
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(
            None, True, "AetherRemoteSupervisorSingleton")
        if ctypes.windll.kernel32.GetLastError() == 183:   # ALREADY_EXISTS
            return False
        # Deliberately leaked: the handle must outlive this call, and the OS
        # releases it when the process ends.
        return bool(h)
    except Exception:
        return True          # never let the guard itself stop recovery


def main():
    if not claim_singleton():
        return 0
    log("supervisor started (pid %d)" % os.getpid())
    threading.Thread(target=keep_tray_alive, daemon=True).start()
    backoff = MIN_BACKOFF

    while True:
        started = time.time()
        try:
            proc = subprocess.Popen(
                paths.child(paths.MODE_SERVER),
                cwd=HERE,
                creationflags=CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log("could not launch: %s" % e)
            time.sleep(MAX_BACKOFF)
            continue

        log("server started (pid %d)" % proc.pid)
        code = proc.wait()
        alive_for = time.time() - started
        log("server exited code=%s after %.0fs" % (code, alive_for))

        # A server that ran for a while then died is a one-off; restart fast.
        # One that dies immediately is usually a port clash or a code error -
        # back off so we do not spin.
        if alive_for > 60:
            backoff = MIN_BACKOFF
        else:
            backoff = min(backoff * 2, MAX_BACKOFF)

        if port_taken():
            log("port %d already held by something else - waiting" % PORT)
            backoff = MAX_BACKOFF

        time.sleep(backoff)


if __name__ == "__main__":
    main()
