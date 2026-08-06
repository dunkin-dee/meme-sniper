#!/usr/bin/env bash
#
# Provision a fresh Ubuntu 24.04 EC2 instance to run the recorder.
# Idempotent: safe to re-run.
#
#   sudo bash bootstrap.sh
#
set -euo pipefail

APP_DIR=/opt/meme-sniper
ETC_DIR=/etc/meme-sniper
APP_USER=sniper
REPO_SRC="${REPO_SRC:-}"   # optional: rsync/git source, otherwise copy manually

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo bash bootstrap.sh" >&2
  exit 1
fi

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# Deliberately NOT python3-pip: on Ubuntu 24.04 it drags in the whole
# build-essential/g++-13 chain, which took ~10 minutes of downloads on a
# t3.micro (measured 2026-08-05). Nothing here needs a compiler - every
# dependency is pure Python or ships a manylinux wheel - and python3-venv
# already provides pip inside the venv via ensurepip.
apt-get install -y -qq \
  python3 python3-venv \
  sqlite3 rsync curl ca-certificates chrony unattended-upgrades

# Accurate clock matters: received_at is our only time reference on launch
# frames, because the stream sends no timestamp field.
log "Enabling time sync"
systemctl enable --now chrony >/dev/null 2>&1 || systemctl enable --now chronyd

log "Creating service user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"/data "$ETC_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# litestream.service runs as $APP_USER and opens litestream.yml from here, so
# the service user needs traverse rights on the directory. root:root 750 made
# it fail on every start with "permission denied" (observed 2026-08-05).
# Secrets stay root-only regardless: systemd reads litestream.env as root via
# EnvironmentFile before dropping privileges.
chown root:"$APP_USER" "$ETC_DIR"
chmod 750 "$ETC_DIR"

if [[ -n "$REPO_SRC" ]]; then
  log "Syncing application from $REPO_SRC"
  rsync -a --delete \
    --exclude data/ --exclude .venv/ --exclude '__pycache__/' \
    "$REPO_SRC"/ "$APP_DIR"/
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
  echo "ERROR: no application at $APP_DIR - push the code first." >&2
  echo "  from your laptop:  .\\deploy\\push-code.ps1 -RemoteHost <host> -KeyFile <key>" >&2
  echo "  (a bare 'scp -r .' would ship the Windows .venv and a torn copy of" >&2
  echo "   data/sniper.db - see docs/DEPLOY.md)" >&2
  exit 1
fi

log "Building virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Installing Litestream"
if ! command -v litestream >/dev/null 2>&1; then
  ARCH=$(dpkg --print-architecture)   # amd64 | arm64
  # Litestream renamed its release assets at 0.5.x: `litestream-v0.3.13-linux-
  # amd64.deb` became `litestream-0.5.16-linux-x86_64.deb`. dpkg says "amd64",
  # so match BOTH spellings or x86 silently finds nothing (it did, 2026-08-05).
  # arm64 is spelled the same either way.
  case "$ARCH" in
    amd64) LS_ARCH='(amd64|x86_64)' ;;
    arm64) LS_ARCH='arm64' ;;
    *)     LS_ARCH="$ARCH" ;;
  esac
  TMP=$(mktemp -d)
  LS_URL=$(curl -fsSL --max-time 30 https://api.github.com/repos/benbjohnson/litestream/releases/latest \
    | grep -oE "https://[^\"]*/releases/download/[^\"]*linux-${LS_ARCH}\.deb" | head -1)
  if [[ -z "$LS_URL" ]]; then
    echo "WARNING: could not resolve a Litestream .deb for ${ARCH}; install manually." >&2
  else
    curl -fsSL --max-time 120 --retry 3 "$LS_URL" -o "$TMP/litestream.deb"
    dpkg -i "$TMP/litestream.deb"
    rm -rf "$TMP"
    litestream version
  fi
