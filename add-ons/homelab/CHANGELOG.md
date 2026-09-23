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
