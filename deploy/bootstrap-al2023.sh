#!/bin/bash
# One-time setup for a fresh Amazon Linux 2023 instance (run as ec2-user):
#   curl -fsSL https://raw.githubusercontent.com/dkiran/Appscrapper/main/deploy/bootstrap-al2023.sh | bash
# (If the repo is private, clone it first and run:  bash ~/Appscrapper/deploy/bootstrap-al2023.sh)
set -euo pipefail
REPO="${REPO:-https://github.com/dkiran/Appscrapper.git}"

echo "== packages"
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo "== docker compose and buildx plugins"
sudo mkdir -p /usr/local/lib/docker/cli-plugins
[ -x /usr/local/lib/docker/cli-plugins/docker-compose ] || {
    sudo curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; }
[ -x /usr/local/lib/docker/cli-plugins/docker-buildx ] || {
    sudo curl -fsSL https://github.com/docker/buildx/releases/download/v0.19.3/buildx-v0.19.3.linux-amd64 \
        -o /usr/local/lib/docker/cli-plugins/docker-buildx
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx; }

echo "== 2 GB swap (small instances)"
if ! swapon --show | grep -q /swapfile; then
    sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== code"
cd "$HOME"
[ -d Appscrapper ] || git clone "$REPO" Appscrapper
cd Appscrapper
[ -f .env ] || cp .env.example .env
chmod +x deploy/*.sh

cat <<'MSG'

Done. Next:
  1. Log out and back in (so the docker group applies):  exit
  2. nano ~/Appscrapper/.env        (set POSTGRES_PASSWORD, DASHBOARD_PASSWORD, DOMAIN)
  3. nano ~/Appscrapper/config.json (countries, categories, limits)
  4. Follow the migration / start steps in the chat or README.
MSG
