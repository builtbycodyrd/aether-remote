# 0.3.1

- The add-on's container has no password on purpose, so its Console tab's
  login prompt is a dead end. The installer, the manage page and the README
  now say how to reach it instead: from the Proxmox host shell,
  `pct exec <ID> -- update` (or `pct enter <ID>` for a shell inside it).

# 0.3.0

**The same app as your PC - with homelab tiles.** The add-on now runs the
exact phone app Aether Remote uses, so everything you can do there works here.

- **Customize everything:** hold a tile to edit, drag to move, drag the corner
  to resize, add and rename sections, pick themes and colours in Settings.
- **Add tile** has homelab tiles:
  - **VMs & CTs** - status, live CPU and RAM (make it taller for bars). Tap one
    for Start, Shut down, Reboot and Force stop (the last three ask for your PIN).
  - **Server** - CPU, RAM, disk, load, swap, uptime, network, guests running,
    every ZFS pool's health (red when it isn't ONLINE), and your last backup.
  - **Services** - restart services running inside the add-on's container.
  - **Links** - shortcuts to your services' web pages, with your own pictures.
- **The PC switcher works both ways:** switching carries your saved PCs along,
  so the homelab can switch back to your PC too.
- **A manage page** at `http://<address>:8788/manage` on a computer: choose
  which VMs, services and containers the phone may control (PIN-protected),
  manage links and their pictures, updates, and sign every phone out.
- Everything still uses the same locked-down Proxmox token - nothing new is
  asked for. The new server stats (ZFS, network, backups) are read-only.

# 0.2.0

A PIN for the things that can't be undone.

- **VM shut downs need your PIN.** Shutting down or force-stopping a VM or
  container asks for it every time; starting one doesn't. The first time you
  try, it asks you to pick a PIN (4 to 12 digits).
- **The page locks when you open it.** Once a PIN is set, opening the page asks
  for it, and it locks again after 5 minutes in the background.
- The **PIN** button at the top changes it or turns it off (it asks for the
  current one first).
- Five wrong tries lock it for 5 minutes, then longer each time.
- Forgot it? In the container: `aether-homelab reset-pin`. There is no way to
  reset it over the network.
- `aether-homelab reset-login` now takes effect straight away (it used to wait
  for the add-on to restart).

Face ID isn't offered here: it needs a secure (https) address, and the add-on
is reached over plain http on your home network.

# 0.1.1

Installer fixes, from the first real install.

- Picks the Debian template built for your CPU (it was grabbing the ARM one)
- Downloads the add-on on the Proxmox host before changing anything, so a
  network hiccup can't leave a half-made container behind
- If an install fails part-way, it removes the container and API token it
  just made; re-running on an earlier half-finished install offers to finish it
- Refuses a mistyped storage name up front
- No more locale warnings during setup
- Updates retry a flaky download instead of failing on the first try

# 0.1.0

First release of the homelab add-on.

### What it does
- **Server stats** - node CPU, RAM, disk and load as live tiles
- **VMs and containers** - every Proxmox guest with a status dot; tap to
  start one or shut it down
- **Services** - restart the systemd units or Docker containers you list in
  the config

### Install and updates
- One-line installer run from the Proxmox host shell: creates the container,
  a locked-down API token, and the service, and prints your login QR code
- Updates like the PC app: a banner when a new version is out, optional
  automatic updates, or type `update` in the container
- An update that doesn't start cleanly is rolled back on its own