fi
# NOTE: do NOT `systemctl disable --now litestream` here to suppress the unit
# shipped in the .deb. Ours has the same name, and a unit in
# /etc/systemd/system already takes precedence over /lib/systemd/system - so
# that call did nothing useful and instead STOPPED AND DISABLED replication on
# every redeploy, silently, after the script printed success (observed
# 2026-08-05). Replication is restarted at the end of this script instead.

log "Installing config templates"
[[ -f "$ETC_DIR/litestream.yml" ]] || cp "$APP_DIR/deploy/litestream.yml" "$ETC_DIR/litestream.yml"
if [[ ! -f "$ETC_DIR/litestream.env" ]]; then
  cat > "$ETC_DIR/litestream.env" <<'EOF'
# NON-AWS bucket credentials (Backblaze B2 or Cloudflare R2).
# See docs/DEPLOY.md for why this must not be same-account S3.
LITESTREAM_ACCESS_KEY_ID=CHANGE_ME
LITESTREAM_SECRET_ACCESS_KEY=CHANGE_ME
EOF
fi
if [[ ! -f "$ETC_DIR/env" ]]; then
  cat > "$ETC_DIR/env" <<'EOF'
# Optional. The Tier 0 recorder needs none of these.
# HELIUS_API_KEY=
# RUGCHECK_API_KEY=
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
EOF
fi
# Credentials: root-only. Config: readable by the service user (it holds only
# a bucket name and endpoint, no secrets).
chown root:root "$ETC_DIR"/litestream.env "$ETC_DIR"/env
chmod 600 "$ETC_DIR"/litestream.env "$ETC_DIR"/env
chown root:"$APP_USER" "$ETC_DIR"/litestream.yml
chmod 640 "$ETC_DIR"/litestream.yml

log "Installing sniper CLI wrapper"
# Config.load() resolves a bare "config.yaml" against the CWD. The service is
# fine (WorkingDirectory=/opt/meme-sniper), but an operator running `stats` from
# ~ubuntu got PermissionError on /home/ubuntu/config.yaml. Pin the path.
cat > /usr/local/bin/sniper <<'EOF'
#!/bin/sh
exec sudo -u sniper /opt/meme-sniper/.venv/bin/python -m sniper.main \
  --config /opt/meme-sniper/config.yaml "$@"
EOF
chmod 755 /usr/local/bin/sniper

log "Installing systemd units"
cp "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable meme-sniper.service
systemctl enable meme-sniper-enrich.service

log "Starting recorder"
systemctl restart meme-sniper.service
sleep 5
systemctl --no-pager --lines=15 status meme-sniper.service || true

# Tier 1 enrichment is time-critical: metadata hosts 404 within days, so a
# launch whose document was not fetched close to launch is not "pending", it is
# lost. Restarted on every deploy alongside the recorder.
log "Starting Tier 1 enrichment"
systemctl restart meme-sniper-enrich.service
sleep 3
systemctl is-active meme-sniper-enrich.service || true

# Bring replication back up if it has already been configured. A code push must
# never be the reason backups stopped, and "still says success while the replica
# goes stale" is the worst possible failure mode for irreplaceable data.
if [[ -f "$ETC_DIR/litestream.yml" ]] && ! grep -q 'CHANGE-ME' "$ETC_DIR/litestream.yml"; then
  log "Restarting replication"
  systemctl enable litestream.service >/dev/null 2>&1 || true
  systemctl restart litestream.service
  sleep 3
  systemctl is-active litestream.service || true
fi

cat <<EOF

------------------------------------------------------------------
Recorder is running. Remaining manual steps:

  1. Edit $ETC_DIR/litestream.yml   -> set your NON-AWS bucket + endpoint
  2. Edit $ETC_DIR/litestream.env   -> set bucket credentials
  3. sudo systemctl enable --now litestream

Then verify (see docs/DEPLOY.md):

  journalctl -u meme-sniper -f
  sniper stats

IMPORTANT: run the throttling check after ~1 hour. If the launch rate is
far below the ~1500-2700/h measured from a residential connection, this
datacenter IP is being rate-limited and the host needs rethinking.
------------------------------------------------------------------
EOF
