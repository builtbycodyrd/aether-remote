"""Aether Remote installer.

Built into AetherRemoteSetup.exe. A friend downloads one file, double-clicks
it, and ends up with the app installed, running, set to start with Windows,
and the setup wizard open in their browser.

Design rules, all of them learned the hard way earlier in this project:

  * Per-user. Everything goes to %LOCALAPPDATA%, so copying files needs no
    admin and shows no UAC prompt.
  * Exactly ONE elevation, at the end, for the two things that genuinely
    need it: the firewall rule and the scheduled task. If the user declines
    it, the app still works - it just will not start by itself, and we say
    so instead of pretending everything is fine.
  * Never block on a dialog nobody is there to click. Anything that could
    hang has a timeout.
  * The user's own data is never touched by an install or an upgrade - only
    the program folder is replaced.
"""
import os
import shutil
import subprocess
import sys
import time
import winreg

try:
    from version import VERSION, PUBLISHER, APP_NAME as APP
except Exception:                      # running from a stripped copy
    VERSION, PUBLISHER, APP = "0.0.0", "builtbycodyrd", "Aether Remote"

EXE = "AetherRemote.exe"

LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
TARGET = os.path.join(LOCAL, "Programs", APP)
DATA = os.path.join(LOCAL, APP)
UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AetherRemote"

CREATE_NO_WINDOW = 0x08000000


def payload_dir():
    """Where the files we are installing live.

    Frozen, they are inside the bundle. From source, they are the folder this
    script sits in, so the installer can be tested without building it first.
    """
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "payload")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "build", "dist", "AetherRemote")


def copy_program(log):
    """Replace the program folder, leaving the user's data alone.

    The app is usually running when an upgrade happens, so stop it first -
    otherwise the copy fails on a locked exe halfway through and leaves a
    half-installed folder.
    """
    stop_running(log)

    src = payload_dir()
    if not os.path.isdir(src):
        raise RuntimeError("the installer is missing its payload (%s)" % src)

    if os.path.isdir(TARGET):
        log("removing the previous version")
        for attempt in range(5):
            try:
                shutil.rmtree(TARGET)
                break
            except Exception as e:
                if attempt == 4:
                    raise RuntimeError(
                        "could not replace the old version - is it still "
                        "running? (%s)" % e)
                time.sleep(1.5)

    log("copying files")
    shutil.copytree(src, TARGET)
    log("installed to %s" % TARGET)
    return os.path.join(TARGET, EXE)


def stop_running(log):
    """Stop a running copy so its files can be replaced."""
    try:
        subprocess.run(["schtasks", "/end", "/tn", APP],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    try:
        r = subprocess.run(["taskkill", "/IM", EXE, "/F"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log("stopped the running copy")
            time.sleep(1.5)
    except Exception:
        pass


def shortcut(path, target, log, args="", desc=""):
    """Write a .lnk without pywin32, via the shell's own COM object."""
    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        "$l = $s.CreateShortcut('%s'); "
        "$l.TargetPath = '%s'; "
        "$l.Arguments = '%s'; "
        "$l.WorkingDirectory = '%s'; "
        "$l.IconLocation = '%s'; "
        "$l.Description = '%s'; "
        "$l.Save()"
        % (path.replace("'", "''"), target.replace("'", "''"), args,
           os.path.dirname(target).replace("'", "''"),
           target.replace("'", "''"), desc.replace("'", "''"))
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy",
                        "Bypass", "-Command", ps],
                       capture_output=True, timeout=60,
                       creationflags=CREATE_NO_WINDOW)
        log("shortcut: %s" % os.path.basename(path))
        return True
    except Exception as e:
        log("could not create %s (%s)" % (os.path.basename(path), e))
        return False


def place_uninstaller(log):
    """Leave a copy of ourselves in the install folder.

    Windows' uninstall entry has to point at something that knows how to
    uninstall, and AetherRemote.exe does not - it would just report an
    unknown option. So the setup exe copies itself in as uninstall.exe.
    """
    if not getattr(sys, "frozen", False):
        return None
    dest = os.path.join(TARGET, "uninstall.exe")
    try:
        shutil.copy2(sys.executable, dest)
        log("uninstaller placed")
        return dest
    except Exception as e:
        log("could not place the uninstaller (%s)" % e)
        return None


def register_uninstall(exe, log, uninstaller=None):
    """Appear in Settings > Apps, like anything else they installed.

    HKCU, not HKLM - this is a per-user install and has no business writing
    machine-wide keys.
    """
    try:
        size_kb = sum(
            os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(TARGET) for f in fs) // 1024
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as k:
            for name, val in [
                ("DisplayName", APP),
                ("DisplayVersion", VERSION),
                ("Publisher", PUBLISHER),
                ("DisplayIcon", exe),
                ("InstallLocation", TARGET),
                ("UninstallString", '"%s" --uninstall' % (uninstaller or exe)),
                ("QuietUninstallString",
                 '"%s" --uninstall --quiet' % (uninstaller or exe)),
                ("NoModify", None), ("NoRepair", None),
            ]:
                if val is None:
                    winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)
                else:
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        log("registered in Settings > Apps")
    except Exception as e:
        log("could not register the uninstaller (%s)" % e)


