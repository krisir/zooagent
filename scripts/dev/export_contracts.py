"""Export the platform's data contracts as JSON Schema.

These files are the machine-readable contract between the AI (which generates
definitions), the engines (which validate and execute them) and the desktop
renderer. Regenerate with:

    uv run python ../scripts/dev/export_contracts.py
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


def main() -> int:
    out_dir = REPO_ROOT / "contracts"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, model in CONTRACTS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["title"] = f"zooagent {name}"
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
