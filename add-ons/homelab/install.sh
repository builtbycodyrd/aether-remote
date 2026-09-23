#!/usr/bin/env bash
# Aether Homelab - one-line installer for Proxmox VE.
#
# Run it in the Proxmox HOST shell (Datacenter > your node > Shell), as root:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/builtbycodyrd/aether-remote/main/add-ons/homelab/install.sh)"
#
# What it does, and nothing else:
#   - creates one small unprivileged Debian container for the add-on
#   - creates a Proxmox API token that can ONLY start/stop guests and read
#     status (role AetherPower: VM.PowerMgmt, VM.Audit, Sys.Audit)
#   - installs the add-on as a non-root service inside that container
#   - prints a QR code to scan into your authenticator app
#
# Run it again later and it finds the existing container and updates it.
set -euo pipefail

REPO="builtbycodyrd/aether-remote"
GH_API="${AETHER_GH_API:-https://api.github.com}"               # overrides = tests only
CODELOAD="${AETHER_CODELOAD:-https://codeload.github.com}"
ROLE="AetherPower"
PRIVS="VM.PowerMgmt VM.Audit Sys.Audit"
PVE_USER="aether@pve"
TOKEN_NAME="homelab"
CT_TAG="aether-homelab"

b=$'\e[1m'; g=$'\e[32m'; y=$'\e[33m'; r=$'\e[31m'; n=$'\e[0m'
say()  { echo "${g}>>${n} $*"; }
warn() { echo "${y}!!${n} $*"; }
die()  { echo "${r}xx $*${n}" >&2; exit 1; }
ask()  { local q="$1" d="$2" a; read -rp "   $q [$d]: " a; echo "${a:-$d}"; }

# ---------------------------------------------------------------- preflight
[ "$(id -u)" = 0 ] || die "run this as root in the Proxmox host shell"
for c in pct pveum pvesh pveam pvesm python3 curl; do
  command -v "$c" >/dev/null || die "'$c' not found - is this a Proxmox VE host?"
done

TMP="$(mktemp -d)"; chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
NODE="$(hostname -s)"            # the Proxmox node name is the short hostname
yes_() { case "$1" in y|Y|yes|Yes|YES) return 0 ;; *) return 1 ;; esac; }

# ------------------------------------------------ which version to install
if [ -n "${AETHER_REF:-}" ]; then
  REF="$AETHER_REF"                                  # e.g. refs/heads/main
else
  TAG="$(curl -fsSL "$GH_API/repos/$REPO/tags?per_page=100" | python3 -c '
import json, re, sys
best = None
for t in json.load(sys.stdin):
    m = re.fullmatch(r"homelab-v(\d+)\.(\d+)\.(\d+)", t["name"])
    if m:
        v = tuple(map(int, m.groups()))
        if best is None or v > best[0]:
            best = (v, t["name"])
print(best[1] if best else "")')" || die "couldn't reach GitHub from this host"
  [ -n "$TAG" ] || die "couldn't find a released version on GitHub"
  REF="refs/tags/$TAG"
fi

# ------------------------------------------------------------ API token
make_token() {
  # Role: power + read only. Nothing that can create, delete or open a shell.
  if pveum role list --output-format json | python3 -c \
      'import json,sys; sys.exit(0 if any(r["roleid"]=="'"$ROLE"'" for r in json.load(sys.stdin)) else 1)'; then
    pveum role modify "$ROLE" -privs "$PRIVS"
  else
    pveum role add "$ROLE" -privs "$PRIVS"
  fi
  if ! pveum user list --output-format json | python3 -c \
      'import json,sys; sys.exit(0 if any(u["userid"]=="'"$PVE_USER"'" for u in json.load(sys.stdin)) else 1)'; then
    pveum user add "$PVE_USER" -comment "Aether Homelab add-on (power + read only)"
  fi
  pveum acl modify / -user "$PVE_USER" -role "$ROLE"
  # A token's secret is shown once, so an old one can't be reused - replace it.
  pveum user token remove "$PVE_USER" "$TOKEN_NAME" >/dev/null 2>&1 || true
  pveum user token add "$PVE_USER" "$TOKEN_NAME" -privsep 0 --output-format json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])' > "$TMP/secret"
  chmod 600 "$TMP/secret"
}

check_token() {
  # Prove the token works before handing it to the add-on.
  printf 'Authorization: PVEAPIToken=%s!%s=%s\n' "$PVE_USER" "$TOKEN_NAME" "$(cat "$TMP/secret")" > "$TMP/hdr"
  curl -fsSk -H @"$TMP/hdr" "https://127.0.0.1:8006/api2/json/nodes/$NODE/status" >/dev/null \
    || die "Proxmox rejected the new API token for node '$NODE'"
}

