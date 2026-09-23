# Aether Homelab (add-on)

Control your Proxmox server from the same phone app as your PC. It shows up in
Aether Remote's PC switcher as another device, with the same tile layout — but
the tiles drive the server:

- **Server stats** — node CPU, RAM, disk and load, live
- **VMs & containers** — every guest with a status dot; tap to start or shut down
- **Services** — restart the systemd units or Docker containers you choose

Same login as Aether Remote: a 6-digit code from your authenticator app, then a
signed 30-day session. It is meant for your LAN or Tailscale — never expose it
to the internet.

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

## Settings

`/var/lib/aether-homelab/config.json`:

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
