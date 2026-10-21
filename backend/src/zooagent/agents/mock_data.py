"""Mock business data synthesis, for UI testing.

Generates values that satisfy the *real* model constraints (types, enums,
relations, required fields, uniqueness) and writes them through the Data
Engine, so anything generated here is indistinguishable from user-entered data.

Design notes:

* Values are picked from Chinese pools chosen by field name/key heuristics, so
  a table of 200 rows looks like a real app instead of ``text_1`` / ``text_2``.
* A single ``random.Random(seed)`` makes a dataset reproducible.
* Relations are backfilled from records that already exist, so dependency
  order matters and is resolved topologically.
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationFailure
from ..models.engine import ModelEngine
from ..models.schema import FieldDef, ModelDef
from ..models.validation import normalize_unique_value

MAX_RECORDS_PER_CALL = 2000
#: Reference/dimension models (categories, accounts, ...) should look like a
#: real app: a handful of rows, not hundreds.
REFERENCE_MODEL_CAP = 60
DIMENSION_MODEL_RECORDS = 10
OTHER_MODEL_RECORDS = 30


POOLS: dict[str, list[str]] = {
    "person": [
        "张伟", "王芳", "李娜", "刘洋", "陈静", "杨帆", "赵敏", "黄磊", "周涛", "吴倩",
        "徐强", "孙悦", "马超", "朱琳", "胡歌", "郭静", "何伟", "高阳", "林丹", "罗敏",
    ],
    "company": [
        "晨光科技", "海纳贸易", "云图设计", "恒信物流", "博远咨询", "星野传媒", "同辉电子",
        "嘉禾食品", "锦程地产", "锐思软件", "万通建材", "清风环保",
    ],
    "product": [
        "无线鼠标", "机械键盘", "笔记本支架", "蓝牙耳机", "显示器", "移动硬盘", "摄像头",
        "USB 集线器", "桌面台灯", "人体工学椅", "保温杯", "双肩背包",
    ],
    "category_expense": ["餐饮", "交通", "购物", "居住", "娱乐", "医疗", "教育", "通讯"],
    "category_income": ["工资", "奖金", "投资收益", "兼职", "红包", "报销"],
    "account": ["现金", "微信零钱", "支付宝", "招商银行", "工商银行", "信用卡"],
    "note": [
        "工作午餐", "周末采购", "打车去公司", "team building", "与客户沟通", "月度结算",
        "补货", "日常开销", "临时支出", "报销单据", "订阅续费", "节日礼物",
        "地铁通勤", "咖啡", "水果", "外卖", "快递", "水电费",
    ],
    "summary": [
        "电话沟通需求", "上门拜访", "发送报价单", "跟进入驻流程", "确认合同细节",
        "参加行业展会", "微信同步进度", "安排产品演示", "回访满意度", "催收账款",
    ],
    "title": [
        "整理季度报表", "准备周会材料", "预约体检", "更新客户资料", "复核发票",
        "跟进物流异常", "回复询价邮件", "复盘上月数据", "整理合同归档", "安排培训",
    ],
    "address": [
        "北京市朝阳区建国路 88 号", "上海市浦东新区世纪大道 100 号", "广州市天河区体育西路 5 号",
        "深圳市南山区科技园南区", "杭州市西湖区文三路 199 号", "成都市高新区天府大道 1 号",
    ],
    "phone": ["138", "139", "150", "158", "176", "186", "199"],
    "email_domain": ["example.com", "mail.cn", "corp.cn", "demo.cn"],
}

#: Field key/name fragments that steer value generation.
_PERSON_HINTS = ("customer", "contact", "客户", "联系人", "负责人", "客户名称", "姓名", "owner")
_COMPANY_HINTS = ("company", "vendor", "supplier", "公司", "供应商", "企业")
_PRODUCT_HINTS = ("product", "item", "goods", "商品", "产品", "物料", "sku")
_ACCOUNT_HINTS = ("account", "账户", "账号")
_CATEGORY_HINTS = ("category", "分类", "类别", "标签")
_NOTE_HINTS = ("note", "remark", "comment", "备注", "描述", "说明", "memo")
_SUMMARY_HINTS = ("summary", "content", "detail", "内容", "摘要", "跟进", "记录")
_TITLE_HINTS = ("title", "name", "标题", "名称", "subject")
_ADDRESS_HINTS = ("address", "地址", "位置")
_PHONE_HINTS = ("phone", "mobile", "tel", "电话", "手机")
_EMAIL_HINTS = ("email", "mail", "邮箱")
_CODE_HINTS = ("code", "no", "number", "编号", "单号", "序号")
_MONEY_HINTS = ("amount", "price", "balance", "cost", "total", "fee", "金额", "价格", "余额", "费用", "合计")
_QUANTITY_HINTS = ("quantity", "qty", "count", "stock", "数量", "库存", "件数")
_DATE_PAST_HINTS = ("date", "time", "日期", "时间", "created", "joined", "发生")
_DATE_FUTURE_HINTS = ("due", "deadline", "expire", "截止", "到期", "计划")

EXPENSE_DAY_RANGE = 180
FUTURE_DAY_RANGE = 30


def _matches(field_def: FieldDef, hints: tuple[str, ...]) -> bool:
    haystack = f"{field_def.key} {field_def.name}".lower()
    return any(hint.lower() in haystack for hint in hints)


def _pool_for(field_def: FieldDef, *, income: bool) -> list[str]:
    if _matches(field_def, _PERSON_HINTS):
        return POOLS["person"]
    if _matches(field_def, _COMPANY_HINTS):
        return POOLS["company"]
    if _matches(field_def, _PRODUCT_HINTS):
        return POOLS["product"]
    if _matches(field_def, _ACCOUNT_HINTS):
        return POOLS["account"]
    if _matches(field_def, _CATEGORY_HINTS):
        return POOLS["category_income"] if income else POOLS["category_expense"]
    if _matches(field_def, _SUMMARY_HINTS):
        return POOLS["summary"]
    if _matches(field_def, _NOTE_HINTS):
        return POOLS["note"]
    if _matches(field_def, _ADDRESS_HINTS):
        return POOLS["address"]
    if _matches(field_def, _TITLE_HINTS):
        return POOLS["title"]
    return POOLS["note"]


@dataclass
class MockContext:
    """Everything the synthesizer needs besides the field definition."""

    rng: random.Random
    today: dt.date = field(default_factory=dt.date.today)
    #: target model key -> existing record ids (for relation fields)
    relations: dict[str, list[str]] = field(default_factory=dict)
    #: field key -> values already used in this batch (for unique fields)
    used: dict[str, set[str]] = field(default_factory=dict)
    counter: int = 0

    def next_counter(self) -> int:
        self.counter += 1
        return self.counter


def _enum_value(field_def: FieldDef, context: MockContext, *, income: bool) -> str:
    options = field_def.option_values
    if not options:
        raise ValidationFailure(f"枚举字段 '{field_def.key}' 没有可选值")
    preferred = ["income", "in", "vip", "done", "paid", "active"] if income else [
        "expense",
        "out",
        "normal",
        "todo",
        "pending",
        "inactive",
    ]
    candidates = [option for option in options if option in preferred]
    # Weight toward the "interesting" branch so charts are not lopsided, but
    # keep every option reachable.
    if candidates and context.rng.random() < 0.75:
        return context.rng.choice(candidates)
    return context.rng.choice(options)


def synthesize_value(
    field_def: FieldDef, model: ModelDef, context: MockContext, *, income: bool = False
) -> Any:
    """Produce one plausible value for ``field_def``."""
    if field_def.type == "relation":
        if not field_def.target_model:
            return None
        candidates = context.relations.get(field_def.target_model, [])
        if not candidates:
            return [] if field_def.multiple else None
        if field_def.multiple:
            picks = context.rng.sample(
                candidates, k=min(len(candidates), context.rng.randint(1, 2))
            )
            return picks
        return context.rng.choice(candidates)

    if field_def.type == "enum":
        return _enum_value(field_def, context, income=income)

    if field_def.type == "boolean":
        return context.rng.random() < 0.35

    if field_def.type == "decimal":
        if _matches(field_def, _MONEY_HINTS) or _matches(field_def, ("amount", "金额")):
            # Income rows look like salaries, expense rows like daily spending,
            # so StatCards and pie charts blend both without looking synthetic.
            value = context.rng.uniform(3000, 15000) if income else context.rng.uniform(8, 800)
        elif _matches(field_def, _QUANTITY_HINTS):
            value = float(context.rng.randint(1, 40))
        else:
            value = context.rng.uniform(1, 100)
        scale = field_def.scale if field_def.scale is not None else 2
        return f"{value:.{scale}f}"

    if field_def.type == "integer":
        if _matches(field_def, _QUANTITY_HINTS):
            return context.rng.randint(1, 50)
        if _matches(field_def, ("age", "年龄", "score", "评分")):
            return context.rng.randint(18, 65)
        return context.rng.randint(1, 500)

    if field_def.type == "date":
        if _matches(field_def, _DATE_FUTURE_HINTS):
            offset = context.rng.randint(1, FUTURE_DAY_RANGE)
        else:
            offset = -context.rng.randint(0, EXPENSE_DAY_RANGE)
        return (context.today + dt.timedelta(days=offset)).isoformat()

    if field_def.type == "datetime":
        moment = dt.datetime.combine(
            context.today - dt.timedelta(days=context.rng.randint(0, EXPENSE_DAY_RANGE)),
            dt.time(
                hour=context.rng.randint(8, 22),
                minute=context.rng.choice([0, 5, 15, 30, 45]),
            ),
        )
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    if field_def.type == "json":
        return {"source": "mock", "tags": context.rng.sample(["a", "b", "c"], k=1)}

    # text
    if _matches(field_def, _PHONE_HINTS):
        return f"{context.rng.choice(POOLS['phone'])}{context.rng.randint(10000000, 99999999)}"
    if _matches(field_def, _EMAIL_HINTS):
        return f"user{context.rng.randint(1000, 9999)}@{context.rng.choice(POOLS['email_domain'])}"
    if _matches(field_def, _CODE_HINTS):
        return f"{field_def.key[:2].upper()}{context.rng.randint(100000, 999999)}"

    pool = _pool_for(field_def, income=income)
    value = context.rng.choice(pool)
    if field_def.unique:
        # Uniqueness must hold against the *database*, not just this batch, so
        # suffix until we find a free value.
        used = context.used.setdefault(field_def.key, set())
        candidate = value
        attempts = 0
        while normalize_unique_value(field_def, candidate) in used and attempts < 400:
            attempts += 1
            candidate = f"{value}{context.next_counter()}"
        normalized = normalize_unique_value(field_def, candidate)
        if normalized is not None:
            used.add(normalized)
        return candidate
    return value


def _relation_candidates(
    conn: sqlite3.Connection, models: ModelEngine, model: ModelDef
) -> dict[str, list[str]]:
    relations: dict[str, list[str]] = {}
    for field_def in model.relation_fields():
        target = models.get_model_optional(conn, field_def.target_model or "")
        if target is None:
            continue
        rows = conn.execute(
            """
            SELECT id FROM records
            WHERE model_id = ? AND deleted_at IS NULL
            ORDER BY created_at DESC LIMIT ?
            """,
            (target.id, REFERENCE_MODEL_CAP),
        ).fetchall()
        relations[target.key] = [row["id"] for row in rows]
    return relations


def direction_field_of(model: ModelDef) -> FieldDef | None:
    """The enum field that carries income/expense semantics, if any."""
    for field_def in model.fields:
        if field_def.type != "enum":
            continue
        options = set(field_def.option_values)
        if {"income", "expense"} <= options or {"in", "out"} <= options:
            return field_def
    return None


def _existing_unique_values(
    conn: sqlite3.Connection, model: ModelDef
) -> dict[str, set[str]]:
    """Seed the ``used`` bookkeeping with values already stored in the database."""
    used: dict[str, set[str]] = {}
    for field_def in model.fields:
        if not field_def.unique:
            continue
        rows = conn.execute(
            """
            SELECT value FROM record_unique_keys
            WHERE model_id = ? AND field_key = ?
            """,
            (model.id, field_def.key),
        ).fetchall()
        used[field_def.key] = {row["value"] for row in rows}
    return used


def generate_records(
    conn: sqlite3.Connection,
    models: ModelEngine,
    data_engine: Any,
    model: ModelDef,
    count: int,
    *,
    seed: int | None = None,
    actor: str = "mock",
) -> dict[str, Any]:
    """Create ``count`` mock records for one model.

    Each record is written in its own savepoint, so a rejected value only drops
    that record and the rest of the batch still lands. Returns a report instead
    of raising, because "generate 200 rows" is a bulk operation.
    """
    if count < 1:
        raise ValidationFailure("生成条数必须大于 0")
    if count > MAX_RECORDS_PER_CALL:
        raise ValidationFailure(f"单次最多生成 {MAX_RECORDS_PER_CALL} 条")
    if not model.id:
        # A model must be persisted by the Model Engine first; otherwise records
        # would be written without a model_id. Fail loudly instead.
        raise ValidationFailure(
            f"模型 '{model.key}' 还没有保存到应用中，无法写入数据（请先 define_model）"
        )

    rng = random.Random(seed)
    context = MockContext(
        rng=rng,
        relations=_relation_candidates(conn, models, model),
        used=_existing_unique_values(conn, model),
        today=dt.date.today(),
    )
    direction = direction_field_of(model)
    income_options = set(direction.option_values) if direction else set()
    income_value = next((value for value in ("income", "in") if value in income_options), None)
    expense_value = next((value for value in ("expense", "out") if value in income_options), None)

    created: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index in range(count):
        # ~15% income keeps expense charts and totals both meaningful.
        is_income = bool(direction) and rng.random() < 0.15
        values: dict[str, Any] = {}
        missing_required = False

        for field_def in model.fields:
            if direction is not None and field_def.key == direction.key:
                values[field_def.key] = income_value if is_income else expense_value
                continue
            value = synthesize_value(field_def, model, context, income=is_income)
            if value is None and field_def.required:
                missing_required = True
                break
            values[field_def.key] = value

        if missing_required:
            failures.append(
                {
                    "index": index,
                    "error": "必填字段没有可用值（关联模型可能还没有数据）",
                }
            )
            continue

        try:
            record = data_engine.create_record(conn, model, values, actor=actor)
            created.append({"id": record["id"], "title": record["title"]})
        except Exception as exc:  # noqa: BLE001 - report and keep generating
            failures.append({"index": index, "error": str(exc)})

    return {
        "model": model.key,
        "requested": count,
        "created": len(created),
        "failed": len(failures),
        "sample": created[:3],
        "errors": failures[:5],
    }


def dependency_order(models: list[ModelDef]) -> list[ModelDef]:
    """Order models so referenced models are generated first (Kahn's algorithm)."""
    by_key = {model.key: model for model in models}
    pending: dict[str, set[str]] = {}
    for model in models:
        targets = {
            field_def.target_model
            for field_def in model.relation_fields()
            if field_def.target_model in by_key and field_def.target_model != model.key
        }
        pending[model.key] = {target for target in targets if target}

    ordered: list[ModelDef] = []
    while pending:
        ready = sorted(key for key, deps in pending.items() if not deps)
        if not ready:  # relation cycle: fall back to declaration order
            ready = sorted(pending)
        for key in ready:
            ordered.append(by_key[key])
            pending.pop(key)
        for deps in pending.values():
            deps.difference_update(ready)
    return ordered


def generate_application_data(
    conn: sqlite3.Connection,
    models: ModelEngine,
    data_engine: Any,
    *,
    count: int,
    seed: int | None = None,
    actor: str = "mock",
) -> dict[str, Any]:
    """Fill an entire application with mock data, in dependency order.

    The "busiest" model (the one with the most fields, i.e. the transaction-like
    fact table) gets the full ``count``. Dimension models that others point at
    get a small, realistic number of rows; anything else gets a medium slice.
    """
    all_models = models.list_models(conn)
    if not all_models:
        raise ValidationFailure("这个应用还没有数据模型")

    ordered = dependency_order(all_models)
    busiest = max(ordered, key=lambda model: (len(model.fields), model.key))
    referenced = {
        field_def.target_model
        for model in all_models
        for field_def in model.relation_fields()
        if field_def.target_model
    }

    report: dict[str, Any] = {"count": count, "models": []}
    for index, model in enumerate(ordered):
        is_dimension = model.key in referenced and model.key != busiest.key
        existing = conn.execute(
            "SELECT COUNT(*) AS total FROM records WHERE model_id = ? AND deleted_at IS NULL",
            (model.id,),
        ).fetchone()["total"]

        if is_dimension:
            # A real app has a handful of categories/accounts, and they are
            # usually seeded rather than bulk generated. Only fill an empty
            # dimension model, otherwise leave the existing rows alone.
            if existing > 0:
                report["models"].append(
                    {
                        "model": model.key,
                        "requested": 0,
                        "created": 0,
                        "failed": 0,
                        "skipped": f"已有 {existing} 条，保持不变",
                        "sample": [],
                        "errors": [],
                    }
                )
                continue
            amount = min(count, DIMENSION_MODEL_RECORDS)
        elif model.key == busiest.key:
            amount = count
        else:
            amount = min(count, OTHER_MODEL_RECORDS)

        if amount < 1:
            continue
        report["models"].append(
            generate_records(
                conn,
                models,
                data_engine,
                model,
                amount,
                seed=None if seed is None else seed + index,
                actor=actor,
            )
        )
    report["created"] = sum(item["created"] for item in report["models"])
    report["failed"] = sum(item["failed"] for item in report["models"])
    return report
