"""Action definitions: a deterministic, versioned business operation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ValidationFailure
from ..ids import new_id
from ..models.schema import FieldDef

StepType = Literal["query", "aggregate", "create", "update", "delete", "compute", "return"]


class StepFilter(BaseModel):
    """A filter whose value may be a literal, ``{"ref": ...}`` or ``{"expr": ...}``."""

    model_config = ConfigDict(extra="forbid")

    field: str
    operator: str = "eq"
    value: Any = None


class ActionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: StepType
    description: str | None = None
    # query / aggregate / create / update / delete
    model: str | None = None
    filters: list[StepFilter] = Field(default_factory=list)
    filter_logic: Literal["and", "or"] = "and"
    sort: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = None
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[dict[str, Any]] = Field(default_factory=list)
    # create / update
    values: dict[str, Any] = Field(default_factory=dict)
    record_id: Any = None
    expected_revision: Any = None
    # compute / return
    expression: str | None = None
    value: dict[str, Any] | None = None
    # output variable name for query/aggregate/create/update/compute
    output: str | None = None

    @field_validator("sort")
    @classmethod
    def _check_sort(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in value:
            if "field" not in item:
                raise ValueError("sort 项必须包含 field")
            if item.get("direction", "asc") not in {"asc", "desc"}:
                raise ValueError("sort direction 只能是 asc 或 desc")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> ActionStep:
        if self.type in {"query", "aggregate", "create", "update", "delete"} and not self.model:
            raise ValueError(f"步骤类型 {self.type} 必须指定 model")
        if self.type == "compute" and not self.expression:
            raise ValueError("compute 步骤必须提供 expression")
        if self.type == "return" and not self.value:
            raise ValueError("return 步骤必须提供 value")
        if self.type in {"create", "update"} and not self.values:
            raise ValueError(f"{self.type} 步骤必须提供 values")
        if self.type in {
            "query",
            "aggregate",
            "create",
            "update",
            "compute",
        } and not self.output:
            raise ValueError(f"{self.type} 步骤必须提供 output 变量名")
        return self


class ActionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    key: str
    name: str
    description: str | None = None
    version: int = 1
    status: str = "active"
    # Safe to expose to an LLM as a tool: no side effects for read-only actions.
    read_only: bool = False
    inputs: list[FieldDef] = Field(default_factory=list)
    steps: list[ActionStep] = Field(default_factory=list)
    output: dict[str, Any] | None = None

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        from ..models.schema import KEY_PATTERN

        value = value.strip().lower()
        if not KEY_PATTERN.match(value):
            raise ValueError("Action key 必须以小写字母开头，只能包含小写字母、数字和下划线")
        return value

    @model_validator(mode="after")
    def _check_steps(self) -> ActionDef:
        if not self.steps:
            raise ValidationFailure(f"Action '{self.key}' 至少需要一个步骤")
        seen: set[str] = set()
        for step in self.steps:
            if not step.id:
                step.id = new_id("step")
            if step.id in seen:
                raise ValidationFailure(f"Action '{self.key}' 存在重复步骤 id '{step.id}'")
            seen.add(step.id)
        for field in self.inputs:
            if not field.id:
                field.id = new_id("fld")
        return self

    def step(self, step_id: str) -> ActionStep | None:
        for item in self.steps:
            if item.id == step_id:
                return item
        return None

    def input_model(self):
        from ..models.schema import ModelDef

        return ModelDef(
            key=f"action_{self.key}_input",
            name=f"{self.name} 输入",
            fields=self.inputs or [FieldDef(key="noop", name="无", type="text")],
        )

    def to_prompt_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "inputs": [field.to_prompt_dict() for field in self.inputs],
            "read_only": self.read_only,
        }
        if self.description:
            data["description"] = self.description
        return data