write_config() {   # $1 = file, $2 = host ip, $3 = auto_update (1/0)
  SECRET="$(cat "$TMP/secret")" python3 - "$2" "$NODE" "$PVE_USER!$TOKEN_NAME" "$3" > "$1" <<'PY'
import json, os, sys
ip, node, tokid, auto = sys.argv[1:5]
print(json.dumps({
    "port": 8788, "host": "0.0.0.0",
    "proxmox": {"host": ip, "port": 8006, "node": node, "token_id": tokid,
                "token_secret": os.environ["SECRET"], "verify_tls": False},
    "show": {"guests": "all", "services": [], "containers": []},
    "auto_update": auto == "1",
}, indent=2))
PY
  chmod 600 "$1"
}

# --------------------------------------------------- already installed?
EXISTING="$(grep -l "^tags:.*$CT_TAG" /etc/pve/lxc/*.conf 2>/dev/null | head -n 1 || true)"
if [ -n "$EXISTING" ]; then
  CTID="$(basename "$EXISTING" .conf)"
  echo
  say "Aether Homelab is already installed in container ${b}$CTID${n}."
  echo "   u = update it to the newest version"
  echo "   t = replace its Proxmox API token (e.g. if you think it leaked)"
  echo "   q = quit"
  choice="$(ask "What do you want to do?" u)"
  case "$choice" in
    u|U)
      pct exec "$CTID" -- /usr/local/bin/update || true
      pct exec "$CTID" -- /usr/local/bin/aether-homelab status
      exit 0 ;;
    t|T)
      make_token; check_token
      pct exec "$CTID" -- cat /var/lib/aether-homelab/config.json > "$TMP/old.json"
      # Swap only the token; keep every other setting you've changed.
      SECRET="$(cat "$TMP/secret")" python3 - "$TMP/old.json" "$PVE_USER!$TOKEN_NAME" \
        > "$TMP/config.json" <<'PY'
import json, os, sys
c = json.load(open(sys.argv[1]))
c.setdefault("proxmox", {})["token_id"] = sys.argv[2]
c["proxmox"]["token_secret"] = os.environ["SECRET"]
print(json.dumps(c, indent=2))
PY
      chmod 600 "$TMP/config.json"
      pct push "$CTID" "$TMP/config.json" /var/lib/aether-homelab/config.json
      pct exec "$CTID" -- sh -c 'chown aether:aether /var/lib/aether-homelab/config.json && chmod 600 /var/lib/aether-homelab/config.json && systemctl restart aether-homelab'
      say "New token in place; the old one no longer works."
      exit 0 ;;
    *) exit 0 ;;
  esac
fi

# ---------------------------------------------------------------- settings
echo
echo "${b}Aether Homelab installer${n}  (installing $REF)"
echo "Press Enter to accept each default."
echo
CTID="$(ask "Container ID" "$(pvesh get /cluster/nextid)")"
HN="$(ask "Hostname" "aether-homelab")"
STORAGES="$(pvesm status -content rootdir | awk 'NR>1 && $3=="active" {print $1}')"
[ -n "$STORAGES" ] || die "no active storage can hold containers"
echo "   Storage for the container disk: $(echo "$STORAGES" | tr '\n' ' ')"
STORAGE="$(ask "Storage" "$(echo "$STORAGES" | head -n 1)")"
BRIDGE="$(ask "Network bridge" "vmbr0")"
echo "   IP: 'dhcp', or a fixed address like 192.168.1.60/24 (fixed is better -"
echo "       your phone saves the address)"
IP="$(ask "IP" "dhcp")"
GW=""
if [ "$IP" != "dhcp" ]; then
  GW="$(ask "Gateway" "$(ip route | awk '/^default/ {print $3; exit}')")"
fi
if yes_ "$(ask "Install updates automatically? (y/n)" "y")"; then AUTO=1; else AUTO=0; fi

HOST_IP="$(ip -4 -o addr show "$BRIDGE" | awk '{print $4}' | cut -d/ -f1 | head -n 1)"
[ -n "$HOST_IP" ] || die "couldn't find this host's IP on $BRIDGE"

echo
echo "   Container $CTID '$HN' on $STORAGE, $BRIDGE, IP $IP, 1 core / 512 MB / 4 GB"
echo "   Proxmox API user $PVE_USER with role $ROLE ($PRIVS)"
echo "   Auto-update: $([ "$AUTO" = 1 ] && echo on || echo off)"
yes_ "$(ask "Go ahead? (y/n)" "y")" || die "cancelled - nothing was changed"

