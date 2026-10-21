#!/usr/bin/env bash
# macOS（以及 Linux）入口：真正的逻辑在 build-app.py，两端共用一份。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3（macOS 可通过 xcode-select --install 安装）" >&2
  exit 1
fi

exec python3 "$HERE/build-app.py" "$@"
