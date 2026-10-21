#!/usr/bin/env bash
# Development entry point: clears the dev workspace, starts the backend and the
# desktop shell. Use scripts/dev/dev.sh for the browser-only flow.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if ! command -v cargo >/dev/null 2>&1 && [[ -x "$HOME/.cargo/bin/cargo" ]]; then
  export PATH="$HOME/.cargo/bin:$PATH"
fi

cd "$REPO_ROOT/apps/desktop"
exec pnpm tauri dev "$@"
