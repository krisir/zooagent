"""Unit tests for the eval checks themselves (no AI involved).

If the checks are wrong the whole eval set is meaningless, so they are tested
directly with hand-built snapshots.
"""

from __future__ import annotations

from zooagent.eval.cases import Case, ConceptField, Evolution, Expectation, Range, Turn
from zooagent.eval.checks import evaluate, evaluate_evolution
from zooagent.eval.snapshot import (
    ActionSnapshot,
    AppSnapshot,
    ComponentSnapshot,
    FieldSnapshot,
    ModelSnapshot,
    PageSnapshot,
)


def field(
    key: str,
    name: str,
    type_: str = "text",
    *,
    required: bool = False,
    has_default: bool = False,
    options: int = 0,
    target: str | None = None,
) -> FieldSnapshot:
    return FieldSnapshot(
        key=key,
        name=name,
        type=type_,
        required=required,
        unique=False,
        has_default=has_default,
        option_count=options,
        target_model=target,
    )


def finance_snapshot(version: int = 1, records: dict[str, str] | None = None) -> AppSnapshot:
    category = ModelSnapshot(
        key="category",
        name="分类",
        version=version,
        title_field="name",
        fields=[field("name", "分类名称", required=True)],
        record_count=3,
    )
    transaction = ModelSnapshot(
        key="transaction",
        name="交易",
        version=version,
        title_field="note",
        fields=[
            field("date", "日期", "date", required=True),
            field("amount", "金额", "decimal", required=True),
            field(
                "direction",
                "收支方向",
                "enum",
                required=True,
                options=2,
            ),
            field("category", "分类", "relation", target="category"),
        ],
        record_count=10,
        record_digests=records or {"rec_1": "aaaa", "rec_2": "bbbb"},
    )
    return AppSnapshot(
        application_id="app_1",
        application_name="个人记账",
        models=[category, transaction],
        actions=[
            ActionSnapshot(
                key="record_transaction",
                name="记录交易",
                version=1,
                input_count=2,
                step_count=1,
                referenced_models=["transaction"],
            )
        ],
        pages=[
            PageSnapshot(
                key="overview",
                title="概览",
                components=[
                    ComponentSnapshot(type="DataTable", model="transaction"),
                    ComponentSnapshot(type="StatCard", model="category"),
                ],
            )
        ],
    )


def _case(**expect_kwargs) -> Case:
    return Case(
        id="unit",
        domain="测试",
        turns=[Turn(prompt="x")],
        providers=["mock"],
        expect=Expectation(**expect_kwargs),
    )


def test_passing_snapshot_satisfies_good_expectations() -> None:
    outcome = evaluate(
        _case(
            application_created=True,
            model_count=Range(min=2, max=4),
            relation_count=Range(min=1),
            action_count=Range(min=1),
            data_table_count=Range(min=1),
            tool_failures=Range(max=0),
            core_ops_covered=True,
            relation_targets_resolve=True,
            actions_valid=True,
            no_default_on_required=True,
            enum_options_present=True,
            keys_valid=True,
            field_names_chinese=Range(min=0.6),
            concept_fields=[ConceptField(type="decimal", hints=["amount", "金额"])],
        ),
        finance_snapshot(),
    )
    assert outcome.passed, [f"{c.name}: {c.reason}" for c in outcome.failures]
    assert outcome.metrics["models"] == 2
    assert outcome.metrics["relations"] == 1


def test_missing_concept_field_fails_with_reason() -> None:
    outcome = evaluate(
        _case(concept_fields=[ConceptField(type="integer", hints=["stock", "库存"])]),
        finance_snapshot(),
    )
    assert not outcome.passed
    assert "没有找到" in outcome.failures[0].reason


def test_core_ops_detects_unreferenced_model() -> None:
    snapshot = finance_snapshot()
    snapshot.models.append(
        ModelSnapshot(
            key="orphan",
            name="孤儿模型",
            version=1,
            title_field="name",
            fields=[field("name", "名称")],
        )
    )
    outcome = evaluate(_case(core_ops_covered=True), snapshot)
    assert not outcome.passed
    assert "orphan" in outcome.failures[0].reason


def test_dangling_relation_and_bad_keys_are_caught() -> None:
    snapshot = finance_snapshot()
    snapshot.models[1].fields.append(field("Drop Table", "注入字段", "text"))
    snapshot.models[1].fields.append(
        field("vendor", "供应商", "relation", target="missing_model")
    )
    outcome = evaluate(
        _case(
            keys_valid=True,
            relation_targets_resolve=True,
            forbidden_key_fragments=["drop table"],
        ),
        snapshot,
    )
    names = {check.name for check in outcome.failures}
    assert "keys_valid" in names
    assert "relation_targets_resolve" in names
    assert "forbidden_key_fragments" in names
    assert "no_sql_like_keys" in names


def test_range_reports_actual_value() -> None:
    outcome = evaluate(_case(model_count=Range(min=5)), finance_snapshot())
    assert not outcome.passed
    assert "实际 2" in outcome.failures[0].reason


def test_evolution_detects_lost_field_lost_records_and_stale_version() -> None:
    before = finance_snapshot(version=1)
    after = finance_snapshot(version=2, records={"rec_1": "cccc"})
    # drop a field and lose rec_2
    after.models[1].fields = [item for item in after.models[1].fields if item.key != "category"]

    case = Case(
        id="evo",
        domain="测试",
        turns=[Turn(prompt="a"), Turn(prompt="b")],
        providers=["llm"],
        expect=Expectation(),
        evolution=Evolution(
            field_superset=True,
            records_preserved=True,
            version_increased=True,
            new_concept_fields=[ConceptField(type="text", hints=["account", "账户"])],
        ),
    )
    outcome = evaluate_evolution(case, before, after)
    names = {check.name for check in outcome.failures}
    assert "evolution:field_superset" in names
    assert "evolution:records_preserved" in names
    assert "evolution:new_field:account" in names
    # version increased, so that check passes
    assert "evolution:version_increased" not in names


def test_evolution_passes_when_nothing_is_lost() -> None:
    before = finance_snapshot(version=1)
    after = finance_snapshot(version=2)
    after.models[1].fields.append(field("account", "账户", "relation", target="category"))
    case = Case(
        id="evo",
        domain="测试",
        turns=[Turn(prompt="a"), Turn(prompt="b")],
        providers=["llm"],
        expect=Expectation(),
        evolution=Evolution(
            version_increased=True,
            new_concept_fields=[ConceptField(type="relation", hints=["account", "账户"])],
        ),
    )
    outcome = evaluate_evolution(case, before, after)
    assert outcome.passed, [f"{c.name}: {c.reason}" for c in outcome.failures]


def test_expectation_parser_rejects_unknown_keys() -> None:
    import pytest

    from zooagent.errors import ValidationFailure

    with pytest.raises(ValidationFailure):
        Expectation.parse({"model_count": 3, "unknown_thing": True})
