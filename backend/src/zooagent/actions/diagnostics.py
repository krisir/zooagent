"""Static, deterministic inspection of a failed Action.

The first responder to an Action failure is *not* a model: it is a plain
checker that compares what the definition says against what the models
actually are. That matters for two reasons:

* the default install runs offline, so a diagnosis must still be useful with
  no model configured;
* the most common failures are mechanical (a field key typo, a forward
  reference to a step output that has not been produced yet, a missing required
  value) and a model is unnecessary to spot them.

The result is a small, structured report: findings, a category, a one-line
summary, and - when the fix is unambiguous - a patched Action definition the
user can confirm. Nothing here writes anything.
"""

from __future__ import annotations

import ast
import difflib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..data.query import SYSTEM_COLUMNS
from ..models.schema import ModelDef
from .expression import FUNCTIONS, LITERALS
from .schema import ActionDef, ActionStep
from .validation import ALLOWED_EXPRESSION_NODES, ROOTS

CATEGORY_LABELS = {
    "unknown_field": "Action 定义引用了不存在的字段",
    "forward_reference": "Action 引用了还没产出的步骤结果",
    "bad_reference": "Action 里的变量/表达式解析不到",
    "missing_required": "创建记录时缺少必填字段",
    "invalid_input": "输入参数与 Action 的输入定义不匹配",
    "missing_record": "要操作的记录不存在（数据问题）",
    "revision_conflict": "并发修改冲突（数据问题）",
    "unique_conflict": "唯一约束冲突（数据问题）",
    "empty_result": "查询/聚合返回空结果，后续步骤拿不到数据",
    "runtime_error": "运行时错误",
}

#: Categories that point at the Action definition itself rather than at data.
DEFINITION_CATEGORIES = {
    "unknown_field",
    "forward_reference",
    "bad_reference",
    "missing_required",
    "invalid_input",
}


@dataclass
class Finding:
    level: str  # "problem" | "warning" | "ok"
    title: str
    detail: str = ""
    #: True when the inspection already fixed this finding in the proposal.
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "resolved": self.resolved,
        }


@dataclass
class Inspection:
    category: str
    summary: str
    findings: list[Finding] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    proposal: dict[str, Any] | None = None

    @property
    def is_definition_problem(self) -> bool:
        return self.category in DEFINITION_CATEGORIES

    def finalize(self) -> Inspection:
        """Recompute the summary after a model-supplied proposal was attached."""
        self.summary = _summary(self.category, self.findings, self.proposal)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "summary": self.summary,
            "definition_problem": self.is_definition_problem,
            "findings": [item.to_dict() for item in self.findings],
            "suggestions": self.suggestions,
            "proposal": self.proposal,
        }


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def inspect_failure(
    failure: dict[str, Any], models: Iterable[ModelDef]
) -> Inspection:
    """Compare a failure snapshot against the current model definitions."""
    action_payload = failure.get("action") or {}
    try:
        action = ActionDef.model_validate(action_payload)
    except ValidationError:
        action = None

    model_map = {model.key: model for model in models}
    findings: list[Finding] = []
    suggestions: list[str] = []
    patched = False

    if action is None:
        findings.append(
            Finding(
                "warning",
                "无法解析失败时的 Action 定义",
                "定义快照不完整，只能根据错误信息判断。",
            )
        )
    else:
        patched, more_findings, more_suggestions = _inspect_definition(
            action, model_map
        )
        findings.extend(more_findings)
        suggestions.extend(more_suggestions)

    findings.extend(_inspect_trace(failure))
    category = _classify(failure, findings)

    proposal: dict[str, Any] | None = None
    if patched and action is not None and not _unresolved(findings):
        try:
            proposal = ActionDef.model_validate(action.model_dump()).model_dump()
        except ValidationError:  # pragma: no cover - defensive
            proposal = None

    findings.insert(
        0,
        Finding(
            "problem",
            f"错误信息：{_error_message(failure)}",
            f"失败步骤：{_step_label(failure)}",
        ),
    )

    summary = _summary(category, findings, proposal)
    if not suggestions:
        suggestions.append(
            "如果这不是定义问题，请检查这次的输入数据和关联记录是否仍然存在。"
        )

    return Inspection(
        category=category,
        summary=summary,
        findings=findings,
        suggestions=suggestions,
        proposal=proposal,
    )


