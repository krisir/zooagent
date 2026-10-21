"""CLI: run the generation-quality eval set.

    uv run python -m zooagent.eval                        # CI-safe subset (offline + mock)
    uv run python -m zooagent.eval --provider llm          # only real-model cases
    uv run python -m zooagent.eval --profile profile_xxx   # a specific configured model
    uv run python -m zooagent.eval --only inventory-basic --keep
    uv run python -m zooagent.eval --json eval/results/latest.json --compare eval/results/baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..errors import ZooAgentError
from .cases import default_cases_path, load_cases
from .runner import compare_reports, run_cases


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zooagent.eval", description="生成质量评测")
    parser.add_argument("--cases", default=None, help="用例文件（默认 backend/eval/cases.toml）")
    parser.add_argument(
        "--provider",
        action="append",
        choices=["offline", "mock", "llm"],
        help="只跑某一类 provider（可重复）",
    )
    parser.add_argument("--profile", default=None, help="指定模型配置 id 或名称片段")
    parser.add_argument(
        "--workspace",
        default=None,
        help="从某个工作空间读取已配置的模型（跑 llm 用例时用，例如你的应用工作目录）",
    )
    parser.add_argument("--only", action="append", help="只跑指定 case id（可重复）")
    parser.add_argument("--tag", action="append", help="只跑带指定 tag 的用例")
    parser.add_argument("--keep", action="store_true", help="保留每个用例的工作空间以便排查")
    parser.add_argument("--json", dest="json_out", default=None, help="把完整结果写成 JSON")
    parser.add_argument("--compare", default=None, help="与上次的 JSON 结果比较（指标变化）")
    parser.add_argument("--quiet", action="store_true", help="只输出结论行")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases_path = Path(args.cases) if args.cases else default_cases_path()

    try:
        cases = load_cases(cases_path)
    except ZooAgentError as exc:
        print(f"无法加载用例: {exc.message}", file=sys.stderr)
        return 2

    cases_to_run = [
        case for case in cases if not args.provider or (set(case.providers) & set(args.provider))
    ]
    if not cases_to_run:
        print("没有匹配的用例。", file=sys.stderr)
        return 2

    report = run_cases(
        cases_to_run,
        profile_override=args.profile,
        only=args.only,
        tags=args.tag,
        cases_path=str(cases_path),
        keep=args.keep,
        profiles_workspace=Path(args.workspace).expanduser() if args.workspace else None,
    )

    if not args.quiet:
        print(report.render())
    else:
        print(
            f"通过 {len(report.passed)} / 失败 {len(report.failed)} / 跳过 {len(report.skipped)}"
        )

    if args.compare:
        baseline_path = Path(args.compare)
        if baseline_path.is_file():
            changes = compare_reports(report, json.loads(baseline_path.read_text("utf-8")))
            if changes:
                print("\n与基线比较：")
                for line in changes:
                    print(f"  - {line}")
            else:
                print("\n与基线比较：无变化")
        else:
            print(f"\n（基线文件不存在，已跳过比较: {baseline_path}）")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), "utf-8"
        )
        print(f"\n完整结果已写入 {out}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
