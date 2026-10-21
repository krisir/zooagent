"""Headless UI smoke capture: drives the real app and screenshots it.

This is how the UI is verified without a human looking at the screen: launch
Chrome headless with the DevTools protocol, click through the workspace, and
report any JS exception or console error along the way.

    uv run python ../scripts/dev/capture_ui.py --url http://localhost:1420 --out /tmp/ui
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import shutil
import subprocess
import time
import urllib.request
from typing import Any

import websockets

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

NOISE = (
    "Download the React DevTools",
    "antd: compatible",
    "[antd:",
)


class DevTools:
    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.counter = 0
        self.events: list[dict[str, Any]] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counter += 1
        identifier = self.counter
        await self.socket.send(json.dumps({"id": identifier, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self.socket.recv())
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})
            self.events.append(message)

    async def drain(self) -> None:
        """Collect events that arrived since the last call."""
        await self.send("Runtime.evaluate", {"expression": "1", "returnByValue": True})

    async def evaluate(self, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")

    async def screenshot(self, path: pathlib.Path) -> None:
        result = await self.send("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(result["data"]))

    def problems(self) -> list[str]:
        found: list[str] = []
        for event in self.events:
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails") or {}
                found.append(f"异常: {details.get('text')} {details.get('exception', {}).get('description', '')}")
            elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                text = " ".join(
                    str(arg.get("value", arg.get("description", "")))
                    for arg in params.get("args", [])
                )
                if not any(noise in text for noise in NOISE):
                    found.append(f"console.error: {text[:200]}")
            elif method == "Log.entryAdded":
                entry = params.get("entry") or {}
                if entry.get("level") == "error":
                    found.append(f"log: {entry.get('text', '')[:200]}")
        return found


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if pathlib.Path(candidate).is_file():
            return candidate
    raise SystemExit("找不到 Chrome/Chromium，无法截图")


def wait_for_target(port: int, timeout: float = 25.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.load(response)
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.4)
    raise SystemExit("Chrome 调试端口未就绪")


async def capture(
    url: str,
    out_dir: pathlib.Path,
    port: int,
    wait: float,
    token: str | None,
    click_text: str | None = None,
    hero_prompt: str | None = None,
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    profile = out_dir / "chrome-profile"
    shutil.rmtree(profile, ignore_errors=True)

    process = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=1440,940",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,OptimizationHints",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        socket_url = wait_for_target(port)
        async with websockets.connect(socket_url, max_size=80 * 1024 * 1024) as socket:
            devtools = DevTools(socket)
            await devtools.send("Page.enable")
            await devtools.send("Runtime.enable")
            await devtools.send("Log.enable")
            if token:
                # The packaged app receives its token from the shell's init
                # script; inject the same thing so a browser can be driven the
                # way the real webview is.
                await devtools.send(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {
                        "source": (
                            "window.__ZOOAGENT_CONFIG__ = "
                            f'{{ token: {json.dumps(token)}, baseUrl: "" }};'
                        )
                    },
                )
                await devtools.send("Page.reload", {"ignoreCache": True})
            await devtools.drain()

            await asyncio.sleep(wait)
            await devtools.screenshot(out_dir / "01-workspace.png")
            title = await devtools.evaluate("document.querySelector('#root')?.innerText.slice(0, 80)")
            print(f"工作台首屏文本: {title!r}")

            # the settings modal (the rail has no workspace switcher / view tabs)
            settings_open = await devtools.evaluate(
                "(function(){const b=[...document.querySelectorAll('.nav')]"
                ".find(x => x.textContent.includes('偏好设置'));"
                "if(!b) return false; b.click(); return true;})()"
            )
            if settings_open:
                await asyncio.sleep(wait * 0.5)
                await devtools.screenshot(out_dir / "01e-settings.png")
                await devtools.evaluate("document.querySelector('.ant-modal-close')?.click()")
                await asyncio.sleep(0.3)
            # open an assistant straight from the rail list
            if click_text:
                await devtools.evaluate(
                    "[...document.querySelectorAll('.nav.app')]"
                    f".find(item => item.textContent.includes({json.dumps(click_text)}))?.click()"
                )
            else:
                await devtools.evaluate("document.querySelector('.nav.app')?.click()")
            await asyncio.sleep(wait * 0.7)
            # the assistant is always there; this icon only shows/hides the data
            await devtools.screenshot(out_dir / "02-assistant-split.png")

            async def toggle_data(filename: str) -> bool:
                clicked = await devtools.evaluate(
                    "(function(){const b=document.querySelector('.chat-header-actions button');"
                    "if(!b) return false; b.click(); return true;})()"
                )
                if not clicked:
                    return False
                await asyncio.sleep(wait * 0.5)
                await devtools.screenshot(out_dir / filename)
                return True

            await toggle_data("02b-assistant-chat-only.png")
            await toggle_data("02c-assistant-split-again.png")

            for index, tab in enumerate(("交易记录", "账户", "物品台账", "借还记录", "总览", "分类与位置")):
                clicked = await devtools.evaluate(
                    "(function(){const t=[...document.querySelectorAll('.ant-tabs-tab')]"
                    f".find(x => x.textContent.includes({json.dumps(tab)}));"
                    "if(!t) return false; t.click(); return true;})()"
                )
                if not clicked:
                    continue
                await asyncio.sleep(wait * 0.5)
                safe = tab.encode("ascii", "ignore").decode() or f"tab{index}"
                await devtools.screenshot(out_dir / f"03-tab-{index}-{safe}.png")

            await devtools.drain()

            if hero_prompt:
                await devtools.evaluate(
                    "(function(){const b=[...document.querySelectorAll('.nav.primary')]"
                    ".find(x => x.textContent.includes('主助手'));"
                    "if(b) b.click(); return true;})()"
                )
                await asyncio.sleep(0.5)
                typed = await devtools.evaluate(
                    "(function(){const t=document.querySelector('.assistant-dock textarea');"
                    "if(!t) return false;"
                    "const setter=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;"
                    f"setter.call(t, {json.dumps(hero_prompt)});"
                    "t.dispatchEvent(new Event('input', {bubbles:true}));"
                    "return true;})()"
                )
                if typed:
                    await asyncio.sleep(0.2)
                    await devtools.evaluate(
                        "document.querySelector('.assistant-dock .chat-input .ant-btn')?.click()"
                    )
                    await asyncio.sleep(wait * 1.4)
                    await devtools.screenshot(out_dir / "05-hero-conversation.png")

            return devtools.problems()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="screenshot the zooagent UI headlessly")
    parser.add_argument("--url", default="http://localhost:1420")
    parser.add_argument("--out", default="/tmp/zooagent-ui")
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--wait", type=float, default=6.0)
    parser.add_argument(
        "--token",
        default=None,
        help="会话 token（验证打包后的应用时需要，它靠壳注入而不是 URL）",
    )
    parser.add_argument(
        "--click-text",
        default=None,
        help="打开标题包含该文字的应用卡片（默认打开第一个）",
    )
    parser.add_argument(
        "--hero-prompt",
        default=None,
        help="在首页主助手输入框发送一句话并截图对话态（验证用，会产生一条对话记录）",
    )
    args = parser.parse_args()

    problems = asyncio.run(
        capture(
            args.url,
            pathlib.Path(args.out),
            args.port,
            args.wait,
            args.token,
            args.click_text,
            args.hero_prompt,
        )
    )
    if problems:
        print("发现前端问题:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"没有 JS 异常或控制台错误。截图: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
