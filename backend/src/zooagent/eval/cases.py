"""Evaluation cases: schema and loader.

Cases are data, not code, so the same set can be run against any model. Written
in TOML because it is structured, readable, and available in the standard
library (``tomllib``, Python 3.11+) - no extra dependency.

Provider kinds:

* ``offline`` - the built-in template planner (no key, no network)
* ``mock``    - the AgentScope-interface mock model (no key, no network)
* ``llm``     - any configured real model (agentscope / openai / ollama ...)

A case lists the providers it must pass on. Cases that require ``llm`` are
skipped (not failed) when no real model is configured, so the set stays usable
in CI.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ValidationFailure

LLM_PROVIDERS = ("agentscope", "openai", "ollama")
KNOWN_PROVIDERS = ("offline", "mock", "llm")


@dataclass
class Range:
    """An optional inclusive range (``min``/``max``)."""

    min: float | None = None
    max: float | None = None

    @classmethod
    def parse(cls, raw: Any) -> Range | None:
        if raw is None:
            return None
        if isinstance(raw, bool):
            return cls(min=1) if raw else cls(max=0)
        if isinstance(raw, (int, float)):
            return cls(min=float(raw), max=float(raw))
        if isinstance(raw, dict):
            return cls(min=raw.get("min"), max=raw.get("max"))
        raise ValidationFailure(f"无法解析范围: {raw!r}")

    def check(self, value: float) -> str | None:
        if self.min is not None and value < self.min:
            return f"期望 ≥ {self.min:g}，实际 {value:g}"
        if self.max is not None and value > self.max:
            return f"期望 ≤ {self.max:g}，实际 {value:g}"
        return None


@dataclass
class ConceptField:
    """A field that must exist somewhere, described semantically.

    Model/field keys are chosen by the AI, so cases match on *type + hints*
    instead of an exact key: e.g. "a decimal field about amounts somewhere".
    """

    type: str
    hints: list[str]
    required: bool = True
    description: str = ""

    def matches(self, field_type: str, key: str, name: str) -> bool:
        if self.type and field_type != self.type:
            return False
        haystack = f"{key} {name}".lower()
        return any(hint.lower() in haystack for hint in self.hints)


@dataclass
class Evolution:
    """What must hold between the first turn and the last turn."""

    #: models may gain fields but never lose them
    field_superset: bool = True
    #: records that existed before must still exist, with the same data
    records_preserved: bool = True
    #: every model must be on a later version than turn 1
    version_increased: bool = False
    #: at least one new field matching these hints must appear
    new_concept_fields: list[ConceptField] = field(default_factory=list)


@dataclass
class Expectation:
    application_created: bool | None = None
    application_count: Range | None = None
    model_count: Range | None = None
    total_field_count: Range | None = None
    relation_count: Range | None = None
    action_count: Range | None = None
    action_step_count: Range | None = None
    page_count: Range | None = None
    data_table_count: Range | None = None
    component_count: Range | None = None
    record_count: Range | None = None
    tool_calls: Range | None = None
    tool_failures: Range | None = None
    concept_fields: list[ConceptField] = field(default_factory=list)
    #: every model must be reachable from some page (可完成核心操作)
    core_ops_covered: bool | None = None
    #: relations must point at models that exist (contract ②)
    relation_targets_resolve: bool | None = None
    #: actions must have deterministic steps and refer to existing models
    actions_valid: bool | None = None
    #: required fields must not also declare a default
    no_default_on_required: bool | None = None
    #: enum fields must declare options
    enum_options_present: bool | None = None
    #: keys must be snake_case, non-reserved, non-SQL-looking
    keys_valid: bool | None = None
    #: ratio of Chinese user-facing field names (quality signal, not correctness)
    field_names_chinese: Range | None = None
    #: fragments that must not appear in any model/field key (injection guard)
    forbidden_key_fragments: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Expectation:
        concept_fields = [
            ConceptField(
                type=item.get("type", ""),
                hints=list(item.get("hints") or []),
                required=bool(item.get("required", True)),
                description=item.get("description", ""),
            )
            for item in raw.get("concept_fields", [])
        ]
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kwargs: dict[str, Any] = {}
        for key, value in raw.items():
            if key == "concept_fields":
                continue
            if key not in known:
                raise ValidationFailure(f"未知的期望项: {key}")
            if key in {
                "application_created",
                "core_ops_covered",
                "relation_targets_resolve",
                "actions_valid",
                "no_default_on_required",
                "enum_options_present",
                "keys_valid",
            }:
                kwargs[key] = bool(value)
            elif key == "forbidden_key_fragments":
                kwargs[key] = [str(item) for item in value]
            else:
                parsed = Range.parse(value)
                if parsed is not None:
                    kwargs[key] = parsed
        return cls(concept_fields=concept_fields, **kwargs)

    def is_empty(self) -> bool:
        return all(
            getattr(self, name) in (None, [], False)
            for name in self.__dataclass_fields__  # type: ignore[attr-defined]
        )


@dataclass
class Turn:
    """One user message, with expectations that must hold after it."""

    prompt: str
    expect: Expectation | None = None
    note: str = ""


@dataclass
class Case:
    id: str
    domain: str
    turns: list[Turn]
    providers: list[str]
    expect: Expectation
    evolution: Evolution | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def prompt(self) -> str:
        return self.turns[0].prompt if self.turns else ""

    @property
    def is_multi_turn(self) -> bool:
        return len(self.turns) > 1

    def requires(self, provider_kind: str) -> bool:
        return provider_kind in self.providers


def _parse_case(raw: dict[str, Any], source: Path) -> Case:
    case_id = raw.get("id")
    if not case_id:
        raise ValidationFailure(f"{source}: 每个 case 必须有 id")
    turns: list[Turn] = []
    for item in raw.get("turns", []):
        turns.append(
            Turn(
                prompt=item["prompt"],
                expect=Expectation.parse(item["expect"]) if item.get("expect") else None,
                note=item.get("note", ""),
            )
        )
    if not turns and raw.get("prompt"):
        turns.append(Turn(prompt=raw["prompt"]))
    if not turns:
        raise ValidationFailure(f"{source}: case '{case_id}' 缺少 prompt 或 turns")

    providers = list(raw.get("providers") or ["offline", "mock", "llm"])
    unknown = [item for item in providers if item not in KNOWN_PROVIDERS]
    if unknown:
        raise ValidationFailure(f"{source}: case '{case_id}' 的 providers 未知: {unknown}")

    evolution_raw = raw.get("evolution")
    evolution = None
    if evolution_raw:
        evolution = Evolution(
            field_superset=bool(evolution_raw.get("field_superset", True)),
            records_preserved=bool(evolution_raw.get("records_preserved", True)),
            version_increased=bool(evolution_raw.get("version_increased", False)),
            new_concept_fields=[
                ConceptField(
                    type=item.get("type", ""),
                    hints=list(item.get("hints") or []),
                )
                for item in evolution_raw.get("new_concept_fields", [])
            ],
        )

    return Case(
        id=case_id,
        domain=raw.get("domain", ""),
        turns=turns,
        providers=providers,
        expect=Expectation.parse(raw.get("expect", {})),
        evolution=evolution,
        tags=list(raw.get("tags") or []),
        notes=raw.get("notes", ""),
    )


def load_cases(path: str | Path) -> list[Case]:
    file = Path(path)
    if not file.is_file():
        raise ValidationFailure(f"找不到用例文件: {file}")
    data = tomllib.loads(file.read_text("utf-8"))
    cases = [_parse_case(raw, file) for raw in data.get("case", [])]
    if not cases:
        raise ValidationFailure(f"{file}: 没有定义任何 case")
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValidationFailure(f"{file}: case id 重复: {case.id}")
        seen.add(case.id)
    return cases


def default_cases_path() -> Path:
    """``backend/eval/cases.toml`` (sibling of the ``src`` tree)."""
    # .../backend/src/zooagent/eval/cases.py -> parents[3] == backend
    return Path(__file__).resolve().parents[3] / "eval" / "cases.toml"


__all__ = [
    "KNOWN_PROVIDERS",
    "LLM_PROVIDERS",
    "Case",
    "ConceptField",
    "Evolution",
    "Expectation",
    "Range",
    "Turn",
    "default_cases_path",
    "load_cases",
]
