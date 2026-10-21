"""Markdown for the summary comment and the inline comments.

Layout (docs/HOW_IT_WORKS.md): summary, 0-5 confidence
score, file-by-file table, an optional diagram, then a footer with the
reviewed commit. Inline comments carry a severity badge, the comment type,
and a suggestion block GitHub can apply in one click.

Every comment embeds a hidden marker so later runs can find, update,
de-duplicate, and learn from what this reviewer posted.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from simpli_review.agent_prompt import finding_details
from simpli_review.models import BlockDecision, CheckRow, Finding, ReviewResult, Severity

if TYPE_CHECKING:
    from simpli_review.verifier import Verification

SUMMARY_MARKER = "<!-- simpli-review:summary -->"
FINDING_MARKER = "<!-- simpli-review:finding"
_BADGE = {Severity.P0: "🔴 P0", Severity.P1: "🟠 P1", Severity.P2: "🟡 P2"}
_CHECK_RESULT = {"pass": "✅ Pass", "fail": "❌ Fail", "inconclusive": "❔ Inconclusive"}


def finding_marker(f: Finding) -> str:
    meta = {"category": f.category.value, "title": f.title, "path": f.path, "severity": f.severity.value}
    # "<" and ">" are escaped so a title like "a --> b" can't close the HTML comment.
    payload = json.dumps(meta, sort_keys=True).replace("<", r"\u003c").replace(">", r"\u003e")
    return f"{FINDING_MARKER} {payload} -->"


def parse_finding_marker(body: str) -> dict[str, Any]:
    m = re.search(re.escape(FINDING_MARKER) + r"\s*(\{.*?\})\s*-->", body, flags=re.S)
    if not m:
        return {}
    try:
        meta: dict[str, Any] = json.loads(m.group(1))
        return meta
    except json.JSONDecodeError:
        return {}


def inline_body(f: Finding) -> str:
    parts = [f"**{_BADGE[f.severity]} · {f.type.value.title()}** — {f.title}", "", f.body.strip()]
    if f.suggestion is not None:
        parts += ["", "```suggestion", f.suggestion.rstrip("\n"), "```"]
    if f.rule_id:
        parts += ["", f"<sub>Team rule: `{f.rule_id}`</sub>"]
    parts += [
        "",
        f"<details><summary>Evidence</summary>\n\n{f.evidence.strip()}\n\n</details>",
        finding_details(f),
        finding_marker(f),
    ]
    return "\n".join(parts)


def summary_body(
    *,
    name: str,
    result: ReviewResult,
    posted: list[Finding],
    unplaced: list[Finding],
    suppressed: int,
    head_sha: str,
    files_reviewed: int,
    include_diagram: bool,
    review_count: int,
    mention: str,
    since_last: str = "",
    re_review_label: str | None = None,
    checks: list[CheckRow] | None = None,
    block: BlockDecision | None = None,
    agent_prompt: str = "",
    override_label: str | None = None,
    config_changes: list[str] | None = None,
    config_source: str = "",
    verification: Verification | None = None,
) -> str:
    score = result.confidence_score
    lines = [SUMMARY_MARKER, f"## {name}", ""]
    if block and block.reasons:
        lines += [block_notice(block, re_review_label, override_label), ""]
    lines += [
        result.summary.strip(),
        "",
        f"**Confidence: {score}/5** {'🟢' if score >= 4 else '🟡' if score >= 3 else '🔴'} — "
        f"{result.confidence_reason.strip()}",
        "",
    ]
    if since_last:
        lines += [since_last, ""]
    if config_changes:
        lines += [config_changes_notice(config_changes, config_source), ""]
    if checks:
        lines += [checks_table(checks), ""]
    counts = {s: sum(1 for f in posted + unplaced if f.severity == s) for s in Severity}
    if posted or unplaced:
        lines.append("**Issues:** " + ", ".join(f"{_BADGE[s]} x {n}" for s, n in counts.items() if n))
        lines.append("")

    by_path: dict[str, list[Finding]] = {}
    for f in posted + unplaced:
        by_path.setdefault(f.path, []).append(f)
    lines += ["<details><summary>Files changed</summary>", "", "| File | Change | Issues |", "|---|---|---|"]
    for note in result.files:
        issues = ", ".join(_BADGE[f.severity] for f in by_path.get(note.path, [])) or "—"
        lines.append(f"| `{note.path}` | {_cell(note.summary)} | {issues} |")
    lines += ["", "</details>", ""]

    if unplaced:
        lines += ["### Issues outside the changed lines", ""]
        for f in unplaced:
            lines += [f"- **{_BADGE[f.severity]}** `{f.path}:{f.line}` — {f.title}", f"  {_one_line(f.body)}"]
        lines.append("")

    if verification is not None:
        section = verification_section(verification, override_label)
        if section:
            lines += [section, ""]

    if agent_prompt:
        lines += [agent_prompt, ""]

    if include_diagram and result.diagram:
        lines += ["<details><summary>Diagram</summary>", "", "```mermaid", result.diagram.strip(), "```", "",
                  "</details>", ""]  # fmt: skip

    rerun = (
        f"add the `{re_review_label}` label to re-run"
        if re_review_label
        else f"comment `{mention}` to re-run"
    )
    extra = f" · {suppressed} suppressed by team feedback" if suppressed else ""
    lines.append(
        f"<sub>Reviewed `{head_sha[:7]}` · {files_reviewed} files · review #{review_count}{extra} · "
        f"React 👍/👎 on comments to tune future reviews · {rerun}</sub>"
    )
    return "\n".join(lines)


def verification_section(v: Verification, override_label: str | None = None) -> str:
    """What the verifier removed or changed, collapsed, for audit; and a visible note when checks failed.

    Items carry no severity badge and sit inside <details>, so the per-PR memory never reads them back as
    earlier findings (memory.summary_findings only reads the 'Issues outside the changed lines' list).
    """
    dropped, lowered, unverified = (
        v.by_status("dropped"),
        v.by_status("downgraded"),
        v.by_status("unverified"),
    )
    out: list[str] = []
    disputed = v.by_status("disputed")
    if disputed:
        n = len(disputed)
        label = f"the `{_tick(override_label)}` label" if override_label else "the override label"
        they, blocks = ("They", "block") if n != 1 else ("It", "blocks")
        out += [
            f"**Disputed:** the second check disputes {n} finding{'s' if n != 1 else ''} at the blocking "
            f"severity. {they} still {blocks} the merge; add {label} if you agree with the second check.",
            "",
        ]
        for c in disputed:
            where = f"`{_tick(c.original.path)}:{c.original.line}`"
            out.append(f"- Disputed {c.original.severity.value}: {where} — {_one_line(c.original.title)}. "
                       f"Second check: {_one_line(c.reason)}")  # fmt: skip
        out.append("")
    if unverified:
        n = len(unverified)
        plural = n != 1
        out.append(
            f"⚠️ {n} finding{'s' if plural else ''} could not be verified and "
            f"{'were' if plural else 'was'} kept as the reviewer wrote {'them' if plural else 'it'}."
        )
    if dropped or lowered:
        if out:
            out.append("")
        n = len(dropped)
        summary = f"{n} candidate{'s' if n != 1 else ''} dropped by the verifier"
        if lowered:
            summary += f", {len(lowered)} downgraded"
        out += [f"<details><summary>{summary}</summary>", ""]
        for c in dropped:
            where = f"`{_tick(c.original.path)}:{c.original.line}`"
            out.append(f"- Dropped: {where} — {_one_line(c.original.title)}. Reason: {_one_line(c.reason)}")
        for c in lowered:
            where = f"`{_tick(c.original.path)}:{c.original.line}`"
            change = f"{c.original.severity.value} to {c.finding.severity.value if c.finding else '?'}"
            title, reason = _one_line(c.original.title), _one_line(c.reason)
            out.append(f"- Downgraded {change}: {where} — {title}. Reason: {reason}")
        if v.summary == "rewritten":
            out += ["", "The summary and confidence score above were updated after these changes."]
        out += ["", "</details>"]
    return "\n".join(out).strip("\n")


def _tick(text: str) -> str:
    return text.replace("`", "")


def checks_table(rows: list[CheckRow]) -> str:
    lines = ["### Pre-merge checks", "", "| Check | Result | Mode | Detail |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{_cell(r.name)}` | {_CHECK_RESULT[r.result]} | {r.mode} | {_cell(r.reason)} |")
    return "\n".join(lines)


def config_changes_notice(paths: list[str], source: str) -> str:
    """This PR edits files that steer reviews. They are reviewed like any change but not applied to it."""
    names = ", ".join(f"`{p.replace('`', '')}`" for p in paths)
    where = f" (`{source.replace('`', '')}`)" if source else ""
    return "\n".join([
        "### Configuration changes in this PR", "",
        f"This PR changes files that control how reviews run: {names}. The changes apply after the PR is "
        "merged. They were not used for this review: the settings, rules and context files came from "
        f"the base branch{where}.",
    ])  # fmt: skip


def block_notice(
    block: BlockDecision, re_review_label: str | None = None, override_label: str | None = None
) -> str:
    """Why this review fails the check, or that a label skipped the block."""
    reasons = [f"- {_one_line(r)}" for r in block.reasons]
    if block.overridden_by:
        return "\n".join([
            "### Merge block overridden", "",
            f"The `{block.overridden_by}` label is on this PR, so this check passes. "
            "Without the label this review would block the merge because:", "", *reasons,
        ])  # fmt: skip
    steps = "Fix these"
    if re_review_label:
        steps += f", then add the `{re_review_label}` label to review again"
    steps += "."
    if override_label:
        steps += f" A maintainer can add the `{override_label}` label to skip this block."
    return "\n".join([
        "### ⛔ Merge blocked", "",
        "This review fails the check, so the PR cannot merge while the check is required. Reasons:", "",
        *reasons, "", steps,
    ])  # fmt: skip


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


def _one_line(text: str) -> str:
    return " ".join(text.split())
