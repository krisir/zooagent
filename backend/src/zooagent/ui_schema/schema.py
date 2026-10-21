"""Declarative UI schema.

The AI writes a page description once, when it creates or evolves an app. The
React renderer renders it on every open - no model calls, no generated code.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ValidationFailure
from ..ids import new_id

ComponentType = Literal[
    "Heading",
    "Text",
    "Divider",
    "DataTable",
    "Form",
    "DetailView",
    "StatCard",
    "Chart",
    "Calendar",
    "Tabs",
    "ActionButton",
    "FilterPanel",
    "ChatPanel",
]

LayoutType = Literal["vertical", "grid", "two_column"]


class UIActionRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    kind: Literal["create", "edit", "delete", "run_action", "open_page", "chat"] = "create"
    action: str | None = None
    page: str | None = None
    prompt: str | None = None


class UIComponent(BaseModel):
    """A component instance. Unknown keys are allowed in ``props`` only."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: ComponentType
    title: str | None = None
    description: str | None = None
    model: str | None = None
    fields: list[str] | None = None
    columns: list[dict[str, Any]] | None = None
    actions: list[UIActionRef] = Field(default_factory=list)
    props: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _assign_id(self) -> UIComponent:
        if not self.id:
            self.id = new_id("cmp")
        return self


class UIPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    key: str
    title: str
    icon: str | None = None
    description: str | None = None
    layout: LayoutType = "vertical"
    components: list[UIComponent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _assign_id(self) -> UIPage:
        if not self.id:
            self.id = new_id("pg")
        return self


class UISchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[UIPage] = Field(default_factory=list)
    home_page: str | None = None

    @model_validator(mode="after")
    def _unique_pages(self) -> UISchema:
        seen: set[str] = set()
        for page in self.pages:
            if page.key in seen:
                raise ValidationFailure(f"页面 key '{page.key}' 重复")
            seen.add(page.key)
        if self.home_page and self.home_page not in seen:
            raise ValidationFailure(f"home_page '{self.home_page}' 不存在")
        return self
