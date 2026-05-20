#!/bin/bash
set -euo pipefail

umask 0077

echo "  ╔══════════════════════════════════════╗"
echo "  ║      Eumenes Discord Bot — Startup   ║"
echo "  ╚══════════════════════════════════════╝"

# Deploy Cloudflare keepalive worker before bot starts
if [ -n "${CLOUDFLARE_WORKERS_TOKEN:-}" ]; then
  echo "Deploying Cloudflare keepalive worker..."
  python3 /app/setup_keepalive.py || echo "Warning: keepalive deployment failed (non-fatal)"
else
  echo "CLOUDFLARE_WORKERS_TOKEN not set — keepalive skipped"
fi

echo "Starting bot..."
exec python3 /app/bot.py
