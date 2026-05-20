#!/usr/bin/env bash
# Install / refresh mcpserver on latex-tools.
# Usage:  sudo bash deploy/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET=/opt/mcpserver

mkdir -p "$TARGET"
rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '.env' \
    "$REPO_DIR/" "$TARGET/"

cd "$TARGET"

if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "!! .env created from .env.example — edit secrets then re-run." >&2
fi

install -m 644 deploy/mcpserver.service /etc/systemd/system/mcpserver.service
systemctl daemon-reload
systemctl enable mcpserver
systemctl restart mcpserver
sleep 1
systemctl --no-pager status mcpserver | head -20
