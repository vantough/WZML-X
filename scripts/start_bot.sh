#!/usr/bin/env bash
set -euo pipefail
cd /usr/src/app
if ! python scripts/provision_prowlarr.py; then
  echo 'WARNING: Prowlarr auto-setup did not complete; starting WZML with saved settings.' >&2
fi
exec "$@"
