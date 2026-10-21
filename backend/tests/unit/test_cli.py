"""CLI helpers that the desktop shell depends on."""

from __future__ import annotations

import os

from zooagent.__main__ import _parent_alive, build_parser, free_port


def test_free_port_is_usable() -> None:
    port = free_port()
    assert 1024 < port < 65536


def test_parent_alive_detection() -> None:
    assert _parent_alive(os.getpid()) is True
    # pid 0x7FFFFFFF is not a valid process id on macOS/Linux
    assert _parent_alive(2_147_483_647) is False


def test_cli_defaults_to_serve() -> None:
    args = build_parser().parse_args([])
    assert (args.command or "serve") == "serve"
    serve = build_parser().parse_args(["serve", "--port", "1234"])
    assert serve.port == 1234