# -------------------------------------------------------------- template
say "Finding a Debian template"
pveam update >/dev/null 2>&1 || warn "template list refresh failed; using what's cached"
pick_tpl() { pveam available --section system | awk '{print $2}' | { grep -E "^$1" || true; } | sort -V | tail -n 1; }
TPL="$(pick_tpl debian-13-standard)"
[ -n "$TPL" ] || TPL="$(pick_tpl debian-12-standard)"
[ -n "$TPL" ] || die "no Debian template available"
TSTORE="$(pvesm status -content vztmpl | awk 'NR>1 && $3=="active" {print $1; exit}')"
[ -n "$TSTORE" ] || die "no storage can hold templates"
if ! pveam list "$TSTORE" | grep -q "$TPL"; then
  say "Downloading $TPL"
  pveam download "$TSTORE" "$TPL" >/dev/null
fi

# ------------------------------------------------------------- container
say "Creating container $CTID"
NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -n "$GW" ] && NET="$NET,gw=$GW"
pct create "$CTID" "$TSTORE:vztmpl/$TPL" \
  -hostname "$HN" -unprivileged 1 -features nesting=1 \
  -cores 1 -memory 512 -swap 256 -rootfs "$STORAGE:4" \
  -net0 "$NET" -onboot 1 -tags "$CT_TAG" \
  -description "Aether Homelab add-on - github.com/$REPO" >/dev/null
pct start "$CTID"

say "Waiting for the container's network"
for _ in $(seq 1 60); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 1
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 \
  || die "container $CTID has no network - check the bridge/IP and re-run"

# -------------------------------------------------------------- the token
say "Creating the locked-down Proxmox API token"
make_token
check_token

# -------------------------------------------------- install inside the CT
say "Installing the add-on inside the container"
cat > "$TMP/setup.sh" <<'SETUP'
#!/bin/bash
set -euo pipefail
REF="$1"; REPO="$2"; CODELOAD="$3"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-cryptography curl ca-certificates qrencode >/dev/null
id aether >/dev/null 2>&1 || useradd --system --home-dir /var/lib/aether-homelab \
  --shell /usr/sbin/nologin aether
install -d -o aether -g aether -m 755 /opt/aether-homelab
install -d -o aether -g aether -m 700 /var/lib/aether-homelab
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
curl -fsSL "$CODELOAD/$REPO/tar.gz/$REF" | tar xz -C "$work"
src="$(find "$work" -maxdepth 3 -type d -path '*/add-ons/homelab' | head -n 1)"
[ -n "$src" ] && [ -f "$src/server.py" ] || { echo "download looks wrong"; exit 1; }
rm -rf /opt/aether-homelab/app
cp -a "$src" /opt/aether-homelab/app
chown -R aether:aether /opt/aether-homelab
install -m 644 /opt/aether-homelab/app/aether-homelab.service /etc/systemd/system/aether-homelab.service
cat > /usr/local/bin/update <<'W'
#!/bin/sh
# Update the Aether homelab add-on to the newest release.
exec runuser -u aether -- env AETHER_HL_DATA=/var/lib/aether-homelab \
  /bin/sh /opt/aether-homelab/app/update.sh "$@"
W
cat > /usr/local/bin/aether-homelab <<'W'
#!/bin/sh
umask 077
cd /opt/aether-homelab/app
exec runuser -u aether -- env AETHER_HL_DATA=/var/lib/aether-homelab \
  python3 /opt/aether-homelab/app/cli.py "$@"
W
chmod 755 /usr/local/bin/update /usr/local/bin/aether-homelab
systemctl daemon-reload
systemctl enable -q aether-homelab
SETUP
pct push "$CTID" "$TMP/setup.sh" /root/aether-setup.sh
pct exec "$CTID" -- bash /root/aether-setup.sh "$REF" "$REPO" "$CODELOAD"
pct exec "$CTID" -- rm -f /root/aether-setup.sh

write_config "$TMP/config.json" "$HOST_IP" "$AUTO"
pct push "$CTID" "$TMP/config.json" /var/lib/aether-homelab/config.json
pct exec "$CTID" -- sh -c 'chown aether:aether /var/lib/aether-homelab/config.json && chmod 600 /var/lib/aether-homelab/config.json'
pct exec "$CTID" -- systemctl start aether-homelab

say "Starting the add-on"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- curl -fsS http://127.0.0.1:8788/ping >/dev/null 2>&1 && break
  sleep 1
done
pct exec "$CTID" -- curl -fsS http://127.0.0.1:8788/ping >/dev/null 2>&1 \
  || die "the add-on didn't start - see: pct exec $CTID -- journalctl -u aether-homelab"

CT_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"

# ----------------------------------------------------------------- done
pct exec "$CTID" -- /usr/local/bin/aether-homelab code
echo
say "${b}Installed.${n}"
echo "   Open on your phone:   ${b}http://$CT_IP:8788${n}"
echo "   In Aether Remote, add it to your PC switcher with that address."
echo "   Inside the container: 'update' gets the newest version,"
echo "                         'aether-homelab status' shows how it's doing."
echo "   Run this installer again any time to update it or replace its token."
