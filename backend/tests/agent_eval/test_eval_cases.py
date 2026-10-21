"""The eval set runs in CI: no keys, no network, deterministic.

These cases exercise the same engines and tools as a real model would; the
``llm``-only cases (arbitrary domains, evolution, vague requests) are skipped
here and run with ``uv run python -m zooagent.eval --provider llm``.
"""

from __future__ import annotations

from zooagent.eval.cases import default_cases_path, load_cases
from zooagent.eval.runner import run_cases

#: providers that need no API key
CI_PROVIDERS = ["offline", "mock"]


def test_case_file_is_valid() -> None:
    cases = load_cases(default_cases_path())
    assert cases, "用例文件不应为空"
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))
    # the design document's five domains must all be represented
    domains = {case.domain for case in cases}
    for domain in ("家庭记账", "客户管理", "简单库存", "个人物品管理", "学习计划与进度"):
        assert domain in domains, f"缺少设计文档要求的领域: {domain}"
    # every case must state clear expectations
    for case in cases:
        assert case.turns, f"{case.id} 没有 turns"
        assert case.providers, f"{case.id} 没有 providers"
        assert not case.expect.is_empty() or case.turns[0].expect, f"{case.id} 没有任何期望"


def test_ci_safe_cases_pass_offline_and_mock() -> None:
    """offline + mock providers must satisfy their structural expectations."""
    cases = [
        case
        for case in load_cases(default_cases_path())
        if set(case.providers) & set(CI_PROVIDERS)
    ]
    report = run_cases(cases, cases_path=str(default_cases_path()))

    ran = [item for item in report.results if item.status != "skipped"]
    assert ran, "没有任何用例被执行"
    failures = [
        f"{item.case_id}[{item.provider_kind}]: "
        + "；".join(f"{check.name}({check.reason})" for check in item.failures)
        for item in report.failed
    ]
    assert not failures, "评测失败:\n" + "\n".join(failures)
    # the primary assistant owns structure only: even when the prompt asks for
    # 150 test rows, the created app stays empty (demo data is the app
    # assistant's job, covered by the AgentScope runtime tests).
    mock_case = next(
        item for item in report.results if item.case_id == "mock-data-generation"
    )
    assert mock_case.metrics["records"] == 0