def elevate_setup(log):
    """One UAC prompt, for the two things that actually need admin.

    Returns True only if both scripts reported success. A declined prompt is
    not an error - it is a choice, and the caller says what it costs.
    """
    fw = os.path.join(TARGET, "setup-firewall.ps1")
    tk = os.path.join(TARGET, "setup-task.ps1")
    missing = [p for p in (fw, tk) if not os.path.isfile(p)]
    if missing:
        log("setup scripts missing: %s" % ", ".join(os.path.basename(m)
                                                    for m in missing))
        return False

    # Both scripts in one elevated shell = one prompt, not two.
    inner = (
        "& '%s' -Root '%s'; $a = $LASTEXITCODE; "
        "& '%s' -Root '%s' -TaskName '%s'; $b = $LASTEXITCODE; "
        "exit ([int]($a -ne 0) + [int]($b -ne 0))"
        % (fw.replace("'", "''"), TARGET.replace("'", "''"),
           tk.replace("'", "''"), TARGET.replace("'", "''"), APP)
    )
    # Start-Process -Verb RunAs is what raises the UAC prompt.
    outer = (
        "$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
        "-WindowStyle Hidden -ArgumentList "
        "'-NoProfile','-ExecutionPolicy','Bypass','-Command',"
        "'%s'; exit $p.ExitCode" % inner.replace("'", "''")
    )
    log("asking for permission to add the firewall rule and the startup task")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy",
                            "Bypass", "-Command", outer],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log("that took too long - skipping it")
        return False

    err = (r.stderr or "")
    if "canceled by the user" in err or "cancelled" in err.lower():
        log("permission declined")
        return False
    if r.returncode != 0:
        log("one of the setup steps did not finish (code %s)" % r.returncode)
        return False
    log("firewall rule added and startup task registered")
    return True


def install(log, make_desktop=True, admin=True, start=True):
    """Install.

    admin=False installs the files and shortcuts but skips the firewall rule
    and the startup task. Useful to anyone who does not want to grant admin -
    the app still runs, it just will not start by itself - and it is how this
    gets tested without a UAC prompt.
    """
    exe = copy_program(log)

    start_menu = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs", APP + ".lnk")
    shortcut(start_menu, exe, log, desc="Control this PC from your phone")
    if make_desktop:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop", APP + ".lnk")
        shortcut(desktop, exe, log, desc="Control this PC from your phone")

    register_uninstall(exe, log, place_uninstaller(log))

    elevated = False
    if admin:
        elevated = elevate_setup(log)
    else:
        log("skipping the firewall rule and startup task (--no-admin)")

    if start:
        # If the task was registered it may already be starting, and the
        # tray's singleton mutex keeps that from becoming two copies.
        log("starting %s" % APP)
        try:
            subprocess.Popen([exe], cwd=TARGET, creationflags=CREATE_NO_WINDOW,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as e:
            log("could not start it: %s" % e)

    return exe, elevated


def uninstall(log, keep_data=True):
    log("stopping %s" % APP)
    stop_running(log)

    # Both removals in one elevated shell, so the user sees one prompt on the
    # way out just as they did on the way in.
    fw = os.path.join(TARGET, "setup-firewall.ps1")
    tk_ = os.path.join(TARGET, "setup-task.ps1")
    parts = []
    if os.path.isfile(tk_):
        parts.append("& ''%s'' -Root ''%s'' -TaskName ''%s'' -Remove"
                     % (tk_, TARGET, APP))
    if os.path.isfile(fw):
        parts.append("& ''%s'' -Root ''%s'' -Remove" % (fw, TARGET))
    if parts:
        log("removing the startup task and firewall rule")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command",
                 "Start-Process powershell -Verb RunAs -Wait "
                 "-WindowStyle Hidden -ArgumentList "
                 "'-NoProfile','-ExecutionPolicy','Bypass','-Command','%s'"
                 % "; ".join(parts)],
                capture_output=True, timeout=240)
        except Exception as e:
            log("could not remove them (%s) - you may need to do it by hand"
                % e)

    for lnk in (os.path.join(os.environ.get("APPDATA", ""),
                             r"Microsoft\Windows\Start Menu\Programs",
                             APP + ".lnk"),
                os.path.join(os.path.expanduser("~"), "Desktop", APP + ".lnk")):
        try:
            os.remove(lnk)
        except Exception:
            pass

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except Exception:
        pass

    # The program goes. Their layout, their apps and their authenticator
    # secret stay unless they explicitly say otherwise - reinstalling should
    # not mean setting the whole thing up again.
    if os.path.isdir(TARGET):
        try:
            shutil.rmtree(TARGET, ignore_errors=True)
        except Exception:
            pass
    log("removed the program")

    if not keep_data:
        shutil.rmtree(DATA, ignore_errors=True)
        log("removed your settings too")
    else:
        log("your settings were kept in %s" % DATA)


