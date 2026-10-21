"""A second opinion on each serious finding before it is posted (the `verify` config block).

The reviewer works recall-first: it is told to report what it confirmed, but it judges its own work in a
long conversation. Here every candidate at a configured severity goes to a fresh, short agent run that sees
only that finding, the diff of its file and the read-only tools. It returns keep, downgrade (one severity
step) or drop, with a 1-5 confidence and a one-line reason. A verdict below `minConfidence` drops the
finding. With `blockOn` set, a finding at or above it is never dropped or lowered below it: the verdict
marks it disputed and it keeps blocking until a person adds the override label.

Failure never removes a finding: if a verification errors, hits its turn or token cap, or runs out of time,
the finding is kept as it was and counted as unverified. Dropped findings are listed in the summary for
audit; they are never posted inline, so they never reach the learned feedback or the per-PR memory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

from simpli_review.config import Config, VerifyConfig
from simpli_review.diff import FilePatch
from simpli_review.models import Finding, ReviewResult, Severity
from simpli_review.reviewer import (
    CONFIDENCE_GUIDE,
    SEVERITY_GUIDE,
    LoopState,
    ReviewFailed,
    Settings,
    agent_loop,
)
from simpli_review.tools import TOOLS, Toolbox

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 8_000
MAX_DIFF_CHARS = 40_000
MAX_REASON_CHARS = 300
RESPONSE_MAX_TOKENS = 32_000
SUBMIT = "submit_verdict"
_now = time.monotonic  # replaceable in tests

VERIFY_SYSTEM = f"""\
You are checking one finding that an automated code reviewer wants to post on a pull request. You did not \
write it. Decide whether it is correct before a person spends time on it.

You get the repository's own context (team instructions, rules and convention files, taken from the base \
branch), the finding, the diff of the file it is on, and read-only tools over the whole repository at the \
pull request's head commit. The repository context is authoritative: the pull request may have changed the \
copies of those files in the working tree.

How to work:
- Work out the concrete failure the finding claims: which input or sequence of events, what goes wrong, \
and where.
- Read the code involved, not only the diff: the changed code, its callers, what it calls, and anything \
that could already prevent the failure (checks earlier on the path, callers, configuration, constraints, \
or the framework).
- Try to show the finding is wrong. Keep it only if the code you read supports it.
- Judge the finding as written. Do not look for other problems and do not rewrite it.

{SEVERITY_GUIDE}

Verdicts:
- keep: the problem is real and the severity fits.
- downgrade: the problem is real but belongs one severity level lower.
- drop: the problem does not happen, is already handled, or is neither caused nor made reachable by this \
change.

confidence (1-5) is how sure you are that the problem, as kept or downgraded, is real: 5 = you traced the \
failing path in the code; 3 = likely, but you could not check every step; 1 = you found that it does not \
happen. A drop normally has confidence 1 or 2. If you run out of room before you can settle it, keep it with \
confidence 3 and say what you could not check.

reason: one line for the audit log: what you found in the code that decides it, with a file:line.

