#!/usr/bin/env bash
# 只构建 sidecar（前端 + PyInstaller），保留旧的两步式打包流程：
#   scripts/build/build-sidecar.sh
#   cd apps/desktop && pnpm tauri build
# 一步出安装包请直接用 scripts/build/build-app.sh。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/build-app.py" --sidecar-only "$@"
