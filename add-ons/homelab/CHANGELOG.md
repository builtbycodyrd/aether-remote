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
