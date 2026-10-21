"""Populate a workspace with a small demo app, through the public HTTP API.

Useful for demos and for手动验收：它完全通过对外接口工作（对话 → 工具 → 引擎），
所以既能给开发工作空间灌数据，也能验证一个真实运行的实例。

    uv run python ../scripts/dev/seed_demo.py --base http://127.0.0.1:7801 --token dev-token
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.error
import urllib.request
from typing import Any

FINANCE_PROMPT = "我想记录家里的收入和支出，按分类统计"

EXPENSES = [
    "餐饮 今天午餐花了 38 元",
    "餐饮 晚上超市买菜花了 126 元",
    "交通 打车花了 25 元",
    "购物 买了一件外套花了 399 元",
]
INCOME = ["工资 这个月工资收入 12000 元"]


class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, payload: Any | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        return urllib.request.urlopen(request, timeout=300)

    def get(self, path: str) -> Any:
        return json.load(self._request("GET", path))

    def post(self, path: str, payload: Any | None = None) -> Any:
        return json.load(self._request("POST", path, payload if payload is not None else {}))

    def chat(self, application_id: str | None, message: str) -> dict[str, Any]:
        """Send a chat message and return the final `done` event."""
        path = (
            f"/api/applications/{application_id}/chat"
            if application_id
            else "/api/chat/primary"
        )
        response = self._request("POST", path, {"message": message})
        done: dict[str, Any] = {}
        last_message = ""
        for raw in response:
            line = raw.decode().strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") == "assistant_message":
                last_message = event.get("content") or ""
            elif event.get("type") == "error":
                last_message = f"[error] {event.get('message')}"
            elif event.get("type") == "done":
                done = event
        done["content"] = last_message
        return done


def main() -> int:
    parser = argparse.ArgumentParser(description="seed a zooagent demo workspace")
    parser.add_argument("--base", default="http://127.0.0.1:7801")
    parser.add_argument("--token", default="dev-token")
    parser.add_argument(
        "--workspace",
        default=None,
        help="工作空间目录（会从这里读取 runtime/session.json 的端口与 token）",
    )
    parser.add_argument("--skip-if-exists", action="store_true", default=True)
    args = parser.parse_args()

    base, token = args.base, args.token
    if args.workspace:
        session = pathlib.Path(args.workspace).expanduser() / "runtime" / "session.json"
        if not session.is_file():
            print(f"找不到会话文件: {session}（应用是否正在运行？）")
            return 1
        data = json.loads(session.read_text())
        token = data["token"]
        base = data.get("base_url") or base
    print(f"目标实例: {base}")

    client = Client(base, token)

    existing = client.get("/api/applications")["applications"]
    if existing and args.skip_if_exists:
        print(f"工作空间已有 {len(existing)} 个应用，跳过创建。")
        application_id = existing[0]["id"]
    else:
        print(f"主助手: {FINANCE_PROMPT}")
        result = client.chat(None, FINANCE_PROMPT)
        print(f"  -> {result.get('content', '').splitlines()[0] if result.get('content') else ''}")
        applications = client.get("/api/applications")["applications"]
        if not applications:
            print("创建应用失败。")
            return 1
        application_id = applications[0]["id"]

    print(f"应用: {application_id}")

    for message in [*EXPENSES, *INCOME]:
        result = client.chat(application_id, message)
        print(f"应用助手: {message} -> {result.get('content', '').strip()}")

    overview = client.get(f"/api/applications/{application_id}")
    transaction = next(
        (model for model in overview["models"] if model["key"] == "transaction"), None
    )
    if transaction is None:
        print("没有找到 transaction 模型。")
        return 0

    summary = client.post(
        f"/api/applications/{application_id}/models/transaction/aggregate",
        {"aggregations": [{"function": "sum", "field": "amount", "alias": "total"}]},
    )
    exported = client.post(
        f"/api/applications/{application_id}/models/transaction/query", {"limit": 1}
    )
    print(f"交易总数: {exported['total']}，合计: {summary['items'][0]['total']}")
    print("完成。打开 http://localhost:1420 查看界面。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