Finish by calling {SUBMIT} exactly once. Do not answer in plain text."""


class Verdict(BaseModel):
    verdict: Literal["keep", "downgrade", "drop"]
    confidence: int = Field(ge=1, le=5, description="1-5: how sure you are the problem, as kept, is real.")
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="One line: what in the code decides it, with a file:line.",
    )


VERIFY_TOOLS: list[dict[str, Any]] = [t for t in TOOLS if t["name"] != "submit_review"] + [
    {
        "name": SUBMIT,
        "description": "Submit your verdict on the finding. Call exactly once, as your final action.",
        "input_schema": Verdict.model_json_schema(),
    }
]

# disputed: the verifier would drop or lower a finding at or above `blockOn`; it stays as it was (see below).
Status = Literal["kept", "downgraded", "dropped", "disputed", "unverified"]
STATUSES: tuple[Status, ...] = ("kept", "downgraded", "dropped", "disputed", "unverified")
_RANK = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}
_LOWER = {Severity.P0: Severity.P1, Severity.P1: Severity.P2}


@dataclass
class Checked:
    """One verification: the finding as the reviewer wrote it and what the verifier made of it."""

    original: Finding
    status: Status
    finding: Finding | None  # what goes on to be posted; None when dropped
    confidence: int | None = None  # None when unverified
    reason: str = ""  # the verifier's one line; for an unverified finding, what went wrong (no model text)
    usage: dict[str, int] = field(default_factory=dict)
    turns: int = 0


@dataclass
class Verification:
    model: str
    checked: list[Checked] = field(default_factory=list)
    # The summary and confidence score after the pass: "rewritten" by a short model call, "noted" (that
    # call failed, so a note says they predate the pass), or "unchanged" (nothing was dropped or downgraded).
    summary: Literal["unchanged", "rewritten", "noted"] = "unchanged"
    summary_usage: dict[str, int] = field(default_factory=dict)

    def by_status(self, status: Status) -> list[Checked]:
        return [c for c in self.checked if c.status == status]

    @property
    def changed(self) -> bool:
        return bool(self.by_status("dropped") or self.by_status("downgraded"))

    @property
    def usage(self) -> dict[str, int]:
        total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        for u in [*(c.usage for c in self.checked), self.summary_usage]:
            for k, v in u.items():
                total[k] = total.get(k, 0) + v
        return total

    def counts(self) -> dict[str, int]:
        return {s: len(self.by_status(s)) for s in STATUSES}


class _TimedOut(ReviewFailed):
    pass


def apply_verdict(
    f: Finding, v: Verdict, min_confidence: int, block_on: str = "none"
) -> tuple[Status, Finding | None]:
    """What a verdict does to a finding. A downgrade of the lowest severity leaves it as it is.

    The verifier reads code the pull request controls, so it must not be able to unblock a merge: with
    `blockOn` set, a finding at or above it is never dropped or lowered below it. Such a verdict marks the
    finding "disputed" and leaves it as it was; only a person with the override label can waive it.
    """
    blocking = block_on in ("P0", "P1") and _RANK[f.severity] <= _RANK[Severity(block_on)]
    if v.verdict == "drop" or v.confidence < min_confidence:
        return ("disputed", f) if blocking else ("dropped", None)
    if v.verdict == "downgrade" and f.severity in _LOWER:
        lower = _LOWER[f.severity]
        if blocking and _RANK[lower] > _RANK[Severity(block_on)]:
            return "disputed", f
        return "downgraded", f.model_copy(update={"severity": lower})
    return "kept", f


def disputed_note(reason: str, override_label: str) -> str:
    """Appended to a disputed finding's body, so the inline comment and the summary carry it."""
    label = override_label.replace("`", "")
    return f"Second check disputes this: {reason} Add the `{label}` label if you agree."


def build_prompt(
    f: Finding, patch: FilePatch | None, config: Config, context: str = ""
) -> list[dict[str, Any]]:
    """The verifier's only context: the repository context the reviewer got (read from the base commit, so a
    pull request cannot rewrite the conventions it is judged by), the finding and the diff of its file. The
    context block comes first and is the same for every finding of one review."""
    lines = f"{f.line}-{f.end_line}" if f.end_line and f.end_line > f.line else str(f.line)
    body = f.body.strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n... (truncated)"
    parts = [
        "## Finding to check",
        f"Severity: {f.severity.value}\nFile: {f.path}, line {lines}\nTitle: {f.title.strip()}\n\n{body}",
    ]
    rule = next((r for r in config.rules if f.rule_id and r.id == f.rule_id and r.enabled), None)
    if rule:
        parts.append(f"## Team rule the finding enforces\n[{rule.id}] ({rule.severity}) {rule.rule}")
    if patch is not None and patch.patch:
        diff = patch.annotated()
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated; read the file with read_file)"
        parts.append(f"## Diff of {f.path} (left gutter = new-file line number)\n{diff}")
    else:
        parts.append(
            f"## Diff of {f.path}\n(the pull request does not change this file; read it with read_file)"
        )
    blocks = [{"type": "text", "text": context}] if context.strip() else []
    return [*blocks, {"type": "text", "text": "\n\n".join(parts)}]


