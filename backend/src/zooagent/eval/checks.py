"""Evaluate expectations against a snapshot.

Every check returns a human-readable failure reason (or ``None`` when it
passes), so a failing eval says *what* is wrong - e.g. "缺少金额字段" - rather
than just "case failed".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .cases import Case, ConceptField, Range
from .snapshot import AppSnapshot, ModelSnapshot

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SQLISH = ("select ", "insert ", "drop ", "delete ", ";", "--", "/*", "sqlite_")
#: components that can create records
CREATABLE_COMPONENTS = {"Form", "DataTable"}


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "reason": self.reason}


@dataclass
class EvalOutcome:
    checks: list[CheckResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [item for item in self.checks if not item.passed]


def _range_check(
    results: list[CheckResult], name: str, expect: Range | None, value: float
) -> None:
    if expect is None:
        return
    reason = expect.check(value)
    results.append(CheckResult(name, reason is None, reason or ""))


def _bool_check(
    results: list[CheckResult], name: str, expect: bool | None, ok: bool, reason: str
) -> None:
    if expect is None:
        return
    if expect:
        results.append(CheckResult(name, ok, "" if ok else reason))
    else:  # expectation is "must NOT hold"
        results.append(CheckResult(name, not ok, "" if not ok else reason))


def _matches_any(field_type: str, key: str, name: str, concept: ConceptField) -> bool:
    return concept.matches(field_type, key, name)


def _concept_label(concept: ConceptField) -> str:
    """Stable identifier for a concept check: explicit label, else first hint."""
    if concept.description:
        return concept.description
    if concept.hints:
        return concept.hints[0]
    return concept.type


def _concept_check(
    results: list[CheckResult], snapshot: AppSnapshot, concept: ConceptField
) -> None:
    label = _concept_label(concept)
    found = any(
        _matches_any(item.type, item.key, item.name, concept)
        for _, item in snapshot.all_fields
    )
    if not found and not concept.required:
        return
    results.append(
        CheckResult(
            f"concept_field:{label}",
            found,
            "" if found else f"没有找到类型 {concept.type} 且名称/描述包含 {concept.hints} 的字段",
        )
    )


def evaluate(case: Case, snapshot: AppSnapshot) -> EvalOutcome:
    """Run every expectation in ``case`` against ``snapshot``."""
    expect = case.expect
    results: list[CheckResult] = []

    _bool_check(
        results,
        "application_created",
        expect.application_created,
        bool(snapshot.application_id),
        "没有创建应用",
    )
    _range_check(results, "application_count", expect.application_count, 1 if snapshot.application_id else 0)
    _range_check(results, "model_count", expect.model_count, snapshot.model_count)
    _range_check(results, "total_field_count", expect.total_field_count, snapshot.field_count)
    _range_check(results, "relation_count", expect.relation_count, snapshot.relation_count)
    _range_check(results, "action_count", expect.action_count, snapshot.action_count)
    _range_check(results, "action_step_count", expect.action_step_count, snapshot.action_step_count)
    _range_check(results, "page_count", expect.page_count, snapshot.page_count)
    _range_check(results, "data_table_count", expect.data_table_count, snapshot.data_table_count)
    _range_check(results, "component_count", expect.component_count, snapshot.component_count)
    _range_check(results, "record_count", expect.record_count, snapshot.record_count)
    _range_check(results, "tool_calls", expect.tool_calls, snapshot.tool_calls)
    _range_check(results, "tool_failures", expect.tool_failures, snapshot.tool_failures)
    _range_check(
        results,
        "field_names_chinese",
        expect.field_names_chinese,
        snapshot.chinese_field_name_ratio,
    )

    for concept in expect.concept_fields:
        _concept_check(results, snapshot, concept)

    # ② actions must be deterministic and reference real models
    if expect.actions_valid is not None:
        problems: list[str] = []
        for action in snapshot.actions:
            if action.step_count < 1:
                problems.append(f"{action.key} 没有步骤")
            unknown = [item for item in action.referenced_models if item not in snapshot.model_keys()]
            if unknown:
                problems.append(f"{action.key} 引用了不存在的模型 {unknown}")
        if expect.actions_valid and snapshot.actions and not snapshot.actions[0].step_count:
            problems.append("Action 缺少确定性步骤")
        _bool_check(
            results,
            "actions_valid",
            expect.actions_valid,
            not problems,
            "；".join(problems),
        )

    # ③ the UI must be able to do the core operations
    if expect.core_ops_covered is not None:
        covered = snapshot.models_referenced_by_pages()
        missing = sorted(snapshot.model_keys() - covered)
        has_creator = any(
            component.type in CREATABLE_COMPONENTS
            for page in snapshot.pages
            for component in page.components
        )
        problems = []
        if missing:
            problems.append(f"这些模型没有任何页面引用: {missing}")
        if snapshot.models and not has_creator:
            problems.append("没有可以录入数据的表格或表单")
        _bool_check(
            results,
            "core_ops_covered",
            expect.core_ops_covered,
            not problems,
            "；".join(problems),
        )

    if expect.relation_targets_resolve is not None:
        dangling = sorted(
            {
                item.target_model
                for _, item in snapshot.all_fields
                if item.type == "relation" and item.target_model not in snapshot.model_keys()
            }
        )
        _bool_check(
            results,
            "relation_targets_resolve",
            expect.relation_targets_resolve,
            not dangling,
            f"关系指向不存在的模型: {dangling}",
        )

    if expect.no_default_on_required is not None:
        bad = [
            f"{model.key}.{item.key}"
            for model, item in snapshot.all_fields
            if item.required and item.has_default
        ]
        _bool_check(
            results,
            "no_default_on_required",
            expect.no_default_on_required,
            not bad,
            f"这些必填字段又设了默认值: {bad}",
        )

    if expect.enum_options_present is not None:
        bad = [
            f"{model.key}.{item.key}"
            for model, item in snapshot.all_fields
            if item.type == "enum" and item.option_count == 0
        ]
        _bool_check(
            results,
            "enum_options_present",
            expect.enum_options_present,
            not bad,
            f"枚举字段缺少选项: {bad}",
        )

    if expect.keys_valid is not None:
        bad: list[str] = []
        for model in snapshot.models:
            if not SNAKE_CASE.match(model.key):
                bad.append(f"模型 key 不规范: {model.key}")
            for item in model.fields:
                if not SNAKE_CASE.match(item.key):
                    bad.append(f"字段 key 不规范: {model.key}.{item.key}")
        _bool_check(results, "keys_valid", expect.keys_valid, not bad, "；".join(bad))

    if expect.forbidden_key_fragments:
        bad = []
        for model in snapshot.models:
            haystack = f"{model.key} {model.name}".lower()
            for fragment in expect.forbidden_key_fragments:
                if fragment.lower() in haystack:
                    bad.append(f"{model.key} 含有禁止片段 {fragment!r}")
            for item in model.fields:
                haystack = f"{item.key} {item.name}".lower()
                for fragment in expect.forbidden_key_fragments:
                    if fragment.lower() in haystack:
                        bad.append(f"{model.key}.{item.key} 含有禁止片段 {fragment!r}")
        _bool_check(
            results,
            "forbidden_key_fragments",
            True,
            not bad,
            "；".join(bad),
        )
        sqlish = [
            f"{model.key}.{item.key}"
            for model, item in snapshot.all_fields
            if any(token in f"{item.key} {item.name}".lower() for token in SQLISH)
        ]
        results.append(
            CheckResult(
                "no_sql_like_keys",
                not sqlish,
                "" if not sqlish else f"字段名像 SQL 片段: {sqlish}",
            )
        )

    return EvalOutcome(checks=results, metrics=snapshot.metrics())


def evaluate_evolution(
    case: Case,
    first: AppSnapshot,
    last: AppSnapshot,
) -> EvalOutcome:
    """Check design-document item ④/⑤: a follow-up requirement must not lose data."""
    evolution = case.evolution
    if evolution is None:
        return EvalOutcome()

    results: list[CheckResult] = []

    if evolution.field_superset:
        lost: list[str] = []
        for model in first.models:
            current = next((item for item in last.models if item.key == model.key), None)
            if current is None:
                lost.append(f"模型 {model.key} 消失了")
                continue
            before = {item.key for item in model.fields}
            after = {item.key for item in current.fields}
            missing = sorted(before - after)
            if missing:
                lost.append(f"{model.key} 丢失字段 {missing}")
        results.append(
            CheckResult("evolution:field_superset", not lost, "；".join(lost))
        )

    if evolution.version_increased:
        stale = [
            model.key
            for model in first.models
            if (current := next((item for item in last.models if item.key == model.key), None))
            and current.version <= model.version
        ]
        results.append(
            CheckResult(
                "evolution:version_increased",
                not stale,
                "" if not stale else f"这些模型版本没有增加: {stale}",
            )
        )

    if evolution.records_preserved:
        before = _record_fingerprints(first)
        after = _record_fingerprints(last)
        lost_keys = sorted(set(before) - set(after))
        changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
        problems: list[str] = []
        if lost_keys:
            problems.append(f"{len(lost_keys)} 条旧记录丢失")
        if changed:
            problems.append(f"{len(changed)} 条旧记录内容被改变")
        results.append(
            CheckResult(
                "evolution:records_preserved",
                not problems,
                "；".join(problems),
            )
        )

    for concept in evolution.new_concept_fields:
        found = any(
            _matches_any(item.type, item.key, item.name, concept)
            for _, item in last.all_fields
        )
        label = _concept_label(concept)
        results.append(
            CheckResult(
                f"evolution:new_field:{label}",
                found,
                "" if found else f"追加需求后没有新增符合 {concept.type}/{concept.hints} 的字段",
            )
        )

    return EvalOutcome(checks=results, metrics={})


def _record_fingerprints(snapshot: AppSnapshot) -> dict[str, str]:
    """Per-record fingerprints (``model/record`` -> content digest).

    Counts alone would not catch an upgrade that rewrites values, so the checks
    compare content digests for the first ``DIGEST_LIMIT`` records per model.
    """
    fingerprints: dict[str, str] = {}
    for model in snapshot.models:
        for record_id, digest in model.record_digests.items():
            fingerprints[f"{model.key}/{record_id}"] = digest
    return fingerprints


def summarize(models: list[ModelSnapshot]) -> str:
    """Compact one-line description of generated models (for the report)."""
    return ", ".join(f"{model.key}({len(model.fields)})" for model in models)


__all__ = ["CheckResult", "EvalOutcome", "evaluate", "evaluate_evolution", "summarize"]
