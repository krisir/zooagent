"""Run evaluation cases and report results.

Each case runs in its own throwaway workspace, so cases never interfere and a
failing case can be re-run in isolation. Snapshots are taken from the engines,
so what is scored is the application the platform actually built.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.llm.base import ChatMessage
from ..config import ModelProfile
from ..errors import ZooAgentError
from ..state import AppState
from .cases import LLM_PROVIDERS, Case
from .checks import CheckResult, evaluate, evaluate_evolution
from .snapshot import AppSnapshot, TurnResult, capture_snapshot

#: tool result events carry no payload in the AgentScope path, so failures are
#: counted from ok flags only - which is all the report needs.
MAX_TOOL_TRACE = 200


@dataclass
class CaseResult:
    case_id: str
    domain: str
    provider_kind: str
    profile_id: str
    status: str  # passed | failed | skipped | error
    checks: list[CheckResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    snapshot: AppSnapshot | None = None
    message: str = ""

    @property
    def failures(self) -> list[CheckResult]:
        return [item for item in self.checks if not item.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            "domain": self.domain,
            "provider": self.provider_kind,
            "profile": self.profile_id,
            "status": self.status,
            "message": self.message,
            "checks": [item.to_dict() for item in self.checks],
            "metrics": self.metrics,
            "application": self.snapshot.to_dict() if self.snapshot else None,
        }


@dataclass
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)
    started_at: str = ""
    cases_path: str = ""
    profile_override: str | None = None

    @property
    def passed(self) -> list[CaseResult]:
        return [item for item in self.results if item.status == "passed"]

    @property
    def failed(self) -> list[CaseResult]:
        return [item for item in self.results if item.status in {"failed", "error"}]

    @property
    def skipped(self) -> list[CaseResult]:
        return [item for item in self.results if item.status == "skipped"]

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "cases_path": self.cases_path,
            "profile_override": self.profile_override,
            "summary": {
                "total": len(self.results),
                "passed": len(self.passed),
                "failed": len(self.failed),
                "skipped": len(self.skipped),
            },
            "results": [item.to_dict() for item in self.results],
        }

    def render(self) -> str:
        lines: list[str] = []
        header = (
            f"{'case':<34}{'provider':<10}{'status':<9}{'models':>7}{'fields':>7}"
            f"{'rel':>5}{'act':>5}{'pages':>6}{'rec':>6}  note"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for item in self.results:
            metrics = item.metrics or {}
            note = item.message or "；".join(
                failure.reason for failure in item.failures[:2]
            )
            lines.append(
                f"{item.case_id[:33]:<34}{item.provider_kind:<10}{item.status:<9}"
                f"{metrics.get('models', '-'):>7}{metrics.get('fields', '-'):>7}"
                f"{metrics.get('relations', '-'):>5}{metrics.get('actions', '-'):>5}"
                f"{metrics.get('pages', '-'):>6}{metrics.get('records', '-'):>6}  {note[:70]}"
            )
        lines.append("")
        lines.append(
            f"共 {len(self.results)} 个用例：通过 {len(self.passed)}，"
            f"失败 {len(self.failed)}，跳过 {len(self.skipped)}"
        )
        if self.failed:
            lines.append("")
            lines.append("失败详情：")
            for item in self.failed:
                lines.append(f"  - {item.case_id} [{item.provider_kind}]: {item.message}")
                for check in item.failures:
                    lines.append(f"      · {check.name}: {check.reason}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# profile selection
# ----------------------------------------------------------------------
def pick_profile(
    profiles: list[ModelProfile], provider_kind: str, override: str | None
) -> ModelProfile | None:
    if override:
        return next(
            (
                profile
                for profile in profiles
                if profile.id == override or override.lower() in profile.name.lower()
            ),
            None,
        )
    if provider_kind == "offline":
        return next((item for item in profiles if item.provider == "offline"), None)
    if provider_kind == "mock":
        return next((item for item in profiles if item.provider == "mock"), None)
    return next(
        (item for item in profiles if item.provider in LLM_PROVIDERS and item.api_key),
        None,
    )


# ----------------------------------------------------------------------
# running one case
# ----------------------------------------------------------------------
async def _run_turn(state: AppState, prompt: str, history: list[ChatMessage]) -> TurnResult:
    """Send one message to the primary assistant and collect what it did."""
    prepared = state.agents.primary()
    turn = TurnResult(prompt=prompt)
    started = time.time()
    tool_calls: list[dict[str, Any]] = []

    try:
        async for event in prepared.runtime.stream(history=history, user_text=prompt):
            kind = event.get("type")
            if kind == "tool_call":
                tool_calls.append(
                    {
                        "name": event.get("name"),
                        "arguments": event.get("arguments") or {},
                    }
                )
            elif kind == "tool_result":
                if not event.get("ok"):
                    turn.tool_failures.append(
                        {
                            "name": event.get("name"),
                            "error": (event.get("error") or {}).get("message")
                            if isinstance(event.get("error"), dict)
                            else event.get("error"),
                        }
                    )
            elif kind == "assistant_message":
                turn.final_text = event.get("content") or ""
            elif kind == "error":
                turn.error = event.get("message") or "未知错误"
    except ZooAgentError as exc:
        turn.error = exc.message
    except Exception as exc:  # pragma: no cover - provider surprises
        turn.error = f"{type(exc).__name__}: {exc}"

    turn.tool_calls = tool_calls[:MAX_TOOL_TRACE]
    turn.duration_s = time.time() - started

    # feed the turn back as history (text only, matching the stored format)
    history.append(ChatMessage(role="user", content=prompt))
    if turn.final_text:
        history.append(ChatMessage(role="assistant", content=turn.final_text))
    return turn


def run_case(
    case: Case,
    *,
    profile: ModelProfile,
    provider_kind: str,
    workspace_root: Path | None = None,
    keep: bool = False,
) -> CaseResult:
    directory = Path(
        workspace_root or tempfile.mkdtemp(prefix=f"zooagent-eval-{case.id}-")
    )
    directory.mkdir(parents=True, exist_ok=True)
    state = AppState.create(directory, token="eval-token")
    settings = state.settings.load()
    profiles = [*settings.model_profiles, profile]
    settings.model_profiles = profiles
    settings.active_model_profile_id = profile.id
    state.settings.save(settings)

    result = CaseResult(
        case_id=case.id,
        domain=case.domain,
        provider_kind=provider_kind,
        profile_id=profile.id,
        status="passed",
    )

    history: list[ChatMessage] = []
    snapshots: list[AppSnapshot] = []
    turns: list[TurnResult] = []
    try:
        for turn_spec in case.turns:
            turn = asyncio.run(_run_turn(state, turn_spec.prompt, history))
            turns.append(turn)
            applications = state.service.list_applications()
            if applications:
                snapshot = capture_snapshot(state.service, applications[0].id, turns=list(turns))
                snapshots.append(snapshot)
                if turn_spec.expect is not None:
                    outcome = evaluate(
                        Case(
                            id=case.id,
                            domain=case.domain,
                            turns=[turn_spec],
                            providers=case.providers,
                            expect=turn_spec.expect,
                        ),
                        snapshot,
                    )
                    result.checks.extend(outcome.checks)

        final_snapshot = snapshots[-1] if snapshots else None
        if final_snapshot is None:
            result.snapshot = None
            result.metrics = {}
            result.checks.append(
                CheckResult("application_created", False, "整轮对话没有创建任何应用")
            )
        else:
            result.snapshot = final_snapshot
            result.metrics = final_snapshot.metrics()
            outcome = evaluate(case, final_snapshot)
            result.checks.extend(outcome.checks)
            if case.evolution is not None and len(snapshots) > 1:
                evolution = evaluate_evolution(case, snapshots[0], final_snapshot)
                result.checks.extend(evolution.checks)

        if any(turn.error for turn in turns):
            result.checks.append(
                CheckResult(
                    "no_runtime_error",
                    False,
                    "；".join(f"{turn.prompt[:20]}: {turn.error}" for turn in turns if turn.error),
                )
            )
    except Exception as exc:  # pragma: no cover - harness-level failure
        result.status = "error"
        result.message = f"{type(exc).__name__}: {exc}"
    finally:
        result.checks = _dedupe(result.checks)
        if result.status != "error":
            result.status = "passed" if all(item.passed for item in result.checks) else "failed"
        if not keep:
            shutil.rmtree(directory, ignore_errors=True)
    return result


def _dedupe(checks: list[CheckResult]) -> list[CheckResult]:
    seen: set[str] = set()
    unique: list[CheckResult] = []
    for check in checks:
        key = f"{check.name}|{check.reason}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(check)
    return unique


def run_cases(
    cases: list[Case],
    *,
    profile_override: str | None = None,
    only: list[str] | None = None,
    tags: list[str] | None = None,
    cases_path: str = "",
    keep: bool = False,
    profiles_workspace: Path | None = None,
) -> EvalReport:
    """Run every (case, provider) pair that has a usable profile.

    ``profiles_workspace`` lets the caller borrow the *configured* model
    profiles (including API keys) from a real workspace - that is how the
    ``llm`` cases get a model to talk to.
    """
    report = EvalReport(
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        cases_path=cases_path,
        profile_override=profile_override,
    )
    probe_root = profiles_workspace or tempfile.mkdtemp(prefix="zooagent-eval-probe-")
    probe = AppState.create(probe_root, token="eval-token")
    profiles = probe.settings.load().model_profiles
    if profiles_workspace is None:
        shutil.rmtree(probe.paths.root, ignore_errors=True)

    for case in cases:
        if only and case.id not in only:
            continue
        if tags and not (set(case.tags) & set(tags)):
            continue
        for provider_kind in case.providers:
            profile = pick_profile(profiles, provider_kind, profile_override)
            if profile is None:
                report.results.append(
                    CaseResult(
                        case_id=case.id,
                        domain=case.domain,
                        provider_kind=provider_kind,
                        profile_id="",
                        status="skipped",
                        message=(
                            "没有配置可用的真实模型（设置里添加模型或传 --profile）"
                            if provider_kind == "llm"
                            else f"没有 {provider_kind} 配置"
                        ),
                    )
                )
                continue
            report.results.append(
                run_case(case, profile=profile, provider_kind=provider_kind, keep=keep)
            )
    return report


def compare_reports(current: EvalReport, baseline: dict[str, Any]) -> list[str]:
    """Metric deltas + status changes versus a previous ``--json`` run."""
    lines: list[str] = []
    previous = {
        (item["case"], item["provider"]): item for item in baseline.get("results", [])
    }
    for result in current.results:
        key = (result.case_id, result.provider_kind)
        before = previous.get(key)
        if before is None:
            lines.append(f"新增用例 {result.case_id} [{result.provider_kind}] -> {result.status}")
            continue
        if before["status"] != result.status:
            lines.append(
                f"{result.case_id} [{result.provider_kind}] 状态变化: {before['status']} -> {result.status}"
            )
        for name, value in (result.metrics or {}).items():
            old = (before.get("metrics") or {}).get(name)
            if isinstance(value, (int, float)) and isinstance(old, (int, float)) and value != old:
                lines.append(
                    f"{result.case_id} [{result.provider_kind}] {name}: {old} -> {value}"
                )
    return lines


__all__ = [
    "CaseResult",
    "EvalReport",
    "compare_reports",
    "pick_profile",
    "run_case",
    "run_cases",
]
