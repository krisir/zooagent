#!/usr/bin/env bash
# Start the local backend only (handy when you run Vite/Tauri separately).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${ZOOAGENT_DEV_PORT:-7801}"
TOKEN="${ZOOAGENT_DEV_TOKEN:-dev-token}"
WORKSPACE="${ZOOAGENT_HOME:-$REPO_ROOT/.workspace}"

cd "$REPO_ROOT/backend"
exec uv run zooagent serve --host 127.0.0.1 --port "$PORT" --token "$TOKEN" --workspace "$WORKSPACE"
