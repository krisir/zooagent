"""PyInstaller entry point for the bundled backend sidecar."""

from zooagent.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
