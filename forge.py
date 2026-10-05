"""Aether Forge: the sandbox Kiln builds in.

Everything lives in ONE folder (forge/ in this install's data folder):

  forge/wsl/        the sandbox's own Linux (a private WSL distro, "AetherForge")
  forge/projects/   one folder per project - the only part of the PC the
                    sandbox can see
  forge/meta/       each project's chat and settings (outside the sandbox, so
                    code running in there can't rewrite its own history)

Inside that Linux runs Docker, and every project gets its own container
with just its own folder mounted at /work. Deleting a project removes its
container and folder; removing Forge unregisters the distro and deletes the
whole forge/ folder - nothing is left anywhere else.

How it's kept away from the rest of the PC - layered, and honest about it:
  * The container only sees /work. Its root user is NOT root in the sandbox's
    Linux (Docker user-namespace remapping), it can't gain privileges
    (no-new-privileges + Docker's default seccomp), and it has no Docker socket.
  * The sandbox's Linux mounts no Windows drives except the projects folder,
    and can't launch Windows programs (interop off).
  * Containers reach the internet (to install packages) but never this PC,
    your LAN or your tailnet: every private address range is blocked.
  * Not perfect: a bug in the Linux kernel or Docker that lets code break out
    of a container would put it in the sandbox's Linux, which could then
    mount the PC's drives. That's the remaining risk, and it's the same one
    every Docker-based sandbox has.

Needs WSL 2 (built into Windows 10/11). No Docker Desktop, no admin.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
import hashlib

import paths

DISTRO = "AetherForge"
IMAGE = "aether-forge:1"
NOWIN = 0x08000000                     # CREATE_NO_WINDOW
IDLE_STOP = 20 * 60                    # stop the sandbox after this long unused
ALPINE = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/"
SKIP = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".v", ".cache", ".mypy_cache", ".pytest_cache", "dist", "build"}

_lock = threading.RLock()
_st = {"step": None, "error": None, "busy": False, "log": []}
_holder = {"p": None}
_last = {"t": time.time(), "jobs": 0}

STEPS = [("wsl", "Checking WSL"), ("download", "Downloading Linux (Alpine, about 4 MB)"),
         ("import", "Creating the sandbox"), ("docker", "Installing Docker inside it"),
         ("start", "Starting Docker"), ("image", "Building the workshop (Python, Node, git - about 420 MB)"),
         ("done", "Ready")]

START_SH = r"""#!/bin/sh
# Aether Forge sandbox - started and held open by Aether Remote.
# $1 = the Windows path of the projects folder: the only part of the PC mounted.
set -e
rm -f /run/forge.ready
REMAP=100000
mkdir -p /projects
if ! mountpoint -q /projects; then
  mount -t drvfs "$1" /projects -o metadata,uid=$REMAP,gid=$REMAP,umask=022
fi
grep -q '^dockremap:' /etc/group || addgroup -S -g $REMAP dockremap
grep -q '^dockremap:' /etc/passwd || adduser -S -D -H -u $REMAP -G dockremap -s /sbin/nologin dockremap
echo "dockremap:$REMAP:65536" > /etc/subuid
echo "dockremap:$REMAP:65536" > /etc/subgid
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<J
{"dns":["1.1.1.1","8.8.8.8"],"userns-remap":"dockremap","log-driver":"local",
 "log-opts":{"max-size":"5m"},"no-new-privileges":true,"ip6tables":false,"ipv6":false}
J
rm -f /var/run/docker.pid
dockerd --host=unix:///var/run/docker.sock >/var/log/dockerd.log 2>&1 &
D=$!
i=0; while [ ! -S /var/run/docker.sock ] && [ $i -lt 80 ]; do sleep 0.5; i=$((i+1)); done
sleep 1
# Containers reach the internet - never this PC, the LAN or the tailnet.
iptables -F DOCKER-USER 2>/dev/null || iptables -N DOCKER-USER
iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for n in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 127.0.0.0/8 224.0.0.0/4 0.0.0.0/8 198.18.0.0/15; do
  iptables -A DOCKER-USER -i docker0 -d $n -j REJECT
