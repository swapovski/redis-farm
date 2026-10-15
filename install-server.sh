#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ "${EUID}" -ne 0 ]; then
    echo "Run with sudo: sudo bash install-server.sh" >&2
    exit 1
fi
exec python3 deploy/ubuntu/install.py "$@"
