#!/usr/bin/env bash
# One-shot setup + run for a fresh Ubuntu box (e.g. a Killercoda playground).
# Usage:
#   export MOCKSERVER_ADMIN_PASSWORD=pick-something
#   bash setup.sh
set -euo pipefail

if [ -z "${MOCKSERVER_ADMIN_PASSWORD:-}" ]; then
    echo "ERROR: set MOCKSERVER_ADMIN_PASSWORD first — this server will be publicly reachable."
    echo "  export MOCKSERVER_ADMIN_PASSWORD=pick-something"
    exit 1
fi

PORT="${PORT:-4500}"

# sudo is absent when already root (Killercoda's default shell is root).
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

$SUDO apt-get update -y
$SUDO apt-get install -y python3-venv python3-pip

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo
echo "Starting mock server on 0.0.0.0:${PORT}"
echo "  Admin UI : /admin (user 'admin', password = MOCKSERVER_ADMIN_PASSWORD)"
echo "  Sample   : GET/POST /api/1.0.0/items"
echo

# One worker on purpose: the request log and flake counters are in-memory,
# so multiple workers would each keep their own copy.
export MOCKSERVER_DEBUG=0
exec ./venv/bin/gunicorn --bind "0.0.0.0:${PORT}" --workers 1 app:app
