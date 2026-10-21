"""A tiny, safe expression language for Action steps.

Actions are composed by the AI but executed by plain Python. We therefore
never ``eval`` arbitrary text: we walk a whitelisted AST ourselves and coerce
numeric strings (money is stored as text) into ``Decimal`` for arithmetic.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import ValidationFailure


class ExpressionError(ValidationFailure):
    code = "expression_error"


def to_number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ExpressionError(f"布尔值不能参与算术运算: {value!r}")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            raise ExpressionError(f"'{value}' 不是数字") from None
    raise ExpressionError(f"{value!r} 不是数字")


def _is_numberish(value: Any) -> bool:
    try:
        to_number(value)
    except ValidationFailure:
        return False
    return True


def _fn_sum(value: Any) -> Decimal:
    items = value if isinstance(value, (list, tuple)) else [value]
    total = Decimal(0)
    for item in items:
        if item is None:
            continue
        total += to_number(item)
    return total


def _fn_avg(value: Any) -> Decimal:
    items = [item for item in (value if isinstance(value, (list, tuple)) else [value]) if item is not None]
    if not items:
        raise ExpressionError("avg 需要至少一个元素")
    return _fn_sum(items) / Decimal(len(items))


def _fn_pluck(records: Any, field: Any) -> list[Any]:
    if not isinstance(records, (list, tuple)):
        raise ExpressionError("pluck 的第一个参数必须是记录列表")
    return [
        (record.get(str(field)) if isinstance(record, dict) else None)
        for record in records
    ]


def _fn_coalesce(*args: Any) -> Any:
    for arg in args:
        if arg is not None and arg != "":
            return arg
    return None


def _fn_round(value: Any, digits: Any = 2) -> Decimal:
    exponent = Decimal(1).scaleb(-int(digits))
    return to_number(value).quantize(exponent)


def _to_date(value: Any) -> date:
    """Accept a date, a datetime or an ISO string ('2026-10-20...')."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            raise ExpressionError(f"'{value}' 不是日期") from None
    raise ExpressionError(f"{value!r} 不是日期")


def _fn_days_between(start: Any, end: Any) -> int:
    """Whole days from ``start`` to ``end`` (negative when ``end`` is earlier)."""
    return (_to_date(end) - _to_date(start)).days


def _fn_add_days(value: Any, days: Any) -> str:
    return (_to_date(value) + timedelta(days=int(to_number(days)))).isoformat()


def _fn_today() -> str:
    return date.today().isoformat()


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sum": _fn_sum,
    "avg": _fn_avg,
    "min": lambda *args: min(*args) if len(args) > 1 else min(args[0]),
    "max": lambda *args: max(*args) if len(args) > 1 else max(args[0]),
    "abs": lambda value: abs(to_number(value)),
    "round": _fn_round,
    "num": to_number,
    "int": lambda value: int(to_number(value)),
    "float": lambda value: float(to_number(value)),
    "str": lambda value: "" if value is None else str(value),
    "len": lambda value: len(value),
    "count": lambda value: len(value),
    "pluck": _fn_pluck,
    "coalesce": _fn_coalesce,
    "days_between": _fn_days_between,
    "add_days": _fn_add_days,
    "today": _fn_today,
    "upper": lambda value: str(value).upper(),
    "lower": lambda value: str(value).lower(),
    "contains": lambda haystack, needle: str(needle) in str(haystack),
}

NUMERIC_OPS: dict[type, Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
}


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate ``expression`` against ``context`` (e.g. ``input``/``steps``).

    ``$input.amount`` is accepted as sugar for ``input.amount`` so that prompts
    and generated Action definitions stay readable.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("表达式不能为空")
    source = re.sub(r"\$([A-Za-z_])", r"\1", expression)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"表达式语法错误: {expression}") from exc
    return _eval(tree.body, context, expression)


LITERALS = {"null": None, "true": True, "false": False}


