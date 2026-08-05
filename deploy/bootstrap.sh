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
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  sqlite3 rsync curl ca-certificates chrony unattended-upgrades

# Accurate clock matters: received_at is our only time reference on launch
# frames, because the stream sends no timestamp field.
log "Enabling time sync"
systemctl enable --now chrony >/dev/null 2>&1 || systemctl enable --now chronyd

log "Creating service user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"/data "$ETC_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
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
  TMP=$(mktemp -d)
  LS_URL=$(curl -fsSL https://api.github.com/repos/benbjohnson/litestream/releases/latest \
    | grep -o "https://[^\"]*linux-${ARCH}\.deb" | head -1)
  if [[ -z "$LS_URL" ]]; then
    echo "WARNING: could not resolve a Litestream .deb for ${ARCH}; install manually." >&2
  else
    curl -fsSL "$LS_URL" -o "$TMP/litestream.deb"
    dpkg -i "$TMP/litestream.deb"
    rm -rf "$TMP"
  fi
fi
# Litestream ships its own unit; ours supersedes it.
systemctl disable --now litestream >/dev/null 2>&1 || true

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
chmod 600 "$ETC_DIR"/litestream.env "$ETC_DIR"/env

log "Installing systemd units"
cp "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable meme-sniper.service

log "Starting recorder"
systemctl restart meme-sniper.service
sleep 5
systemctl --no-pager --lines=15 status meme-sniper.service || true

cat <<EOF

------------------------------------------------------------------
Recorder is running. Remaining manual steps:

  1. Edit $ETC_DIR/litestream.yml   -> set your NON-AWS bucket + endpoint
  2. Edit $ETC_DIR/litestream.env   -> set bucket credentials
  3. sudo systemctl enable --now litestream

Then verify (see docs/DEPLOY.md):

  journalctl -u meme-sniper -f
  sudo -u $APP_USER $APP_DIR/.venv/bin/python -m sniper.main stats

IMPORTANT: run the throttling check after ~1 hour. If the launch rate is
far below the ~1500-2700/h measured from a residential connection, this
datacenter IP is being rate-limited and the host needs rethinking.
------------------------------------------------------------------
EOF
