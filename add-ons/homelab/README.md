# Aether Homelab (add-on)

Control your Proxmox server from the same phone app as your PC. It runs the
**exact same app** as Aether Remote — hold a tile to edit, drag to move, drag
the corner to resize, sections, themes, the PIN, the PC switcher — only the
tiles are homelab tiles:

- **VMs & containers** — status and live CPU/RAM; tap for Start, Shut down,
  Reboot and Force stop (the last three ask for your PIN)
- **Server stats** — CPU, RAM, disk, load, swap, uptime, network, guests
  running, each ZFS pool's health, and your last backup
- **Services** — restart systemd units or Docker containers running inside
  the add-on's own container
- **Links** — shortcuts to your services' web pages, with your own pictures

Plus a **manage page** for a computer's browser at `http://<address>:8788/manage`:
which VMs/services/containers the phone may control, links and their pictures,
updates, and signing every phone out.

Same login as Aether Remote: a 6-digit code from your authenticator app, then a
signed 30-day session. It is meant for your LAN or Tailscale — never expose it
to the internet.

In Aether Remote, add it to the PC switcher as `<address>:8788` (the port
matters — the PC's is 8787). Switching carries your list of PCs along, so the
homelab can switch back to your PC too.

## Install

In the **Proxmox host shell** (Datacenter → your node → Shell), paste:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/builtbycodyrd/aether-remote/main/add-ons/homelab/install.sh)"
```

Press Enter to take each default. When it finishes it prints a QR code — scan it
with your authenticator app — and the address to open on your phone. In Aether
Remote, add that address to your PC switcher.

### What the installer does (and nothing else)

- Creates one small **unprivileged** Debian container (1 core, 512 MB, 4 GB).
- Creates a Proxmox API user `aether@pve` whose role, `AetherPower`, allows only
  **`VM.PowerMgmt`, `VM.Audit`, `Sys.Audit`** — start/stop guests and read status.
  It cannot create, delete, clone, back up, or open a console on anything.
- Checks that token against Proxmox before using it.
- Installs the add-on as a **non-root** service (`aether` user) inside the
  container. The token and your login secret live in
  `/var/lib/aether-homelab/`, readable only by that user.
- The login secret is never sent over the network; it's shown only in the shell.

## Updates

Same idea as the PC app:

- A banner appears in the add-on when a new version is out — **More info** shows
  what changed, **Update** installs it, **✕** hides that version.
- If you said yes to automatic updates during install, it updates itself daily.
- Or type **`update`** in the container's console.

An update that doesn't start cleanly is rolled back to the version you had.
Your login and settings are never touched by an update.

Running the installer again finds the existing container and offers to update
it, or to replace its Proxmox token (use that if you ever think it leaked).

## Commands inside the container

| Command | What it does |
|---|---|
| `update` | install the newest version |
| `aether-homelab status` | version, service state, address, update status |
| `aether-homelab code` | show the login QR again (e.g. for a new phone) |
| `aether-homelab reset-login` | new authenticator secret, signs every phone out |
| `aether-homelab reset-pin` | turn the PIN off (if you forgot it) |

## The PIN

Shutting down or force-stopping a VM or container needs a PIN every time;
starting one doesn't. The first time you try, it asks you to pick one (4 to 12
digits). Once a PIN is set, opening the page asks for it too, and it locks
again after 5 minutes in the background. The server enforces all of this, not
just the page.

**Settings → Face ID & PIN** changes it or turns it off (it asks for the
current one first). Five wrong tries lock it for 5 minutes, then longer each
time. A forgotten PIN can only be reset from the container: `aether-homelab
reset-pin`.

Face ID isn't offered here: it needs a secure (https) address, and the add-on
is reached over plain http on your home network.

## Settings

The easy way is the manage page, `http://<address>:8788/manage`. Underneath,
it all lives in `/var/lib/aether-homelab/config.json`:

- `show.services` — systemd units to show restart tiles for, e.g.
  `[{"unit": "nginx", "label": "Nginx"}]`
- `show.containers` — Docker container names, or `"all"`
- `show.guests` — `"all"`, or a list of VM/CT IDs to limit it to
- `auto_update` — `true` / `false`

Restart after editing: `systemctl restart aether-homelab`.

The phone can only act on what's listed there — the server refuses anything
else, even from a signed-in phone.

Note: service and Docker tiles control whatever runs **in the add-on's own
container**. To restart something that lives in another guest, restart that
guest from its VM tile.

## Uninstall

On the Proxmox host:

```bash
pct stop <CTID> && pct destroy <CTID>
pveum user delete aether@pve && pveum role delete AetherPower
```

## Versions

Add-on versions are tags named `homelab-vX.Y.Z` in this repo — separate from
the Windows app's releases. See [CHANGELOG.md](CHANGELOG.md).

## For developers: one phone app, two servers

`web/shared/` (ui.html, app.js, login.html, icons) is a copy of the Windows
app's phone app — the add-on ships its own copy because an install or update
only takes this folder. Never edit it here: change the files at the repo root,
then run `python3 add-ons/homelab/sync_ui.py`. `sync_ui.py --check` fails if
the copy is out of date. The app asks `/api/platform` which kind of server it's
on and swaps tile types and PC-only tools accordingly.
