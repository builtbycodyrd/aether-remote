"""The `aether-homelab` command inside the container.

    aether-homelab code         show the login QR / secret for your authenticator
    aether-homelab status       version, service state, address, update status
    aether-homelab reset-login  new authenticator secret + sign every phone out
    aether-homelab reset-pin    turn off the PIN (for a forgotten one)
    aether-homelab update       same as the `update` command

Having root in this container already means being able to read the secret on
disk, so showing it here gives nothing away that wasn't already reachable.
"""
import json
import os
import shutil
import socket
import subprocess
import sys

import paths
import version


def _qr(text):
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ansiutf8", "-m", "2", text])
    else:
        print("(install qrencode to see a scannable QR here)")


def _ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))          # no packet is sent; picks the LAN IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "this-container"


def _port():
    try:
        with open(paths.data("config.json"), encoding="utf-8") as f:
            return json.load(f).get("port", 8788)
    except Exception:
        return 8788


def code():
    import auth
    uri = auth.provisioning_uri()
    print("\nScan this with your authenticator app (Google Authenticator, "
          "1Password, Bitwarden...):\n")
    _qr(uri)
    print("\nOr enter the secret by hand:  %s\n" % auth.STATE["secret"])


def status():
    active = subprocess.run(["systemctl", "is-active", "aether-homelab"],
                            capture_output=True, text=True).stdout.strip()
    print("Aether Homelab %s" % version.VERSION)
    print("  service : %s" % (active or "unknown"))
    print("  address : http://%s:%s" % (_ip(), _port()))
    try:
        import updater
        st = updater.state()
        if st.get("newer"):
            print("  update  : %s is available - run  update" % st["latest"])
        elif st.get("error"):
            print("  update  : couldn't check (%s)" % st["error"])
        else:
            print("  update  : up to date")
    except Exception as e:
        print("  update  : couldn't check (%s)" % e)


def _restart_service():
    """The running add-on keeps its state in memory, so a change made here
    only counts once it restarts. The service runs as this same user, so it
    may end its own main process - systemd (Restart=always) brings it back."""
    try:
        pid = int(subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "aether-homelab"],
            capture_output=True, text=True, timeout=10).stdout.strip() or 0)
    except Exception:
        pid = 0
    if pid > 0:
        try:
            os.kill(pid, 15)
            print("(restarted the add-on so the change takes effect)")
            return
        except OSError:
            pass
    print("Restart the add-on for this to take effect:  systemctl restart aether-homelab")


def reset_pin():
    import auth
    import secondfactor
    sf = secondfactor.SecondFactor(paths.data("sf.json"), lambda: auth.STATE["server_key"],
                                   log=lambda m: None)
    if not sf.enabled():
        print("No PIN is set - nothing to reset.")
        return
    answer = input("This turns the PIN off. Phones can set a new one from the "
                   "app. Type RESET to continue: ")
    if answer.strip() != "RESET":
        print("Nothing changed.")
        return
    sf.reset()
    print("Done - the PIN is off.")
    _restart_service()


def reset_login():
    import auth
    answer = input("This signs out every phone and needs a new authenticator "
                   "entry. Type RESET to continue: ")
    if answer.strip() != "RESET":
        print("Nothing changed.")
        return
    auth.reset_enrollment()
    print("Done. Scan the new code:")
    code()
    _restart_service()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "code":
        code()
    elif cmd == "status":
        status()
    elif cmd == "reset-login":
        reset_login()
    elif cmd == "reset-pin":
        reset_pin()
    elif cmd == "update":
        os.execv("/bin/sh", ["/bin/sh", paths.asset("update.sh")] + sys.argv[2:])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
