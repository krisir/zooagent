#!/usr/bin/env bash
# Browser-only development: Vite dev server + backend (started by Vite).
cd "$(dirname "${BASH_SOURCE[0]}")/../../apps/desktop"
exec pnpm dev "$@"