def verify_one(
    client: anthropic.Anthropic,
    settings: Settings,
    f: Finding,
    patch: FilePatch | None,
    config: Config,
    toolbox: Toolbox,
    context: str = "",
) -> Checked:
    """One verification. Never raises: any failure keeps the finding, unverified."""
    cfg = config.verify
    state = LoopState()
    deadline = _now() + cfg.timeout_seconds

    def on_turn(_turn: int) -> None:
        if _now() > deadline:
            raise _TimedOut(f"verification ran past {cfg.timeout_seconds}s")

    with_options = getattr(client, "with_options", None)
    c = with_options(timeout=float(cfg.timeout_seconds)) if callable(with_options) else client
    try:
        verdict = agent_loop(
            c, settings, system=VERIFY_SYSTEM, tools=VERIFY_TOOLS, submit_name=SUBMIT,
            parse=Verdict.model_validate, user_content=build_prompt(f, patch, config, context),
            toolbox=toolbox, state=state, on_turn=on_turn,
        )  # fmt: skip
    except Exception as e:  # never drop a finding because its check failed
        why = "timed out" if isinstance(e, _TimedOut) else type(e).__name__
        log.warning("a verification did not finish (%s); the finding is kept, unverified", why)
        return Checked(f, "unverified", f, reason=why, usage=dict(state.usage), turns=state.turns)
    status, kept = apply_verdict(f, verdict, cfg.min_confidence, config.block_on)
    reason = " ".join(verdict.reason.split())[:MAX_REASON_CHARS]
    if status == "disputed" and kept is not None:
        note = disputed_note(reason if reason.endswith(".") else reason + ".", config.override_label)
        kept = kept.model_copy(update={"body": kept.body.rstrip() + "\n\n" + note})
    return Checked(f, status, kept, verdict.confidence, reason, dict(state.usage), state.turns)


def verifier_settings(base: Settings, cfg: VerifyConfig) -> Settings:
    return Settings(
        model=cfg.model,
        effort=cfg.effort,
        max_turns=cfg.max_turns,
        max_tokens=RESPONSE_MAX_TOKENS,
        use_fallbacks=base.use_fallbacks,
        budget_tokens=cfg.max_tokens,
    )


def verify_findings(
    client: anthropic.Anthropic,
    base: Settings,
    findings: list[Finding],
    *,
    patches: list[FilePatch],
    config: Config,
    make_toolbox: Callable[[], Toolbox],
    should_verify: Callable[[Finding], bool] = lambda _f: True,
    context: str = "",
) -> tuple[list[Finding], Verification]:
    """Verify the findings at the configured severities that `should_verify` lets through, at most
    `concurrency` at a time. Returns the findings to carry on with (same order, drops removed, downgrades
    applied; everything not verified passes through unchanged) and the record of the pass."""
    cfg = config.verify
    targets = [i for i, f in enumerate(findings) if f.severity.value in cfg.severities and should_verify(f)]
    by_path = {p.path: p for p in patches}
    settings = verifier_settings(base, cfg)

    def one(i: int) -> Checked:
        f = findings[i]
        return verify_one(client, settings, f, by_path.get(f.path), config, make_toolbox(), context)

    done: dict[int, Checked] = {}
    if targets:
        with ThreadPoolExecutor(
            max_workers=min(cfg.concurrency, len(targets)), thread_name_prefix="verify"
        ) as ex:
            done = dict(zip(targets, ex.map(one, targets), strict=True))
    out: list[Finding] = []
    for i, f in enumerate(findings):
        c = done.get(i)
        if c is None:
            out.append(f)
        elif c.finding is not None:
            out.append(c.finding)
    verification = Verification(cfg.model, [done[i] for i in targets])
    n = verification.counts()
    log.info(
        "verifier: %d checked, %d kept, %d downgraded, %d dropped, %d unverified",
        len(targets), n["kept"], n["downgraded"], n["dropped"], n["unverified"],
    )  # fmt: skip
    return out, verification


# ---------- the summary after the pass ----------

SUMMARY_SUBMIT = "submit_summary"
SUMMARY_MAX_TURNS = 3

SUMMARY_SYSTEM = f"""\
You update the summary of a pull request review. The reviewer wrote the summary, the confidence score and \
its reason before a second check of its findings. That check dropped some findings as wrong and lowered the \
severity of others. Make the three fields match the findings that remain.

- Keep every statement that is still true, in the reviewer's words. Remove or correct statements that rest \
on a dropped finding, and describe a lowered finding at its new severity.
- Do not add new problems, new claims about the code or praise.
- Set the score with the rubric below, from the findings that remain and the risk of the area changed.

{CONFIDENCE_GUIDE}

Call {SUMMARY_SUBMIT} exactly once with the updated summary, confidence_score and confidence_reason."""