def _eval(node: ast.AST, context: dict[str, Any], source: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        # LLMs write JSON-ish literals; accept them instead of failing at runtime.
        if node.id in LITERALS:
            return LITERALS[node.id]
        if node.id in context:
            return context[node.id]
        # Step outputs are addressable as `steps.<output>`; accepting the bare
        # output name keeps AI-authored Actions like `{{expr: "record.id"}}`
        # working instead of failing after the write already happened.
        steps = context.get("steps")
        if isinstance(steps, dict) and node.id in steps:
            return steps[node.id]
        raise ExpressionError(f"未知变量 '{node.id}'")

    if isinstance(node, ast.Attribute):
        value = _eval(node.value, context, source)
        if isinstance(value, dict):
            if node.attr in value:
                return value[node.attr]
            raise ExpressionError(f"不存在属性 '{node.attr}'")
        raise ExpressionError(f"{type(value).__name__} 没有属性 '{node.attr}'")

    if isinstance(node, ast.Subscript):
        target = _eval(node.value, context, source)
        key = _eval(node.slice, context, source)
        if isinstance(target, dict):
            if key not in target:
                raise ExpressionError(f"不存在键 '{key}'")
            return target[key]
        if isinstance(target, (list, tuple)):
            index = int(to_number(key))
            if index < 0 or index >= len(target):
                raise ExpressionError(f"索引 {index} 越界")
            return target[index]
        if isinstance(target, str):
            return target[int(to_number(key))]
        raise ExpressionError(f"{type(target).__name__} 不支持下标访问")

    if isinstance(node, ast.List):
        return [_eval(item, context, source) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(item, context, source) for item in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _eval(key, context, source): _eval(value, context, source)
            for key, value in zip(node.keys, node.values, strict=False)
        }

    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, context, source)
        if isinstance(node.op, ast.USub):
            return -to_number(operand)
        if isinstance(node.op, ast.UAdd):
            return +to_number(operand)
        if isinstance(node.op, ast.Not):
            return not operand
        raise ExpressionError("不支持的一元运算")

    if isinstance(node, ast.BinOp):
        left = _eval(node.left, context, source)
        right = _eval(node.right, context, source)
        op = type(node.op)
        if op is ast.Div:
            divisor = to_number(right)
            if divisor == 0:
                raise ExpressionError("除数不能为 0")
            return to_number(left) / divisor
        if op is ast.FloorDiv:
            divisor = to_number(right)
            if divisor == 0:
                raise ExpressionError("除数不能为 0")
            return (to_number(left) / divisor).to_integral_value(rounding="ROUND_DOWN")
        if op is ast.Mod:
            return to_number(left) % to_number(right)
        if op is ast.Pow:
            return to_number(left) ** int(to_number(right))
        if op is ast.Add:
            # Money is stored as text, so treat two number-like operands as a
            # numeric addition and only fall back to concatenation otherwise.
            if _is_numberish(left) and _is_numberish(right):
                return to_number(left) + to_number(right)
            if isinstance(left, str) or isinstance(right, str):
                return f"{left}{right}"
            raise ExpressionError("'+' 需要数字或字符串")
        handler = NUMERIC_OPS.get(op)
        if handler is None:
            raise ExpressionError("不支持的算术运算")
        return handler(to_number(left), to_number(right))

    if isinstance(node, ast.Compare):
        left = _eval(node.left, context, source)
        for op, comparator_node in zip(node.ops, node.comparators, strict=False):
            right = _eval(comparator_node, context, source)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = _eval(value_node, context, source)
                if not result:
                    return result
            return result
        for value_node in node.values:
            result = _eval(value_node, context, source)
            if result:
                return result
        return result

    if isinstance(node, ast.IfExp):
        condition = _eval(node.test, context, source)
        branch = node.body if condition else node.orelse
        return _eval(branch, context, source)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("只允许调用内置函数")
        name = node.func.id
        if name not in FUNCTIONS:
            raise ExpressionError(f"不允许调用函数 '{name}'")
        if node.keywords:
            raise ExpressionError("函数不支持关键字参数")
        args = [_eval(arg, context, source) for arg in node.args]
        return FUNCTIONS[name](*args)

    raise ExpressionError(f"表达式 '{source}' 包含不支持的语法")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Is):
        return left is right
    if isinstance(op, ast.IsNot):
        return left is not right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right

    if _is_numberish(left) and _is_numberish(right):
        left_value, right_value = to_number(left), to_number(right)
    else:
        if left is None or right is None:
            left_value, right_value = left, right  # type: ignore[assignment]
        else:
            left_value, right_value = str(left), str(right)  # type: ignore[assignment]

    if isinstance(op, ast.Eq):
        return left_value == right_value
    if isinstance(op, ast.NotEq):
        return left_value != right_value
    if isinstance(op, ast.Gt):
        return left_value > right_value
    if isinstance(op, ast.GtE):
        return left_value >= right_value
    if isinstance(op, ast.Lt):
        return left_value < right_value
    if isinstance(op, ast.LtE):
        return left_value <= right_value
    raise ExpressionError("不支持的比较运算")


def resolve_path(path: str, context: dict[str, Any]) -> Any:
    current: Any = context
    for index, part in enumerate(path.split(".")):
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            elif (
                index == 0
                and isinstance(context.get("steps"), dict)
                and part in context["steps"]
            ):
                # `{{"ref": "record"}}` is how step outputs are naturally
                # written; the canonical form is `steps.record`.
                current = context["steps"][part]
            else:
                raise ExpressionError(f"引用路径 '{path}' 不存在")
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                raise ExpressionError(f"引用路径 '{path}' 越界") from None
        else:
            raise ExpressionError(f"引用路径 '{path}' 无法解析")
    return current


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    """Resolve a step parameter that may be a literal, ``{"ref": ...}`` or ``{"expr": ...}``."""
    if isinstance(value, dict):
        if set(value.keys()) == {"ref"}:
            return resolve_path(str(value["ref"]), context)
        if set(value.keys()) == {"expr"}:
            return evaluate(str(value["expr"]), context)
        return {key: resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_value(item, context) for item in value]
    return value