# ----------------------------------------------------------------------
# definition checks
# ----------------------------------------------------------------------
def _inspect_definition(
    action: ActionDef, model_map: dict[str, ModelDef]
) -> tuple[bool, list[Finding], list[str]]:
    findings: list[Finding] = []
    suggestions: list[str] = []
    patched = False
    produced: set[str] = set()
    input_keys = {item.key for item in action.inputs}

    for index, step in enumerate(action.steps):
        label = _label(step, index)
        model = model_map.get(step.model or "")

        if step.type in {"query", "aggregate", "create", "update", "delete"}:
            if not step.model:
                findings.append(Finding("problem", f"{label} 没有指定 model"))
            elif model is None:
                findings.append(
                    Finding(
                        "problem",
                        f"{label} 引用的模型 `{step.model}` 不存在",
                        f"现有模型：{', '.join(sorted(model_map)) or '无'}",
                    )
                )
                suggestions.append(
                    f"把 {label} 的 model 改成已有模型之一，或先创建 `{step.model}` 模型。"
                )

        if step.type in {"update", "delete"} and step.record_id is None:
            findings.append(
                Finding("problem", f"{label} 没有指定 record_id")
            )
            suggestions.append(
                f"给 {label} 补上 record_id（通常是前面查询步骤的输出，例如 "
                '{"ref": "steps.found.0.id"}）。'
            )

        if model is not None:
            unknown, known = _unknown_fields(step, model)
            for location, name in unknown:
                closest = _closest(name, known)
                detail = f"{label} 的{location}引用了 `{name}`"
                if closest:
                    detail += f"；模型里最接近的是 `{closest}`"
                findings.append(Finding("problem", detail))
                if closest:
                    suggestions.append(
                        f"把{location}里的 `{name}` 改成 `{closest}`。"
                    )
                    if _rename_field(step, name, closest):
                        findings[-1].resolved = True
                        patched = True
                else:
                    suggestions.append(
                        f"核对{location}里的 `{name}`：模型 `{model.key}` 没有这个字段"
                        f"（现有字段：{', '.join(known)}）。"
                    )

            if step.type in {"create", "update"}:
                missing = [
                    item.key
                    for item in model.fields
                    if item.required
                    and item.default is None
                    and item.key not in step.values
                ]
                if step.type == "create" and missing:
                    findings.append(
                        Finding(
                            "warning",
                            f"{label} 没有提供必填字段 {', '.join(missing)}",
                            "除非默认值补齐，否则这次创建一定失败。",
                        )
                    )
                    suggestions.append(
                        f"在 {label} 的 values 里补上 {', '.join(missing)}"
                        "（值可以来自 input 或前面步骤的输出）。"
                    )
                _check_enum_values(step, model, label, findings, suggestions)

        # References are resolved against what previous steps *actually*
        # produced. The define-time validator uses a global set, so a forward
        # reference passes validation and then fails on every run.
        forward, bad = _check_references(step, produced, input_keys)
        for name in forward:
            findings.append(
                Finding(
                    "problem",
                    f"{label} 引用了 `steps.{name}`，但这个名字在当前步骤之前还没有产出",
                    "步骤是按顺序执行的；只有前面的步骤才能被引用。",
                )
            )
            suggestions.append(
                f"调整步骤顺序，或把 {label} 的引用改成前面步骤的 output。"
            )
        for name in bad:
            findings.append(
                Finding(
                    "problem",
                    f"{label} 引用了 `{name}`，它既不是输入、也不是前面步骤的输出",
                )
            )
            suggestions.append(f"检查 {label} 的引用路径 `{name}`。")

        if step.output:
            produced.add(step.output)

    if action.output:
        _inspect_output(action, produced, findings, suggestions)

    return patched, findings, suggestions


def _unknown_fields(
    step: ActionStep, model: ModelDef
) -> tuple[list[tuple[str, str]], list[str]]:
    """Field keys the step reads/writes that the model does not declare."""
    known_fields = [item.key for item in model.fields]
    writable = set(known_fields)
    readable = set(known_fields) | SYSTEM_COLUMNS

    candidates: list[tuple[str, str]] = []
    for item in step.filters:
        candidates.append(("筛选条件", item.field))
    for item in step.sort:
        field_name = item.get("field")
        if field_name:
            candidates.append(("排序", str(field_name)))
    for name in step.group_by:
        candidates.append(("分组", name))
    for item in step.aggregations:
        field_name = item.get("field")
        if field_name:
            candidates.append(("聚合", str(field_name)))
    if step.type in {"create", "update"}:
        for key in step.values:
            candidates.append(("字段值", key))

    unknown = [
        (location, name)
        for location, name in candidates
        if name not in (writable if location == "字段值" else readable)
    ]
    return unknown, known_fields