done
iptables -A DOCKER-USER -j RETURN
iptables -C INPUT -i docker0 -m conntrack ! --ctstate ESTABLISHED,RELATED -j REJECT 2>/dev/null ||
  iptables -I INPUT -i docker0 -m conntrack ! --ctstate ESTABLISHED,RELATED -j REJECT
echo ready > /run/forge.ready
wait $D
"""

WSL_CONF = """[automount]
enabled = false
mountFsTab = false
[interop]
enabled = false
appendWindowsPath = false
"""

IMAGE_SH = r"""set -e
docker rm -f forge-build >/dev/null 2>&1 || true
docker run --name forge-build debian:bookworm-slim sh -c "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-pip python3-venv nodejs npm git curl ca-certificates procps less nano unzip zip file && rm -rf /var/lib/apt/lists/* && mkdir -p /work && ln -sf /usr/bin/python3 /usr/local/bin/python && printf '[global]\nbreak-system-packages = true\n' > /etc/pip.conf"
docker commit -c 'WORKDIR /work' -c 'CMD ["sleep","infinity"]' forge-build """ + IMAGE + r"""
docker rm forge-build
docker rmi debian:bookworm-slim >/dev/null 2>&1 || true
"""


# --------------------------------------------------------------- where

def _conf():
    try:
        with open(paths.data("kiln.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def root():
    r = (_conf().get("root") or "").strip()
    return os.path.abspath(r) if r else paths.data("forge")


def projects_dir():
    return os.path.join(root(), "projects")


def meta_dir():
    return os.path.join(root(), "meta")


# --------------------------------------------------------------- running wsl

def _dec(b):
    if not b:
        return ""
    if b[:200].count(b"\x00") > 4:                 # wsl.exe's own messages are UTF-16
        return b.decode("utf-16le", "replace").replace("\x00", "")
    return b.decode("utf-8", "replace")


def _run(args, timeout=60, input=None):
    try:
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NOWIN, **kw)
        return r.returncode, _dec(r.stdout), _dec(r.stderr)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except FileNotFoundError:
        return 127, "", "wsl.exe not found"


def sh(script, timeout=60, input=None):
    """Run a shell command as root in the sandbox's Linux (not in a project)."""
    if input is not None:
        return _run(["wsl", "-d", DISTRO, "-u", "root", "--exec", "sh", "-c", script], timeout, input)
    return _run(["wsl", "-d", DISTRO, "-u", "root", "--exec", "sh", "-c", script], timeout)


def wsl_ok():
    code, out, err = _run(["wsl", "--status"], 20)
    return code == 0


def distro_exists():
    code, out, _ = _run(["wsl", "-l", "-q"], 20)
    return code == 0 and DISTRO in [l.strip() for l in out.splitlines()]


def distro_running():
    code, out, _ = _run(["wsl", "-l", "--running", "-q"], 20)
    return code == 0 and DISTRO in [l.strip() for l in out.splitlines()]


