#!/bin/sh
# Update the Aether homelab add-on to the newest homelab-vX.Y.Z tag.
#
# Runs as the service user (root is dropped to it). Downloads the tag, checks
# it compiles, swaps the program folder, restarts the service, waits for it to
# answer - and puts the old version back if it doesn't. Your data in
# /var/lib/aether-homelab is never touched.
#
#   update            update if there's something newer
#   update --force    reinstall the newest version even if already on it
set -eu

SVC_USER=aether
if [ "$(id -u)" = 0 ]; then
  exec runuser -u "$SVC_USER" -- /bin/sh "$0" "$@"
fi

# Run from a copy, so swapping the program folder can't pull this script out
# from under itself mid-run.
if [ -z "${AETHER_UPD_COPY:-}" ]; then
  APP="$(cd "$(dirname "$0")" && pwd)"
  tmp="$(mktemp)"
  cp "$0" "$tmp"
  AETHER_UPD_COPY=1 AETHER_APP="$APP" exec /bin/sh "$tmp" "$@"
fi

rm -f "$0" 2>/dev/null || true       # the temp copy; the shell already has it open
APP="$AETHER_APP"
BASE="$(dirname "$APP")"
REPO="builtbycodyrd/aether-remote"
CODELOAD="${AETHER_HL_CODELOAD:-https://codeload.github.com}"   # override = tests only
DATA="${AETHER_HL_DATA:-/var/lib/aether-homelab}"
PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 8788))' \
        "$DATA/config.json" 2>/dev/null || echo 8788)"
say() { echo "[update] $*"; }

CUR="$(cd "$APP" && python3 -c 'import version; print(version.TAG)')"
TAG="$(cd "$APP" && AETHER_HL_DATA="$DATA" python3 updater.py latest)"
if [ -z "$TAG" ]; then say "couldn't find a release on GitHub"; exit 1; fi
if [ "$TAG" = "$CUR" ] && [ "${1:-}" != "--force" ]; then
  say "already on the newest version ($CUR)"; exit 0
fi
say "updating $CUR -> $TAG"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL "$CODELOAD/$REPO/tar.gz/refs/tags/$TAG" | tar xz -C "$work"
src="$(find "$work" -maxdepth 3 -type d -path '*/add-ons/homelab' | head -n 1)"
if [ -z "$src" ] || [ ! -f "$src/server.py" ]; then
  say "the download doesn't look right - nothing changed"; exit 1
fi
# Refuse anything that doesn't even compile, before touching the live copy.
python3 -m py_compile "$src"/*.py

rm -rf "$BASE/app.new" "$BASE/app.old"
cp -a "$src" "$BASE/app.new"
mv "$APP" "$BASE/app.old"
mv "$BASE/app.new" "$APP"

restart() {
  # The service runs as us, so ending its main process is allowed, and
  # systemd (Restart=always) starts the new code straight back up.
  pid="$(systemctl show -p MainPID --value aether-homelab 2>/dev/null || echo 0)"
  # (it may already have exited - a crashing build does - which is fine)
  if [ "${pid:-0}" -gt 0 ]; then kill "$pid" 2>/dev/null || true; fi
}
alive() {
  i=0
  while [ "$i" -lt 30 ]; do
    if curl -fsS "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1; then return 0; fi
    sleep 1; i=$((i + 1))
  done
  return 1
}

restart
sleep 3
if alive; then
  rm -rf "$BASE/app.old"
  say "now on $TAG"
else
  say "the new version didn't come up - rolling back to $CUR"
  rm -rf "$APP"
  mv "$BASE/app.old" "$APP"
  restart
  sleep 3
  if alive; then say "rolled back to $CUR"
  else say "rollback didn't come up either - see: journalctl -u aether-homelab"; fi
  exit 1
fi