def _check_enum_values(
    step: ActionStep,
    model: ModelDef,
    label: str,
    findings: list[Finding],
    suggestions: list[str],
) -> None:
    for key, value in step.values.items():
        if not isinstance(value, str):
            continue
        item = model.field(key)
        if item is None or item.type != "enum":
            continue
        allowed = {
            str(option.get("value")) for option in item.options or [] if option.get("value")
        }
        if allowed and value not in allowed:
            findings.append(
                Finding(
                    "warning",
                    f"{label} 给字段 `{key}` 写的值 `{value}` 不在枚举里",
                    f"可选值：{', '.join(sorted(allowed))}",
                )
            )
            suggestions.append(
                f"把 {label} 的 `{key}` 改成 {', '.join(sorted(allowed))} 之一。"
            )


def _check_references(
    step: ActionStep, produced: set[str], input_keys: set[str]
) -> tuple[list[str], list[str]]:
    available = produced | input_keys | set(ROOTS) | set(LITERALS)
    forward: list[str] = []
    bad: list[str] = []

    for path in _ref_paths(step):
        head = path.split(".", 1)[0]
        if head == "steps" and "." in path:
            name = path.split(".")[1]
            if name not in produced:
                forward.append(name)
        elif head == "input" and "." in path:
            name = path.split(".", 1)[1].split(".", 1)[0]
            if name not in input_keys:
                bad.append(path)
        elif head in ROOTS or head in produced:
            continue
        elif head in input_keys:
            continue
        else:
            bad.append(path)

    for name in _expression_names(step):
        if name in available or name in FUNCTIONS:
            continue
        if name in produced:
            continue
        bad.append(name)

    return _dedupe(forward), _dedupe([item for item in bad if item])


def _ref_paths(step: ActionStep) -> Iterator[str]:
    """Every ``{"ref": ...}`` path reachable from the step's parameters."""
    for value in (
        [item.value for item in step.filters]
        + list(step.values.values())
        + [step.record_id, step.expected_revision]
        + ([step.value] if step.value else [])
    ):
        yield from _walk_refs(value)


def _walk_refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if set(value.keys()) == {"ref"}:
            yield str(value["ref"])
        else:
            for item in value.values():
                yield from _walk_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_refs(item)


def _expression_names(step: ActionStep) -> list[str]:
    """Free variable names used by ``compute`` / ``{"expr": ...}`` strings."""
    sources: list[str] = []
    if step.type == "compute" and step.expression:
        sources.append(step.expression)
    sources.extend(_walk_expr(step.values))
    for item in step.filters:
        sources.extend(_walk_expr(item.value))

    names: list[str] = []
    for source in sources:
        text = re.sub(r"\$([A-Za-z_])", r"\1", source)
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ALLOWED_EXPRESSION_NODES):
                continue
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                names.append(node.func.id)
    return names


