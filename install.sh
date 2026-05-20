#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/ont-tr069"
APP_USER="onttr069"
APP_PORT="8080"
REPO_URL=""
WEB_TOKEN=""

usage() {
  echo "Uso: sudo bash install.sh --repo https://github.com/USUARIO/REPO.git [--port 8080] [--token TOKEN]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_URL="${2:-}"
      shift 2
      ;;
    --dir)
      APP_DIR="${2:-}"
      shift 2
      ;;
    --user)
      APP_USER="${2:-}"
      shift 2
      ;;
    --port)
      APP_PORT="${2:-}"
      shift 2
      ;;
    --token)
      WEB_TOKEN="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Argumento desconhecido: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Execute como root: sudo bash install.sh ..."
  exit 1
fi

if [[ -z "$REPO_URL" ]]; then
  echo "Informe --repo com a URL do repositorio GitHub."
  usage
  exit 1
fi

if [[ -z "$WEB_TOKEN" ]]; then
  WEB_TOKEN="$(openssl rand -hex 24 2>/dev/null || date +%s%N)"
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y git python3 python3-venv python3-pip curl ca-certificates

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only
else
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m playwright install-deps chromium
mkdir -p "$APP_DIR/ms-playwright"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/ms-playwright"
sudo -u "$APP_USER" env PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/ms-playwright" "$APP_DIR/.venv/bin/python" -m playwright install chromium

if [[ ! -f "$APP_DIR/config.json" ]]; then
  cp "$APP_DIR/config.server.example.json" "$APP_DIR/config.json"
  chown "$APP_USER":"$APP_USER" "$APP_DIR/config.json"
  chmod 600 "$APP_DIR/config.json"
fi

cat >/etc/systemd/system/ont-tr069-web.service <<SERVICE
[Unit]
Description=Painel TR-069 ONT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=ONT_WEB_TOKEN=$WEB_TOKEN
Environment=PLAYWRIGHT_BROWSERS_PATH=$APP_DIR/ms-playwright
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/web_server.py --host 0.0.0.0 --port $APP_PORT
Restart=always
RestartSec=5
User=$APP_USER

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now ont-tr069-web.service

echo
echo "Instalacao concluida."
echo "Painel: http://IP_DO_SERVIDOR:$APP_PORT"
echo "Token do painel: $WEB_TOKEN"
echo
echo "Comandos uteis:"
echo "  systemctl status ont-tr069-web.service"
echo "  journalctl -u ont-tr069-web.service -f"