class SummaryUpdate(BaseModel):
    summary: str = Field(min_length=1, max_length=8000)
    confidence_score: int = Field(ge=0, le=5)
    confidence_reason: str = Field(min_length=1, max_length=2000)


SUMMARY_TOOLS: list[dict[str, Any]] = [
    {
        "name": SUMMARY_SUBMIT,
        "description": "Submit the updated summary. Call exactly once.",
        "input_schema": SummaryUpdate.model_json_schema(),
    }
]


def _summary_prompt(result: ReviewResult, v: Verification) -> list[dict[str, Any]]:
    remaining = [f"- {f.severity.value} `{f.path}:{f.line}` {_line(f.title)}" for f in result.findings]
    changes = [f"- dropped: {_line(c.original.title)} (reason: {c.reason})" for c in v.by_status("dropped")]
    changes += [
        f"- lowered {c.original.severity.value} to {c.finding.severity.value if c.finding else '?'}: "
        f"{_line(c.original.title)} (reason: {c.reason})"
        for c in v.by_status("downgraded")
    ]
    text = "\n\n".join([
        "## The reviewer's summary\n" + result.summary.strip(),
        f"## The reviewer's confidence score\n{result.confidence_score}/5: "
        + result.confidence_reason.strip(),
        "## What the second check changed\n" + "\n".join(changes),
        "## Findings that remain\n" + ("\n".join(remaining) or "(none)"),
    ])  # fmt: skip
    return [{"type": "text", "text": text}]


def _line(text: str) -> str:
    return " ".join(text.split())


def reconcile_summary(
    client: anthropic.Anthropic,
    base: Settings,
    result: ReviewResult,
    v: Verification,
    toolbox: Toolbox,
) -> ReviewResult:
    """The review with its summary, confidence score and reason brought in line with the verifier's results.

    `result.findings` must already be the findings after the pass. Nothing changes when nothing was dropped
    or downgraded. If the rewrite fails, the reviewer's text stays and the reason says it predates the pass;
    the failure is logged and recorded in `v.summary`, never hidden.
    """
    if not v.changed:
        return result
    cfg_settings = Settings(
        model=v.model, effort="low", max_turns=SUMMARY_MAX_TURNS, max_tokens=RESPONSE_MAX_TOKENS,
        use_fallbacks=base.use_fallbacks,
    )  # fmt: skip
    state = LoopState()
    try:
        update = agent_loop(
            client, cfg_settings, system=SUMMARY_SYSTEM, tools=SUMMARY_TOOLS, submit_name=SUMMARY_SUBMIT,
            parse=SummaryUpdate.model_validate, user_content=_summary_prompt(result, v), toolbox=toolbox,
            state=state,
        )  # fmt: skip
    except Exception as e:  # the review stands; say plainly that its summary predates the pass
        log.warning("could not update the summary after verification (%s)", type(e).__name__)
        v.summary, v.summary_usage = "noted", dict(state.usage)
        n_drop, n_low = len(v.by_status("dropped")), len(v.by_status("downgraded"))
        note = (
            f" (Written before verification, which then dropped {n_drop} and downgraded {n_low} "
            "finding(s); see the verifier section below.)"
        )
        return result.model_copy(update={"confidence_reason": result.confidence_reason.rstrip() + note})
    v.summary, v.summary_usage = "rewritten", dict(state.usage)
    return result.model_copy(update=update.model_dump())


def verification_json(v: Verification | None) -> dict[str, Any] | None:
    """For `local --json`: every verified finding as the reviewer wrote it, and the outcome."""
    if v is None:
        return None
    return {
        "model": v.model,
        "usage": v.usage,
        "counts": v.counts(),
        "summary": v.summary,
        "items": [
            {
                "status": c.status,
                "path": c.original.path,
                "line": c.original.line,
                "end_line": c.original.end_line,
                "title": c.original.title,
                "body": c.original.body,
                "original_severity": c.original.severity.value,
                "severity": c.finding.severity.value if c.finding else None,
                "confidence": c.confidence,
                "reason": c.reason,
                "turns": c.turns,
            }
            for c in v.checked
        ],
    }
