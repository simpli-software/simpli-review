"""The machine-readable result of `simpli-review local --json`.

One JSON object on stdout, so an agent can review its own branch before opening a PR and act on
the result: the summary, every finding (with a ready-made `agent_prompt`), the pre-merge checks,
and whether the review would block the merge, with the reasons.
"""

from __future__ import annotations

from typing import Any

from simpli_review.agent_prompt import AgentTask, combined_text, task_text
from simpli_review.models import BlockDecision, CheckRow, Finding, ReviewResult


def finding_json(f: Finding, placement: str) -> dict[str, Any]:
    return {
        "severity": f.severity.value,
        "type": f.type.value,
        "category": f.category.value,
        "path": f.path,
        "line": f.line,
        "end_line": f.end_line,
        "title": f.title,
        "body": f.body,
        "suggestion": f.suggestion,
        "confidence": f.confidence,
        "placement": placement,  # "inline" (on a changed line) or "summary" (outside the diff)
        "agent_prompt": task_text(AgentTask.from_finding(f)),
    }


def build_report(
    *,
    result: ReviewResult,
    posted: list[Finding],
    unplaced: list[Finding],
    checks: list[CheckRow],
    decision: BlockDecision,
    tasks: list[AgentTask],
    config_source: str = "",
    config_changes: list[str] | None = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "summary": result.summary,
        "confidence_score": result.confidence_score,
        "confidence_reason": result.confidence_reason,
        "findings": [finding_json(f, "inline") for f in posted]
        + [finding_json(f, "summary") for f in unplaced],
        "checks": [{"name": c.name, "result": c.result, "mode": c.mode, "reason": c.reason} for c in checks],
        "blocking": decision.blocking,
        "reasons": decision.reasons,
        "agent_prompt": combined_text(tasks) if tasks else "",
        # Where the settings, rules and context files came from (the base ref, never your working tree),
        # and which of those files this branch edits (they apply after merge, not to this review).
        "config_source": config_source,
        "config_changes": config_changes or [],
        # The verifier pass (`verify` config), or null when it did not run: every finding it checked as the
        # reviewer wrote it, the outcome (kept, downgraded, dropped, unverified) and its tokens.
        "verification": verification,
    }
