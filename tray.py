# tray.py - the PC app: a tray icon that owns the server.
#
# Deliberately dependency-light. The tray icon is raw Shell_NotifyIcon through
# ctypes rather than pystray, and the dialogs are tkinter, which ships with
# Python - so PyInstaller produces one small exe with nothing to install.
#
# Menu:
#   Open remote        - the same web UI, in the default browser
#   Pair a phone       - QR of the URL + the enrolment code
#   Add an app...      - native file picker, adds a launcher tile
#   Rescan games
#   Start / Stop
#   Quit
#
# The server itself runs as a child process, supervised, exactly as before.

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths                      # noqa: E402
import update                     # noqa: E402

# The program's folder. The user's files live in paths.DATA_DIR, which is
# the same place when running from source and %LOCALAPPDATA% once installed.
HERE = paths.ASSET_DIR

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

APP_NAME = "Aether Remote"

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
NIF_INFO = 0x10

TPM_RIGHTBUTTON = 0x0002
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001

IDM_OPEN, IDM_PAIR, IDM_ADD, IDM_RESCAN, IDM_START, IDM_STOP, IDM_QUIT = (
    1001, 1002, 1003, 1004, 1005, 1006, 1007)
IDM_UPDATE = 1008

# What the last update check found. Filled in on a background thread so a
# slow or dead network can never hold up the tray icon appearing.
UPD = {"s": None}


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HANDLE), ("szTip", ctypes.c_wchar * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", ctypes.c_wchar * 256), ("uTimeout", wt.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wt.DWORD)]


def cfg():
    try:
        with open(paths.data("config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"port": 8787}


def bound():
    """Where the server actually bound, as it reported on startup."""
    try:
        with open(paths.data("bound.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def server_running():
    """Is the server up?

    Two traps here, both hit during testing:
      - 127.0.0.1 alone is wrong: bound to the tailnet address only, localhost
        refuses and we would wrongly start a second server.
      - Probing by binding 0.0.0.0 is wrong on Windows, where that SUCCEEDS
        even while another socket holds the port on a specific address.
    So: connect to the address the server said it bound to.
    """
    import socket
    port = cfg().get("port", 8787)
    hosts = []
    b = bound()
    if b.get("host") and b["host"] != "0.0.0.0":
        hosts.append(b["host"])
    hosts += ["127.0.0.1"]
    for h in hosts:
        s = socket.socket()
        s.settimeout(0.6)
        try:
            if s.connect_ex((h, port)) == 0:
                return True
        except Exception:
            pass
        finally:
            s.close()
    return False


def local_url():
    """The URL to open on THIS PC - which is not localhost when the server
    is bound to a single address."""
    b = bound()
    if b.get("url"):
        return b["url"]
    return "http://127.0.0.1:%d/" % cfg().get("port", 8787)


def phone_url():
    """The address a phone should use - the secure tailnet name when the
    server is on HTTPS (the only address Face ID works on), else the tailnet
    IP, else the LAN."""
    b = bound()
    if b.get("https") and b.get("url"):
        return b["url"]
    try:
        import remote
        ip = remote.tailscale_ip()
        if ip:
            return "http://%s:%d/" % (ip, cfg().get("port", 8787))
        ips = remote.lan_ips()
        good = [i for i in ips if not i.startswith(("169.254.", "192.168.56."))]
        if good:
            return "http://%s:%d/" % (good[0], cfg().get("port", 8787))
    except Exception:
        pass
    return local_url()


# ------------------------------------------------------------ server control

class Server:
    def __init__(self):
        self.proc = None
        self.stop_flag = False
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if server_running():
            # Something already owns the port (the scheduled task, or another
            # copy). Don't start a second one that can only fail to bind.
            return
        self.stop_flag = False
        self.thread = threading.Thread(target=self._supervise, daemon=True)
        self.thread.start()

    def _supervise(self):
        backoff = 3
        while not self.stop_flag:
            try:
                self.proc = subprocess.Popen(
                    paths.child(paths.MODE_SERVER), cwd=HERE,
                    creationflags=0x08000000,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            except Exception:
                time.sleep(30)
                continue
            started = time.time()
            self.proc.wait()
            if self.stop_flag:
                return
            # Ours died but the port is answering: the supervisor (or another
            # copy) has it. Don't fight it for the port - step back.
            time.sleep(3)
            if server_running():
                self.proc = None
                return
            backoff = 3 if time.time() - started > 60 else min(backoff * 2, 60)
            time.sleep(backoff)

    def stop(self):
        self.stop_flag = True
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None


SERVER = Server()


# ---------------------------------------------------------------- dialogs

def _tk():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return tk, root


def add_app_dialog():
    """Native file picker -> a launcher the phone can use.

    This is the bit that is genuinely nicer on the PC than on the phone: you
    already know where the program is.
    """
    tk, root = _tk()
    from tkinter import filedialog, messagebox, simpledialog
    try:
        path = filedialog.askopenfilename(
            title="Pick a program to add",
            filetypes=[("Programs", "*.exe;*.lnk;*.url;*.bat"),
                       ("All files", "*.*")],
            initialdir=os.environ.get("ProgramFiles", "C:\\"))
        if not path:
            return
        default = os.path.splitext(os.path.basename(path))[0]
        name = simpledialog.askstring("Name it", "Show this app as:",
                                      initialvalue=default, parent=root)
        if not name:
            return

        import layout, library
        item_id = "custom-" + "".join(c if c.isalnum() else "-"
                                      for c in path.lower())[-60:]
        store = paths.data("custom_apps.json")
        try:
            with open(store, encoding="utf-8") as f:
                apps = json.load(f)
        except Exception:
            apps = []
        apps = [a for a in apps if a["id"] != item_id]
        apps.append({"id": item_id, "name": name[:60], "kind": "app",
                     "source": "Added by you", "launch": path,
                     "installdir": os.path.dirname(path)})
        with open(store, "w", encoding="utf-8") as f:
            json.dump(apps, f, indent=1)

        layout.icon_for(path)          # pull its icon now so the tile is ready

        lay = layout.load()
        sec = next((s for s in lay["sections"] if s["id"] == "apps"), None)
        if sec is None:
            sec = {"id": "apps", "name": "Apps", "tiles": []}
            lay["sections"].append(sec)
        if not any(t.get("ref") == item_id for t in sec["tiles"]):
            sec["tiles"].append(layout.tile("app", 2, 1, ref=item_id,
                                            label=name[:60], launch=path))
            layout.save(lay)

        messagebox.showinfo(APP_NAME,
                            "%s added.\nIt is on your phone now, in Apps." % name,
                            parent=root)
    finally:
        root.destroy()


def pair_dialog():
    """Show the URL, a QR of it, and the current authenticator code."""
    tk, root = _tk()
    from tkinter import messagebox
    try:
        url = phone_url()
        img_path = paths.data("pair-qr.png")
        have_qr = False
        try:
            import qrcode
            qr = qrcode.QRCode(box_size=7, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            qr.make_image(fill_color="#f1f0f7",
                          back_color="#0b0a18").convert("RGB").save(img_path)
            have_qr = True
        except Exception:
            pass

        win = tk.Toplevel(root)
        win.title("Pair a phone")
        win.configure(bg="#0b0a18")
        win.attributes("-topmost", True)
        win.resizable(False, False)

        tk.Label(win, text="Aether Remote", bg="#0b0a18", fg="#a78bfa",
                 font=("Segoe UI", 15, "bold")).pack(pady=(16, 2))
        tk.Label(win, text="Scan this, or type the address",
                 bg="#0b0a18", fg="#7c8598",
                 font=("Segoe UI", 9)).pack(pady=(0, 10))

        if have_qr:
            photo = tk.PhotoImage(file=img_path)
            lbl = tk.Label(win, image=photo, bg="#0b0a18", bd=0)
            lbl.image = photo          # keep a reference or tk drops it
            lbl.pack(padx=22)

        entry = tk.Entry(win, width=34, justify="center",
                         font=("Consolas", 10), bg="#14122a", fg="#f1f0f7",
                         relief="flat", insertbackground="#f1f0f7")
        entry.insert(0, url)
        entry.pack(pady=(12, 4), ipady=6, padx=22)
        entry.select_range(0, tk.END)

        try:
            import auth
            tk.Label(win, text="Code right now:  %s" % auth.current_code(),
                     bg="#0b0a18", fg="#f1f0f7",
                     font=("Consolas", 12)).pack(pady=(8, 2))
            tk.Label(win, text="(changes every 30s - use your authenticator)",
                     bg="#0b0a18", fg="#7c8598",
                     font=("Segoe UI", 8)).pack(pady=(0, 6))
        except Exception:
            pass

        def copy_it():
            root.clipboard_clear()
            root.clipboard_append(url)

        tk.Button(win, text="Copy address", command=copy_it, relief="flat",
                  bg="#6d28d9", fg="white", activebackground="#7c3aed",
                  font=("Segoe UI", 9), bd=0, padx=14, pady=6).pack(pady=(4, 6))
        tk.Button(win, text="Close", command=win.destroy, relief="flat",
                  bg="#14122a", fg="#cbd5e1", font=("Segoe UI", 9),
                  bd=0, padx=14, pady=5).pack(pady=(0, 16))

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        win.geometry("+%d+%d" % ((win.winfo_screenwidth() - w) // 2,
                                 (win.winfo_screenheight() - h) // 2))
        win.wait_window()
    finally:
        root.destroy()


def setup_done():
    """Has the first-run wizard been finished on this PC?

    Read straight off disk rather than asked over HTTP - the tray has to
    answer this while the server is still coming up.
    """
    try:
        with open(paths.data("setup.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("done"))
    except Exception:
        return False


def rescan():
    try:
        import urllib.request
        # plain-http /ping still answers on the same port under HTTPS
        urllib.request.urlopen(bound().get("ping") or (local_url() + "ping"),
                               timeout=3).read()
    except Exception:
        pass
    try:
        import library
        library._art_cache = None
        n = len(library.scan_all())
        notify("Rescanned", "%d games and apps found." % n)
    except Exception as e:
        notify("Rescan failed", str(e)[:180])


# ------------------------------------------------------------- the tray icon

HWND = None
NID = None
_menu_cb = None


def notify(title, msg):
    if not HWND or not NID:
        return
    nid = NOTIFYICONDATA()
    ctypes.memmove(ctypes.byref(nid), ctypes.byref(NID), ctypes.sizeof(nid))
    nid.uFlags = NIF_INFO
    nid.szInfoTitle = title[:60]
    nid.szInfo = msg[:250]
    nid.dwInfoFlags = 0
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))


def _update_watch():
    """Notice a new release, say so once, then be quiet.

    Once per version, not once per launch: someone who leaves their PC on for
    a month should be told once, and someone who reboots daily should not be
    told thirty times.
    """
    time.sleep(20)                 # let the tray and server settle first
    while True:
        try:
            s = update.state()
            UPD["s"] = s
            if s.get("available") and s.get("seen") != s.get("latest"):
                update.mark_seen(s["latest"])
                notify(APP_NAME, "Version %s is available. Right-click here, "
                                 "or open the desktop page, to install it."
                       % s["latest"])
        except Exception:
            pass
        time.sleep(6 * 60 * 60)


def _load_icon():
    """Our own .ico if it is beside us, else the default application icon."""
    ico = paths.asset("aether.ico")
    if os.path.isfile(ico):
        # Ask for the SMALL icon size explicitly. With LR_DEFAULTSIZE (or
        # 0x0) Windows hands back the 32px entry and the tray shrinks it,
        # which throws away the separate 16px drawing inside the .ico - the
        # whole reason there is one. SM_CXSMICON/SM_CYSMICON are 49/50, and
        # they are not always 16 (they scale with DPI), so ask rather than
        # assume.
        cx = user32.GetSystemMetrics(49) or 16
        cy = user32.GetSystemMetrics(50) or 16
        h = user32.LoadImageW(None, ico, 1, cx, cy, 0x00008000)  # LR_LOADFROMFILE
        if h:
            return h
        # Some .ico files have no matching entry; let Windows scale instead
        # of falling all the way back to a generic Windows icon.
        h = user32.LoadImageW(None, ico, 1, 0, 0, 0x00000010 | 0x00008000)
        if h:
            return h
    return user32.LoadIconW(None, ctypes.c_wchar_p(32512))  # IDI_APPLICATION


def _show_menu(hwnd):
    running = server_running()
    done = setup_done()
    menu = user32.CreatePopupMenu()
    user32.AppendMenuW(menu, MF_STRING, IDM_OPEN,
                       "Open remote" if done else "Finish setup…")
    user32.AppendMenuW(menu, MF_STRING | (0 if done else MF_GRAYED),
                       IDM_PAIR, "Pair a phone…")
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING, IDM_ADD, "Add an app…")
    user32.AppendMenuW(menu, MF_STRING, IDM_RESCAN, "Rescan games")
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING | (MF_GRAYED if running else 0),
                       IDM_START, "Start")
    user32.AppendMenuW(menu, MF_STRING | (0 if running else MF_GRAYED),
                       IDM_STOP, "Stop")
    s = UPD["s"]
    if s and s.get("available"):
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_UPDATE,
                           "Update to %s…" % s["latest"])
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING, IDM_QUIT, "Quit")

    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    # Required or the menu will not dismiss when you click elsewhere.
    user32.SetForegroundWindow(hwnd)
    user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, 0, 0, 0)
    user32.DestroyMenu(menu)


def _on_command(cmd):
    if cmd == IDM_OPEN:
        # The desktop app, not the phone page stretched across a monitor -
        # or the wizard, if this PC has never been through it.
        webbrowser.open(local_url().rstrip("/") +
                        ("/pc" if setup_done() else "/setup"))
    elif cmd == IDM_PAIR:
        threading.Thread(target=pair_dialog, daemon=True).start()
    elif cmd == IDM_ADD:
        threading.Thread(target=add_app_dialog, daemon=True).start()
    elif cmd == IDM_RESCAN:
        threading.Thread(target=rescan, daemon=True).start()
    elif cmd == IDM_START:
        SERVER.start()
        notify(APP_NAME, "Started.")
    elif cmd == IDM_STOP:
        SERVER.stop()
        notify(APP_NAME, "Stopped. Your phone cannot reach this PC now.")
    elif cmd == IDM_UPDATE:
        # The desktop page owns the update conversation - notes, the
        # button, the "no thanks". No point building a second one in a
        # tray menu.
        webbrowser.open(local_url().rstrip("/") + "/pc")
    elif cmd == IDM_QUIT:
        user32.DestroyWindow(HWND)


# On 64-bit Windows a message parameter and a handle are 64 bits, but
# ctypes.wintypes.LPARAM/WPARAM are 32-bit longs and the default restype of a
# foreign function is c_int. Left alone, DefWindowProcW raises
# "OverflowError: int too long to convert" on the first real message and the
# window silently stops working. Declare the pointer-sized types explicitly.
LRESULT = ctypes.c_ssize_t
WPARAM_T = ctypes.c_size_t
LPARAM_T = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM_T, LPARAM_T)

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, WPARAM_T, LPARAM_T]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wt.HWND
# ...and its argtypes too: the hInstance in slot 11 is a 64-bit handle that
# ctypes otherwise tries to squeeze into a c_int.
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, wt.LPVOID]
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, WPARAM_T, wt.LPCWSTR]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, WPARAM_T, LPARAM_T]
user32.LoadIconW.restype = wt.HANDLE
user32.LoadImageW.restype = wt.HANDLE
user32.CreatePopupMenu.restype = wt.HMENU
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.CreateMutexW.restype = wt.HANDLE