def _state():
    try:
        with open(os.path.join(root(), "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(**kw):
    s = _state()
    s.update(kw)
    os.makedirs(root(), exist_ok=True)
    with open(os.path.join(root(), "state.json"), "w", encoding="utf-8") as f:
        json.dump(s, f)


def installed():
    return bool(_state().get("installed")) and distro_exists()


def _dir_size(p):
    n = 0
    for dp, dn, fn in os.walk(p):
        for f in fn:
            try:
                n += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return n


def status(sizes=False, live=True):
    s = _state()
    out = {"installed": bool(s.get("installed")), "busy": _st["busy"], "step": _st["step"],
           "stepName": dict(STEPS).get(_st["step"], ""), "steps": [k for k, _ in STEPS],
           "error": _st["error"], "root": root(), "running": False}
    if live and out["installed"] and not _st["busy"]:
        out["running"] = distro_running()
    if sizes and os.path.isdir(root()):
        out["sizeGB"] = round(_dir_size(root()) / 1e9, 2)
    return out


# --------------------------------------------------------------- install

def _step(k, msg=None):
    _st["step"] = k
    _st["log"].append("%s %s" % (time.strftime("%H:%M:%S"), msg or dict(STEPS).get(k, k)))


def install():
    """Set the sandbox up, in the background. Safe to run again: each step
    checks what's already there."""
    with _lock:
        if _st["busy"]:
            return status()
        _st.update(busy=True, error=None, log=[])
    threading.Thread(target=_install, daemon=True).start()
    return status()


def _install():
    try:
        _step("wsl")
        if not wsl_ok():
            raise RuntimeError("WSL isn't installed on this PC. Use \"Install WSL\" (needs an admin click and a restart).")
        os.makedirs(projects_dir(), exist_ok=True)
        os.makedirs(meta_dir(), exist_ok=True)
        if not distro_exists():
            _step("download")
            y = urllib.request.urlopen(ALPINE + "latest-releases.yaml", timeout=30).read().decode()
            fn = sha = None
            for b in y.split("\n-"):
                if "flavor: alpine-minirootfs" in b:
                    fn = re.search(r"file: (\S+)", b).group(1)
                    sha = re.search(r"sha256: (\S+)", b).group(1)
                    break
            if not fn:
                raise RuntimeError("Couldn't find the Alpine download")
            data = urllib.request.urlopen(ALPINE + fn, timeout=120).read()
            if hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError("The Alpine download didn't match its checksum - try again")
            tar = os.path.join(root(), fn)
            with open(tar, "wb") as f:
                f.write(data)
            _step("import")
            code, out, err = _run(["wsl", "--import", DISTRO, os.path.join(root(), "wsl"), tar, "--version", "2"], 300)
            try:
                os.remove(tar)
            except OSError:
                pass
            if code != 0:
                raise RuntimeError("Creating the sandbox failed: %s" % (err or out).strip()[:300])
        _step("docker")
        code, out, err = sh("apk update -q && apk add -q docker iptables ip6tables >/dev/null && docker --version", 600)
        if code != 0:
            raise RuntimeError("Installing Docker failed: %s" % (err or out).strip()[-300:])
        code, _, err = sh("cat > /usr/local/bin/forge-start && chmod 755 /usr/local/bin/forge-start", 30, START_SH.encode())
        code2, _, err2 = sh("cat > /etc/wsl.conf", 30, WSL_CONF.encode())
        if code or code2:
            raise RuntimeError("Setting the sandbox up failed: %s" % (err or err2)[:300])
        _run(["wsl", "--terminate", DISTRO], 60)       # wsl.conf applies on next start
        _step("start")
        start(force=True)
        _step("image")
        if not _has_image():
            code, out, err = sh(IMAGE_SH, 1800)
            if code != 0 or not _has_image():
                raise RuntimeError("Building the workshop failed: %s" % (err or out).strip()[-300:])
        _save_state(installed=True, installedAt=time.time())
        _step("done")
    except Exception as e:
        _st["error"] = str(e)[:400]
        _st["log"].append("error: " + _st["error"])
    finally:
        _st["busy"] = False


def install_wsl():
    """WSL itself is a Windows feature: turning it on needs admin (a UAC
    click at the PC) and usually a restart."""
    subprocess.Popen(["powershell", "-NoProfile", "-Command",
                      "Start-Process wsl -ArgumentList '--install','--no-distribution' -Verb RunAs"],
                     creationflags=NOWIN)


def uninstall():
    """Remove everything: the sandbox's Linux, every container, every
    project, the whole forge folder."""
    with _lock:
        stop()
        if distro_exists():
            code, out, err = _run(["wsl", "--unregister", DISTRO], 300)
            if code != 0:
                raise RuntimeError("Couldn't remove the sandbox: %s" % (err or out).strip()[:300])
        _rmtree(root())
        _st.update(step=None, error=None, log=[])
    return status()


# --------------------------------------------------------------- start/stop

def _ready():
    code, out, _ = sh("test -f /run/forge.ready && docker info --format ok", 20)
    return code == 0 and "ok" in out


def start(force=False):
    """Make sure the sandbox's Docker is up. It's held open by a hidden
    wsl.exe; if Aether restarted, an already-running sandbox is reused."""
    with _lock:
        _last["t"] = time.time()
        if not force and _ready():
            return True
        p = _holder["p"]
        if p and p.poll() is None and not force:
            pass
        else:
            os.makedirs(projects_dir(), exist_ok=True)
            _holder["p"] = subprocess.Popen(
                ["wsl", "-d", DISTRO, "-u", "root", "--exec", "/usr/local/bin/forge-start", projects_dir()],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NOWIN)
        for _ in range(90):
            time.sleep(0.5)
            if _ready():
                _reaper()
                return True
            if _holder["p"].poll() is not None:
                break
        _, log, _ = sh("tail -5 /var/log/dockerd.log", 10)
        raise RuntimeError("The sandbox didn't start. %s" % log.strip()[-300:])


def stop():
    """Stop the sandbox (containers and all). Projects stay on disk."""
    if distro_exists():
        _run(["wsl", "--terminate", DISTRO], 60)
    p = _holder["p"]
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
    _holder["p"] = None


def touch(delta=0):
    _last["t"] = time.time()
    _last["jobs"] = max(0, _last["jobs"] + delta)


_reap = {"on": False}


def _reaper():
    if _reap["on"]:
        return
    _reap["on"] = True

    def loop():
        while True:
            time.sleep(60)
            if _last["jobs"] or _st["busy"]:
                continue
            if time.time() - _last["t"] > IDLE_STOP and distro_running():
                stop()
    threading.Thread(target=loop, daemon=True).start()


def _has_image():
    code, out, _ = sh("docker image inspect %s --format ok" % IMAGE, 30)
    return code == 0 and "ok" in out


# --------------------------------------------------------------- projects

def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:28]
    return s or "project"


def _valid(pid):
    return bool(re.fullmatch(r"[a-z0-9-]{1,40}", str(pid or "")))


def pdir(pid):
    if not _valid(pid):
        raise ValueError("No such project")
    return os.path.join(projects_dir(), pid)


def _mpath(pid):
    if not _valid(pid):
        raise ValueError("No such project")
    return os.path.join(meta_dir(), pid + ".json")


def meta(pid):
    try:
        with open(_mpath(pid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_meta(m):
    os.makedirs(meta_dir(), exist_ok=True)
    tmp = _mpath(m["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f)
    os.replace(tmp, _mpath(m["id"]))


def create(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60] or "New project"
    pid = "%s-%s" % (_slug(name), secrets.token_hex(2))
    os.makedirs(pdir(pid), exist_ok=False)      # made on the Windows side: owned by the container's root
    m = {"id": pid, "name": name, "created": time.time(), "updated": time.time(), "msgs": []}
    save_meta(m)
    return m


def listing():
    out = []
    try:
        names = os.listdir(meta_dir())
    except OSError:
        names = []
    for n in names:
        if n.endswith(".json"):
            m = meta(n[:-5])
            if m and os.path.isdir(pdir(m["id"])):
                out.append({"id": m["id"], "name": m["name"], "updated": m.get("updated", 0),
                            "n": len(m.get("msgs", []))})
    return sorted(out, key=lambda p: -p["updated"])


def rename(pid, name):
    m = meta(pid)
    if not m:
        raise ValueError("No such project")
    m["name"] = re.sub(r"\s+", " ", str(name or "")).strip()[:60] or m["name"]
    save_meta(m)
    return m


def _rmtree(p):
    def onerr(fn, path, exc):
        try:
            os.chmod(path, 0o666)
            fn(path)
        except Exception:
            pass
    if os.path.isdir(p):
        shutil.rmtree(p, onerror=onerr)


def delete(pid):
    """The project's container and its folder - gone."""
    d = pdir(pid)
    if installed():
        try:
            if distro_running():
                sh("docker rm -f forge-%s >/dev/null 2>&1; true" % pid, 60)
            else:
                start()
                sh("docker rm -f forge-%s >/dev/null 2>&1; true" % pid, 60)
        except Exception:
            pass
    _rmtree(d)
    try:
        os.remove(_mpath(pid))
    except OSError:
        pass
    if os.path.isdir(d):
        raise RuntimeError("Some files couldn't be deleted (is something on the PC using them?)")


def _limits():
    try:
        import ctypes

        class M(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                        ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong), ("f", ctypes.c_ulonglong)]
        m = M()
        m.l = ctypes.sizeof(M)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        gb = max(2, min(8, int(m.total / 1e9 / 4)))
    except Exception:
        gb = 4
    cpus = max(2, (os.cpu_count() or 4) // 2)
    return gb, cpus


def ensure(pid):
    """The project's container, running. Made the first time it's needed."""
    if not installed():
        raise RuntimeError("Kiln's sandbox isn't set up yet - PC app > Settings > Chatbox > Kiln.")
    start()
    d = pdir(pid)
    if not os.path.isdir(d):
        raise ValueError("No such project")
    name = "forge-" + pid
    code, out, _ = sh("docker inspect -f '{{.State.Running}}' %s 2>/dev/null" % name, 30)
    if code == 0 and "true" in out:
        return name
    if code == 0:
        code, out, err = sh("docker start %s" % name, 60)
        if code == 0:
            return name
        sh("docker rm -f %s" % name, 30)
    gb, cpus = _limits()
    code, out, err = sh(
        "docker run -d --name %s --label aether.forge=1 --hostname kiln --memory %dg --memory-swap %dg "
        "--cpus %d --pids-limit 1024 --mount type=bind,src=/projects/%s,dst=/work -w /work %s sleep infinity"
        % (name, gb, gb, cpus, pid, IMAGE), 120)
    if code != 0:
        raise RuntimeError("Couldn't start the project's container: %s" % (err or out).strip()[-300:])
    return name


def run(pid, cmd, timeout=120, on_out=None, cancel=None):
    """Run a shell command in the project's container, in /work. Output
    (stdout and stderr together) streams to on_out(text) as it comes."""
    name = ensure(pid)
    touch()
    timeout = max(5, min(900, int(timeout or 120)))
    p = subprocess.Popen(["wsl", "-d", DISTRO, "-u", "root", "--exec", "docker", "exec", "-w", "/work",
                          "-e", "HOME=/root", "-e", "TERM=dumb", "-e", "PYTHONUNBUFFERED=1",
                          name, "timeout", "-k", "5", str(timeout), "bash", "-lc", "exec 2>&1\n" + cmd],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=NOWIN)
    buf, killed = [], {"v": False}

    def watch():
        end = time.time() + timeout + 20
        while p.poll() is None:
            if (cancel is not None and cancel.is_set()) or time.time() > end:
                killed["v"] = True
                try:
                    p.kill()
                except Exception:
                    pass
                sh("docker restart -t 0 %s >/dev/null 2>&1" % name, 60)   # takes anything left running with it
                return
            time.sleep(0.3)
    threading.Thread(target=watch, daemon=True).start()
    total = 0
    for raw in iter(lambda: p.stdout.readline(), b""):
        t = raw.decode("utf-8", "replace")
        total += len(t)
        if total < 400000:
            buf.append(t)
        if on_out:
            try:
                on_out(t)
            except Exception:
                pass
    code = p.wait()
    out = "".join(buf)
    if killed["v"] or code in (124, 137):
        code = 124 if not (cancel is not None and cancel.is_set()) else 130
    return code, out


# --------------------------------------------------------------- files (Windows side)

def safe(pid, rel):
    """A path inside the project - never outside it, never through a link."""
    base = os.path.realpath(pdir(pid))
    rel = str(rel or "").replace("\\", "/").strip()
    rel = re.sub(r"^(\./|/work/?|/)+", "", rel)
    if not rel or rel == ".":
        return base
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or any(":" in p for p in parts):
        raise ValueError("That path is outside the project")
    p = os.path.join(base, *parts)
    cur = base
    for part in parts:                         # refuse links/junctions anywhere on the way
        cur = os.path.join(cur, part)
        if os.path.lexists(cur) and (os.path.islink(cur) or _reparse(cur)):
            raise ValueError("That path goes through a link")
    real = os.path.realpath(p)
    if real != base and not real.startswith(base + os.sep):
        raise ValueError("That path is outside the project")
    return real


def _reparse(p):
    try:
        return bool(os.lstat(p).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


def rel(pid, p):
    return os.path.relpath(p, os.path.realpath(pdir(pid))).replace("\\", "/")


def tree(pid, limit=400):
    """Every file in the project (big tool folders shown, not walked)."""
    base = os.path.realpath(pdir(pid))
    out = []
    for dp, dn, fn in os.walk(base):
        dn[:] = sorted(d for d in dn if not _reparse(os.path.join(dp, d)))
        r = os.path.relpath(dp, base).replace("\\", "/")
        r = "" if r == "." else r + "/"
        for d in list(dn):
            if d in SKIP:
                out.append({"path": r + d + "/", "dir": True, "skipped": True})
                dn.remove(d)
        for f in sorted(fn):
            fp = os.path.join(dp, f)
            if _reparse(fp):
                continue
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            out.append({"path": r + f, "size": st.st_size, "mtime": int(st.st_mtime)})
            if len(out) >= limit:
                return out, True
    return out, False


def read(pid, path, limit=200000):
    p = safe(pid, path)
    if not os.path.isfile(p):
        raise ValueError("No such file: %s" % path)
    with open(p, "rb") as f:
        data = f.read(limit + 1)
    if b"\x00" in data[:4096]:
        raise ValueError("%s is a binary file" % path)
    return data[:limit].decode("utf-8", "replace"), len(data) > limit


def write(pid, path, text):
    p = safe(pid, path)
    if os.path.isdir(p):
        raise ValueError("%s is a folder" % path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return os.path.getsize(p)


def remove(pid, path):
    p = safe(pid, path)
    if p == os.path.realpath(pdir(pid)):
        raise ValueError("Can't delete the whole project that way")
    if os.path.isdir(p):
        _rmtree(p)
    elif os.path.exists(p):
        os.remove(p)
    else:
        raise ValueError("No such file: %s" % path)


def export(pid, dest_root):
    """Copy a project out of the sandbox onto the PC (the tool folders like
    node_modules are left behind - they're rebuilt from the project)."""
    m = meta(pid)
    if not m:
        raise ValueError("No such project")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", m["name"]).strip(" .") or pid
    dest = os.path.join(dest_root, name)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(dest_root, "%s (%d)" % (name, n))
        n += 1
    src = os.path.realpath(pdir(pid))

    def ignore(d, names):
        return [x for x in names if x in SKIP or _reparse(os.path.join(d, x))]
    shutil.copytree(src, dest, ignore=ignore, symlinks=True)
    return dest


def zip_bytes(pid):
    import io
    import zipfile
    base = os.path.realpath(pdir(pid))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in SKIP and not _reparse(os.path.join(dp, d))]
            for f in fn:
                fp = os.path.join(dp, f)
                if not _reparse(fp):
                    z.write(fp, os.path.relpath(fp, base))
    return buf.getvalue()