# ----------------------------------------------------------------- the window

BG, PANEL, FG, DIM = "#0b0a18", "#161232", "#f1f0f7", "#8c94a8"
PURPLE, CYAN = "#7c3aed", "#22d3ee"


def gui(mode):
    import threading
    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    root.title("%s Setup" % APP)
    root.configure(bg=BG)
    root.resizable(False, False)
    w, h = 560, 420
    root.geometry("%dx%d+%d+%d" % (
        w, h, (root.winfo_screenwidth() - w) // 2,
        (root.winfo_screenheight() - h) // 3))
    try:
        ico = os.path.join(payload_dir(), "aether.ico")
        if os.path.isfile(ico):
            root.iconbitmap(ico)
    except Exception:
        pass

    head = tkfont.Font(family="Segoe UI", size=17, weight="bold")
    body = tkfont.Font(family="Segoe UI", size=10)
    mono = tkfont.Font(family="Consolas", size=9)

    tk.Label(root, text=APP, bg=BG, fg=FG, font=head).pack(pady=(26, 2))
    sub = tk.Label(root, bg=BG, fg=DIM, font=body, wraplength=460,
                   justify="center",
                   text=("Control this PC from your phone.\n"
                         "Installs for you only - no admin needed for the app "
                         "itself.")
                   if mode == "install" else
                   "This will remove %s from your PC." % APP)
    sub.pack(pady=(0, 16))

    box = tk.Text(root, height=10, bg=PANEL, fg=DIM, font=mono, bd=0,
                  padx=12, pady=10, wrap="word", highlightthickness=0)
    box.pack(fill="both", expand=True, padx=26)
    box.configure(state="disabled")

    def log(msg):
        box.configure(state="normal")
        box.insert("end", msg + "\n")
        box.see("end")
        box.configure(state="disabled")
        root.update_idletasks()

    bar = tk.Frame(root, bg=BG)
    bar.pack(fill="x", padx=26, pady=18)

    desktop_var = tk.BooleanVar(value=True)
    if mode == "install":
        tk.Checkbutton(bar, text="Desktop shortcut", variable=desktop_var,
                       bg=BG, fg=DIM, selectcolor=PANEL, font=body,
                       activebackground=BG, activeforeground=FG,
                       bd=0, highlightthickness=0).pack(side="left")

    btn = tk.Button(bar, text="Install" if mode == "install" else "Uninstall",
                    bg=PURPLE, fg="#ffffff", font=body, bd=0, padx=26, pady=8,
                    activebackground=CYAN, activeforeground=BG, cursor="hand2")
    btn.pack(side="right")

    state = {"done": False, "ok": False, "exe": None, "elevated": False}

    def work():
        try:
            if mode == "install":
                state["exe"], state["elevated"] = install(
                    log, make_desktop=desktop_var.get())
            else:
                uninstall(log)
            state["ok"] = True
        except Exception as e:
            log("")
            log("Setup could not finish: %s" % e)
        state["done"] = True
        finish()

    def finish():
        btn.configure(state="normal", text="Close", bg=PANEL, fg=FG,
                      command=root.destroy)
        if mode != "install" or not state["ok"]:
            return
        log("")
        if state["elevated"]:
            log("Done. The tray icon is running, and your browser should be "
                "opening the setup wizard.")
        else:
            # Honest about what was skipped, and what to do about it.
            log("Done - but without permission for the firewall rule and the "
                "startup task, it will not start on its own and your phone "
                "may not reach it. Run this installer again to add them.")
        sub.configure(text="Finish setting up in the window that opens.")

    def go():
        btn.configure(state="disabled", text="Working...")
        threading.Thread(target=work, daemon=True).start()

    btn.configure(command=go)
    root.mainloop()


def silent(mode, admin=True, start=True):
    def log(msg):
        try:
            if sys.stdout is not None:
                print(msg)
        except Exception:
            pass
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(os.path.join(DATA, "install.log"), "a",
                      encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    if mode == "install":
        install(log, admin=admin, start=start)
    else:
        uninstall(log)
    return 0


def main():
    args = [a.lower() for a in sys.argv[1:]]
    mode = "uninstall" if "--uninstall" in args or "/uninstall" in args \
        else "install"
    quiet = "--quiet" in args or "/s" in args or "/silent" in args
    admin = "--no-admin" not in args
    start = "--no-start" not in args

    if quiet:
        return silent(mode, admin=admin, start=start)
    gui(mode)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