def _walk_expr(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if set(value.keys()) == {"expr"}:
            yield str(value["expr"])
        else:
            for item in value.values():
                yield from _walk_expr(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_expr(item)


def _inspect_output(
    action: ActionDef,
    produced: set[str],
    findings: list[Finding],
    suggestions: list[str],
) -> None:
    for path in _walk_refs(action.output):
        head = path.split(".", 1)[0]
        if head == "steps" and "." in path:
            name = path.split(".", 1)[1].split(".", 1)[0]
            if name not in produced:
                findings.append(
                    Finding(
                        "problem",
                        f"Action 的 output 引用了 `steps.{name}`，但没有步骤输出这个名字",
                    )
                )
                suggestions.append(
                    "把 output 改成某个步骤的 output 名"
                    f"（现有输出：{', '.join(sorted(produced)) or '无'}）。"
                )


def _rename_field(step: ActionStep, old: str, new: str) -> bool:
    """Apply the only unambiguous fix: rename a mistyped field key."""
    changed = False
    if old in step.values:
        step.values[new] = step.values.pop(old)
        changed = True
    for item in step.filters:
        if item.field == old:
            item.field = new
            changed = True
    for item in step.sort:
        if item.get("field") == old:
            item["field"] = new
            changed = True
    if old in step.group_by:
        step.group_by = [new if item == old else item for item in step.group_by]
        changed = True
    for item in step.aggregations:
        if item.get("field") == old:
            item["field"] = new
            changed = True
    return changed


def _closest(name: str, known: Iterable[str]) -> str | None:
    matches = difflib.get_close_matches(name, list(known), n=1, cutoff=0.75)
    return matches[0] if matches else None


def _unresolved(findings: list[Finding]) -> bool:
    """True when a problem remains that a mechanical rename cannot fix."""
    return any(item.level == "problem" and not item.resolved for item in findings)


# ----------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------
def _inspect_trace(failure: dict[str, Any]) -> list[Finding]:
    trace = failure.get("trace") or []
    done = [step for step in trace if step.get("status") == "ok"]
    if done:
        return [
            Finding(
                "ok",
                f"失败前已执行 {len(done)} 个步骤",
                "、".join(str(step.get("id") or step.get("type")) for step in done),
            )
        ]
    return []


def _classify(failure: dict[str, Any], findings: list[Finding]) -> str:
    error = failure.get("error") or {}
    message = str(error.get("message") or "")
    code = str(error.get("code") or "")
    step = failure.get("step")

    if step is None:
        return "invalid_input"
    if "不存在字段" in message:
        return "unknown_field"
    if any("引用了 `steps." in item.title for item in findings):
        return "forward_reference"
    if any(
        "不存在的字段" in item.title or "没有这个字段" in item.detail
        for item in findings
    ):
        return "unknown_field"
    if code == "expression_error" or "引用路径" in message or "未知变量" in message:
        return "bad_reference"
    if "必填字段缺失" in message:
        return "missing_required"
    if code == "conflict":
        return "revision_conflict"
    if "UNIQUE constraint" in message:
        return "unique_conflict"
    if code == "not_found":
        return "missing_record"
    return "runtime_error"


def _summary(
    category: str, findings: list[Finding], proposal: dict[str, Any] | None
) -> str:
    label = CATEGORY_LABELS.get(category, category)
    if proposal is not None:
        return f"{label}；已生成一个可确认的修复版本（key 不变）。"
    return label + "。"


def _error_message(failure: dict[str, Any]) -> str:
    error = failure.get("error") or {}
    return str(error.get("message") or "未知错误")


def _step_label(failure: dict[str, Any]) -> str:
    step = failure.get("step")
    if not step:
        return "输入校验阶段"
    return f"{step.get('id') or '?'}（{step.get('type')}）"


def _label(step: ActionStep, index: int) -> str:
    return f"步骤 `{step.id or index + 1}`（{step.type}）"


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------
def render_primary_report(
    failure: dict[str, Any],
    inspection: Inspection,
    *,
    model_text: str | None = None,
    llm_text: str | None = None,
) -> str:
    """The message stored in the primary conversation (from the assistant).

    It is written so the user can confirm a fix in one sentence, and so the
    next primary turn still has the full proposal in its history.
    """
    application = failure.get("application") or {}
    action = failure.get("action") or {}
    lines = [
        f"### Action 执行失败排查 · {action.get('name') or action.get('key') or '未知 Action'}",
        "",
        f"- 应用：{application.get('name') or '未知'}（{application.get('id') or '无 id'}）",
        f"- Action：`{action.get('key') or '?'}`（v{action.get('version', '?')}）",
        f"- 失败步骤：{_step_label(failure)}",
        f"- 错误：{_error_message(failure)}",
        "- 事务：已整体回滚，没有写入任何数据",
        "",
        "**自动检查**",
    ]
    for item in inspection.findings:
        marker = {"problem": "❌", "warning": "⚠️", "ok": "✅"}.get(item.level, "•")
        line = f"- {marker} {item.title}"
        if item.detail:
            line += f"：{item.detail}"
        lines.append(line)
    if model_text:
        lines.append("")
        lines.append(model_text)

    lines.append("")
    lines.append("**修复建议**")
    for index, suggestion in enumerate(inspection.suggestions, start=1):
        lines.append(f"{index}. {suggestion}")

    if inspection.proposal is not None:
        import json

        lines.append("")
        lines.append("**建议的 Action 定义（完整，等确认后替换）**")
        lines.append("```json")
        lines.append(json.dumps(inspection.proposal, ensure_ascii=False, indent=2))
        lines.append("```")

    lines.append("")
    if inspection.proposal is not None:
        lines.append("> 回复「按建议修」我就用上面的定义更新这个 Action；也可以先说明要改哪一处。")
    else:
        lines.append("> 这次没能自动给出确定的改动。告诉我想怎么改，或者先确认输入数据是否正常。")
    return "\n".join(lines)


def render_application_relay(
    failure: dict[str, Any], inspection: Inspection
) -> str:
    """Short version the application assistant relays into its own chat."""
    action = failure.get("action") or {}
    name = action.get("name") or action.get("key") or "这次操作"
    if inspection.is_definition_problem:
        reason = inspection.summary.rstrip("。")
        extra = "主助手给出了修复建议"
        if inspection.proposal is not None:
            extra += "和一个可直接替换的定义"
        return (
            f"「{name}」执行失败，我已交给主助手排查：{reason}。"
            f"{extra}，去主助手的对话里回复确认就会修改。"
            "本次执行已经整体回滚，没有写入任何数据。"
        )
    return (
        f"「{name}」执行失败，我已交给主助手排查：{inspection.summary.rstrip('。')}。"
        "这次失败不是 Action 定义写错，更多是本次输入或数据不满足；"
        "本次执行已经整体回滚，没有写入任何数据。"
    )