def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_TRAY:
        if lparam == WM_RBUTTONUP:
            _show_menu(hwnd)
        elif lparam == WM_LBUTTONDBLCLK:
            webbrowser.open(local_url())
        return 0
    if msg == WM_COMMAND:
        _on_command(wparam & 0xFFFF)
        return 0
    if msg == WM_DESTROY:
        if NID:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(NID))
        SERVER.stop()
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


def main():
    global HWND, NID, _menu_cb

    # One instance only - a second tray icon controlling the same port is
    # nothing but confusing.
    mutex = kernel32.CreateMutexW(None, True, "AetherRemoteTraySingleton")
    if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        return

    SERVER.start()

    _menu_cb = WNDPROC(_wndproc)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASS()
    wc.lpfnWndProc = _menu_cb
    wc.hInstance = hinst
    wc.lpszClassName = "AetherRemoteTray"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        return

    HWND = user32.CreateWindowExW(0, "AetherRemoteTray", APP_NAME, 0,
                                  0, 0, 0, 0, None, None, hinst, None)

    NID = NOTIFYICONDATA()
    NID.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    NID.hWnd = HWND
    NID.uID = 1
    NID.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    NID.uCallbackMessage = WM_TRAY
    NID.hIcon = _load_icon()
    NID.szTip = APP_NAME
    ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(NID))

    # Leave proof that the icon really went in. Checking from outside with
    # FindWindow is unreliable for a hidden message window, and "the process
    # is alive" is not the same as "the tray icon appeared".
    try:
        with open(paths.data("tray.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "icon": bool(ok),
                       "hwnd": int(HWND or 0), "at": time.time()}, f)
    except Exception:
        pass

    time.sleep(1.2)

    threading.Thread(target=_update_watch, daemon=True).start()

    if setup_done():
        notify(APP_NAME, "Running. Right-click the tray icon to pair a phone.")
    else:
        # First launch on this PC: open the wizard rather than leaving a new
        # user to find a tray icon they have no reason to look for. Wait for
        # the server to actually answer first - a connection-refused page is
        # a terrible first impression.
        def _first_run():
            for _ in range(30):
                if server_running():
                    break
                time.sleep(0.5)
            notify(APP_NAME, "Let's get you set up.")
            webbrowser.open(local_url().rstrip("/") + "/setup")
        threading.Thread(target=_first_run, daemon=True).start()

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
