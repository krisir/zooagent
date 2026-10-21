"""Static checks for AI-authored Action definitions.

Actions are composed by a language model but executed by plain Python, so a
definition can be syntactically valid JSON and still fail on every run (an
unresolvable ``{"ref": ...}``, a ``compute`` expression using unsupported
syntax, a step output nobody declared). Because a failing Action used to be
discovered only when a user clicked the button - after some of its steps had
already written - we validate the definition *before* it is stored and hand the
problems back to the assistant as tool feedback.
"""

from __future__ import annotations

import ast
from typing import Any

from ..errors import ValidationFailure
from .expression import FUNCTIONS, LITERALS
from .schema import ActionDef, ActionStep

ROOTS = {"input", "steps", "now", "app"}

ALLOWED_OPERATORS: tuple[type[ast.AST], ...] = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)

ALLOWED_EXPRESSION_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.UnaryOp,
    ast.BinOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
) + ALLOWED_OPERATORS


def validate_action(action: ActionDef) -> None:
    """Raise :class:`ValidationFailure` listing everything that cannot run."""
    outputs = {step.output for step in action.steps if step.output}
    input_keys = {field.key for field in action.inputs}
    problems: list[str] = []

    for step in action.steps:
        problems.extend(
            _check_step(step, outputs=outputs, input_keys=input_keys, action=action)
        )
    if action.output:
        problems.extend(
            _check_refs(action.output, where="output", outputs=outputs, input_keys=input_keys)
        )
    if problems:
        raise ValidationFailure(
            f"Action '{action.key}' 定义无法执行", details=problems
        )


def _check_step(
    step: ActionStep,
    *,
    outputs: set[str],
    input_keys: set[str],
    action: ActionDef,
) -> list[str]:
    where = f"步骤 '{step.id or step.type}'"
    problems: list[str] = []

    if step.type == "compute":
        problems.extend(_check_expression(step.expression or "", where, outputs, input_keys))

    for item in step.filters:
        problems.extend(_check_refs(item.value, where=f"{where} 的筛选条件", outputs=outputs, input_keys=input_keys))

    for key, value in step.values.items():
        problems.extend(
            _check_refs(value, where=f"{where} 的字段 '{key}'", outputs=outputs, input_keys=input_keys)
        )

    problems.extend(_check_refs(step.record_id, where=f"{where} 的 record_id", outputs=outputs, input_keys=input_keys))
    problems.extend(
        _check_refs(
            step.expected_revision,
            where=f"{where} 的 expected_revision",
            outputs=outputs,
            input_keys=input_keys,
        )
    )
    if step.value:
        problems.extend(_check_refs(step.value, where=f"{where} 的返回值", outputs=outputs, input_keys=input_keys))
    return problems


def _check_refs(
    value: Any,
    where: str,
    *,
    outputs: set[str],
    input_keys: set[str],
) -> list[str]:
    """Validate every ``{"ref": ...}`` / ``{"expr": ...}`` nested in ``value``."""
    if isinstance(value, dict):
        if set(value.keys()) == {"ref"}:
            path = str(value["ref"])
            parts = path.split(".")
            head = parts[0]
            if head in ROOTS:
                if head == "steps" and len(parts) > 1 and parts[1] not in outputs:
                    return [
                        f"{where} 引用了 steps.{parts[1]}，但没有任何步骤输出这个名字"
                        f"（已有的输出：{sorted(outputs) or '无'}）"
                    ]
                if head == "input" and len(parts) > 1 and parts[1] not in input_keys:
                    return [
                        f"{where} 引用了 input.{parts[1]}，但 Action 没有这个输入"
                        f"（已有的输入：{sorted(input_keys) or '无'}）"
                    ]
                return []
            if head in outputs:
                # `{"ref": "record"}` is accepted as shorthand for steps.record.
                return []
            return [
                f"{where} 的引用 '{path}' 无法解析：请以 input./steps./now./app. 开头，"
                f"或使用步骤 output 名（{sorted(outputs) or '无'}）"
            ]
        if set(value.keys()) == {"expr"}:
            return _check_expression(str(value["expr"]), where, outputs, input_keys)
        problems: list[str] = []
        for key, item in value.items():
            problems.extend(_check_refs(item, where=f"{where}.{key}", outputs=outputs, input_keys=input_keys))
        return problems
    if isinstance(value, list):
        problems = []
        for index, item in enumerate(value):
            problems.extend(_check_refs(item, where=f"{where}[{index}]", outputs=outputs, input_keys=input_keys))
        return problems
    return []


def _check_expression(
    expression: str,
    where: str,
    outputs: set[str],
    input_keys: set[str],
) -> list[str]:
    import re

    source = re.sub(r"\$([A-Za-z_])", r"\1", expression)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        return [
            f"{where} 的表达式语法错误：{expression}。只支持 Python 风格的表达式"
            "（条件用 `A if cond else B`，不支持 `cond ? A : B`；空值写 None 或 null）"
        ]

    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_EXPRESSION_NODES):
            problems.append(f"{where} 的表达式使用了不支持的语法 {type(node).__name__}：{expression}")
            break
        if isinstance(node, ast.Name):
            name = node.id
            if name in LITERALS or name in ROOTS or name in input_keys or name in outputs:
                continue
            # Function names are visited separately as Call.func.
            if name in FUNCTIONS:
                continue
            problems.append(
                f"{where} 的表达式使用了未知变量 '{name}'（可用：input/steps/now/app、"
                f"输入字段、步骤输出 {sorted(outputs) or '无'}）"
            )
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                func_name = node.func.id if isinstance(node.func, ast.Name) else "?"
                problems.append(
                    f"{where} 的表达式调用了不存在的函数 '{func_name}'"
                    f"（可用：{sorted(FUNCTIONS)}）"
                )
            if node.keywords:
                problems.append(f"{where} 的表达式不支持关键字参数：{expression}")
        elif isinstance(node, ast.Attribute):
            if node.attr not in {"items", "total", "data", "id", "title"} and node.attr.startswith("_"):
                problems.append(f"{where} 的表达式不允许访问 '{node.attr}'")
    return list(dict.fromkeys(problems))
