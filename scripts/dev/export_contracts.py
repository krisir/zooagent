"""Export the platform's data contracts as JSON Schema.

These files are the machine-readable contract between the AI (which generates
definitions), the engines (which validate and execute them) and the desktop
renderer. Regenerate with:

    uv run python ../scripts/dev/export_contracts.py

CI 用 `--check` 校验这些文件没有跟随代码偏离（不写盘，只比对）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from zooagent.actions.schema import ActionDef  # noqa: E402
from zooagent.data.query import AggregateSpec, QuerySpec  # noqa: E402
from zooagent.models.schema import FieldDef, ModelDef  # noqa: E402
from zooagent.ui_schema.schema import UIComponent, UIPage, UISchema  # noqa: E402

CONTRACTS = {
    "model": ModelDef,
    "field": FieldDef,
    "action": ActionDef,
    "ui": UISchema,
    "ui-page": UIPage,
    "ui-component": UIComponent,
    "query": QuerySpec,
    "aggregate": AggregateSpec,
}


def render(name: str, model: type) -> str:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = f"zooagent {name}"
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    out_dir = REPO_ROOT / "contracts"
    if not check:
        out_dir.mkdir(parents=True, exist_ok=True)

    stale: list[Path] = []
    for name, model in CONTRACTS.items():
        path = out_dir / f"{name}.schema.json"
        content = render(name, model)
        if check:
            if not path.is_file() or path.read_text("utf-8") != content:
                stale.append(path)
            continue
        path.write_text(content, "utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")

    if not check:
        return 0
    if stale:
        print("这些契约文件与代码不一致，重新生成：", file=sys.stderr)
        for path in stale:
            print(f"  - {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        print("  cd backend && uv run python ../scripts/dev/export_contracts.py", file=sys.stderr)
        return 1
    print(f"契约文件都是最新的（{len(CONTRACTS)} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
