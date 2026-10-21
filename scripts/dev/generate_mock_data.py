"""Fill applications with mock data, straight from the workspace files.

This is the fastest way to test the UI with realistic volume: hundreds of
records, several categories, months of dates. It talks to the engines directly
(no running app, no HTTP), so it can run while the desktop app is open - SQLite
WAL handles the concurrent access - and it never touches ``session.json``.

    uv run python ../scripts/dev/generate_mock_data.py --workspace ~/Library/Application\\ Support/zooagent --count 200
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from zooagent.actions.engine import ActionEngine  # noqa: E402
from zooagent.agents.mock_data import generate_application_data  # noqa: E402
from zooagent.application.service import ApplicationService  # noqa: E402
from zooagent.config import WorkspacePaths  # noqa: E402
from zooagent.data.engine import DataEngine  # noqa: E402
from zooagent.errors import ZooAgentError  # noqa: E402
from zooagent.models.engine import ModelEngine  # noqa: E402
from zooagent.persistence.sqlite import init_system_db  # noqa: E402
from zooagent.ui_schema.service import UISchemaService  # noqa: E402


def build_service(workspace: Path) -> ApplicationService:
    paths = WorkspacePaths.resolve(workspace)
    init_system_db(paths.system_db)
    models = ModelEngine()
    data = DataEngine(models)
    actions = ActionEngine(models, data)
    ui = UISchemaService(models, actions)
    return ApplicationService(paths, models, data, actions, ui)


def main() -> int:
    parser = argparse.ArgumentParser(description="为应用生成测试数据（UI 测试用）")
    parser.add_argument("--workspace", default=None, help="工作空间目录；默认使用平台默认路径")
    parser.add_argument("--count", type=int, default=200, help="主模型生成条数")
    parser.add_argument("--seed", type=int, default=7, help="随机种子（可复现）")
    parser.add_argument("--app", default=None, help="只处理某个应用（id 或名称片段）")
    args = parser.parse_args()

    service = build_service(Path(args.workspace).expanduser() if args.workspace else None)
    applications = service.list_applications()
    if args.app:
        applications = [
            app
            for app in applications
            if app.id == args.app or args.app.lower() in app.name.lower()
        ]
    if not applications:
        print("没有找到可处理的应用。")
        return 1

    total = 0
    for application in applications:
        try:
            with service.open_app(application.id) as ctx:
                report = generate_application_data(
                    ctx.conn,
                    ctx.models,
                    ctx.data,
                    count=args.count,
                    seed=args.seed,
                )
        except ZooAgentError as exc:
            print(f"✗ {application.name}: {exc.message}")
            continue

        per_model = "，".join(
            f"{item['model']} +{item['created']}" for item in report["models"]
        )
        print(f"✓ {application.name}: 新增 {report['created']} 条（{per_model}）")
        if report["failed"]:
            print(f"  ! 失败 {report['failed']} 条，示例：{report['models'][0]['errors'][:1]}")
        total += report["created"]

    print(f"\n共生成 {total} 条测试数据。打开应用即可看到表格、图表与分页效果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
