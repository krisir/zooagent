"""CLI entry point.

``zooagent serve`` starts the local API on 127.0.0.1. The desktop shell spawns
this process, reads the single ``{"event": "ready", ...}`` line from stdout to
learn the port and session token, then loads the web UI from that origin.

When ``ZOOAGENT_PARENT_PID`` is set (the desktop shell sets it), a watchdog
exits the process as soon as the shell disappears. This matters because the
packaged sidecar is a PyInstaller onefile binary whose *child* process is a
different pid than the one the shell spawned; without the watchdog that child
would survive as an orphan holding the port.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

from . import __version__
from .main import create_app
from .state import AppState

PARENT_CHECK_INTERVAL = 2.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind and listen *before* announcing readiness.

    The desktop shell loads the URL as soon as it sees the ready handshake, so
    the handshake must mean "the port accepts connections", not just "the
    process started". Binding here (and letting uvicorn adopt the socket) makes
    that guarantee true even when importing heavy dependencies takes seconds.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def _parent_alive(pid: int) -> bool:
    if os.name == "nt":  # pragma: no cover - windows only
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError:  # pragma: no cover - platform quirk
        return True
    return True


def _windows_process_alive(pid: int) -> bool:  # pragma: no cover - windows only
    """Windows 上 ``os.kill(pid, 0)`` 会真的结束那个进程，只能问句柄。

    ``OpenProcess(SYNCHRONIZE)`` + ``WaitForSingleObject(0)``：等到对象就是已退出，
    超时说明还活着。打不开句柄（进程已经没了）也按退出处理。
    """
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def start_parent_watchdog(pid: int) -> threading.Thread | None:
    """Exit when the desktop shell that launched us goes away (any platform).

    Diagnostics go to stderr: stdout is reserved for the one-line ready
    handshake the shell parses. The exit must never depend on I/O success -
    by the time we detect the parent is gone, our stdout pipe is closed, and a
    failure to log would otherwise keep the orphan alive.
    """
    _log_stderr({"event": "watchdog-started", "parent_pid": pid})

    def _watch() -> None:
        while True:
            time.sleep(PARENT_CHECK_INTERVAL)
            if not _parent_alive(pid):
                _log_stderr({"event": "parent-exited", "parent_pid": pid})
                os._exit(0)

    thread = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    thread.start()
    return thread


def _log_stderr(payload: dict[str, object]) -> None:
    try:
        print(json.dumps(payload), file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr may already be closed
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zooagent", description="zooagent backend")
    parser.add_argument("--version", action="version", version=f"zooagent {__version__}")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="启动本地服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0, help="0 表示自动选择空闲端口")
    serve.add_argument("--workspace", default=None, help="工作空间目录")
    serve.add_argument("--token", default=None, help="会话令牌（默认自动生成）")
    serve.add_argument("--log-level", default="info")
    info = sub.add_parser("info", help="打印工作空间信息")
    info.add_argument("--workspace", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "info":
        state = AppState.create(getattr(args, "workspace", None))
        print(
            json.dumps(
                {
                    "version": __version__,
                    "workspace": str(state.paths.root),
                    "system_db": str(state.paths.system_db),
                    "session_file": str(state.paths.session_file),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    import uvicorn

    # Bind first so the ready handshake below is truthful about listening.
    sock = bind_socket(args.host, args.port or 0)
    port = int(sock.getsockname()[1])
    state = AppState.create(args.workspace, args.token)
    app = create_app(state)
    # Record the real port so tools (and the user) can find this instance.
    state.write_session(port=port)

    parent_pid = os.environ.get("ZOOAGENT_PARENT_PID")
    if parent_pid and parent_pid.isdigit():
        start_parent_watchdog(int(parent_pid))

    print(
        json.dumps(
            {
                "event": "ready",
                "version": __version__,
                "port": port,
                "host": args.host,
                "token": state.token,
                "workspace": str(state.paths.root),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    # Importing heavy optional dependencies (AgentScope) happens lazily on the
    # first request, so at this point the socket simply accepts connections.
    server = uvicorn.Server(
        uvicorn.Config(app, log_level=args.log_level, access_log=False)
    )
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
