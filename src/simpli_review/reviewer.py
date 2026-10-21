"""The review itself: an agent loop over the code graph, then filtering.

A fixed retrieval pipeline is replaced by an agent loop with codebase tools,
and the model must challenge its own hypotheses before it comments
(docs/HOW_IT_WORKS.md). This module does that:

1. Seed the conversation with the PR, the annotated diff, and graph context
   for every changed function (callers, callees, similar functions,
   importers), plus the team's rules, context files and past feedback.
2. Let Claude investigate with read-only tools until it calls `submit_review`.
3. Keep only findings that land on lines GitHub can comment on, pass the
   strictness/type/confidence filters, aren't suppressed by team feedback,
   and aren't already posted on this PR.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anthropic
from pydantic import ValidationError

from simpli_review.checks import prompt_section as checks_prompt
from simpli_review.config import Config
from simpli_review.config_source import ConfigSourceError, DirSource, FileSource
from simpli_review.diff import FilePatch, nearest_commentable
from simpli_review.index import RepoIndex, Symbol
from simpli_review.learning import Feedback, similarity, words
from simpli_review.memory import PriorFinding, prompt_section
from simpli_review.models import CheckRow, Finding, ReviewResult, Severity
from simpli_review.tools import TOOLS, Toolbox, ToolError

if TYPE_CHECKING:
    from simpli_review.verifier import Verification

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 250_000
MAX_CONTEXT_FILE_CHARS = 20_000
MAX_GRAPH_SYMBOLS = 25
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SEVERITY_GUIDE = (
    "Severity: P0 = must not merge (security hole, data loss, crash on a main path). P1 = should be fixed "
    "before merge (a real bug or a regression). P2 = worth fixing (edge case, robustness, clarity that "
    "prevents a likely mistake)."
)

CONFIDENCE_GUIDE = (
    "Confidence score (0-5) for the whole PR: 5 = safe to merge; lower it for unresolved findings and "
    "for risky areas (auth, payments, migrations, concurrency) even when nothing concrete is wrong."
)

SYSTEM_PROMPT = (
    """\
You are a senior engineer reviewing a pull request. You have the whole repository, a code graph, \
and read-only tools. Your comments go straight onto the PR, so every one must be worth the author's time.

How to work:
- Read the diff, then investigate. For each change, check what calls it and what it calls \
(find_callers, find_definition, read_file), whether similar code elsewhere handles the same case \
differently, and whether config, migrations, tests or docs need to change with it.
- Form a hypothesis for each possible problem, then try to disprove it by reading the code. Only \
keep it if the code confirms it. Cite the file:line you read as evidence. If you could not confirm \
it, drop it - an unconfirmed guess is worse than silence.
- Prefer a few high-value findings over many small ones. Do not comment on formatting a linter \
would catch, on naming preferences, or on code the PR did not change unless the change breaks it.

What to look for, most important first: bugs and wrong behaviour; security (authz gaps, injection, \
secrets, tenant isolation); data loss or corruption; concurrency and transactions; error handling \
that hides failures; breaking changes to callers, APIs or schemas; missing migrations or config; \
then performance, then maintainability.

"""
    + SEVERITY_GUIDE
    + """ Type: logic, syntax, or style.

Line numbers: use the NEW-file line numbers shown in the left gutter of the annotated diff. A \
comment must sit on a line that appears in the diff. If the problem is in unchanged code that the \
change breaks, put the comment on the changed line that causes it.

Suggestions: when the fix is local and certain, give the exact replacement for the lines \
line..end_line (new side) in `suggestion`. Otherwise leave it null.

"""
    + CONFIDENCE_GUIDE
    + """

Diagram: for a non-trivial change, add a small mermaid diagram of the flow or structure it \
changes (sequenceDiagram, flowchart, classDiagram or erDiagram). Null for trivial changes.

Re-reviews: when the PR section "Previous findings on this PR" is present, those are your own \
earlier findings. Check each against the current code and report fixed / still_present / dismissed \
in previous_findings. Do not repeat a finding that was resolved, downvoted or answered with a \
reason unless the code still has the problem and your evidence is stronger; put new effort into \
code that changed since.

Pre-merge checks: when the section "Pre-merge checks to evaluate" is present, evaluate each check \
against the diff and the code, with the same tools, and report one entry per check in `checks`. Say \
inconclusive rather than guess. A check is not a finding; do not repeat it in `findings` unless the \
problem is also a real defect.

Finish by calling submit_review exactly once with the complete review. Do not write the review as \
plain text."""
)


@dataclass
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_turns: int = 40
    max_tokens: int = 64_000
    use_fallbacks: bool = True
    budget_tokens: int | None = None  # scans: input + cache + output tokens allowed across the whole run


@dataclass
class PullRequest:
    title: str
    body: str
    author: str
    base_ref: str
    head_ref: str
    head_sha: str


@dataclass
class Outcome:
    result: ReviewResult
    posted: list[Finding] = field(default_factory=list)  # inline, on a commentable line
    unplaced: list[Finding] = field(default_factory=list)  # real, but outside the diff -> summary
    filtered: int = 0
    suppressed: int = 0
    duplicates: int = 0
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    prior: list[PriorFinding] = field(default_factory=list)  # earlier findings on this PR (re-reviews)
    checks: list[CheckRow] = field(default_factory=list)  # the pre-merge checks table
    verification: Verification | None = None  # the verifier pass, when `verify.enabled`


class ReviewFailed(Exception):
    pass


# ---------- prompt building ----------


def repo_context_parts(
    config: Config, repo_root: Path, paths: list[str], source: FileSource | None = None
) -> list[str]:
    """Team instructions, the rules that apply to `paths`, and the context files (CLAUDE.md...).
    Context files are read from `source` (the PR's base commit in PR mode), else from `repo_root`."""
    source = source or DirSource(repo_root)
    parts: list[str] = []
    rules = config.rules_for(paths)
    if config.instructions.strip():
        parts.append("## Team instructions\n" + config.instructions.strip())
    if rules:
        parts.append(
            "## Team rules (cite the rule id in rule_id when a finding enforces one)\n"
            + "\n".join(f"- [{r.id}] ({r.severity}; {', '.join(r.scope)}) {r.rule}" for r in rules)
        )
    for cf in config.files:
        text = _read_capped(source, cf.path, MAX_CONTEXT_FILE_CHARS)
        if text:
            desc = f" - {cf.description}" if cf.description else ""
            parts.append(f"## Context file: {cf.path}{desc}\n{text}")
    return parts


def build_user_message(
    *,
    pr: PullRequest,
    patches: list[FilePatch],
    index: RepoIndex,
    config: Config,
    repo_root: Path,
    feedback: Feedback | None,
    prior: list[PriorFinding] | None = None,
    source: FileSource | None = None,
) -> list[dict[str, Any]]:
    """Two blocks: stable repo context first (cacheable across re-reviews),
    then the PR itself."""
    repo_parts = repo_context_parts(config, repo_root, [p.path for p in patches], source)
    if feedback:
        section = feedback.prompt_section()
        if section:
            repo_parts.append(section)
    checks_section = checks_prompt(config)
    if checks_section:
        repo_parts.append(checks_section)
    repo_block = "\n\n".join(repo_parts) or "No repository-specific instructions."

    pr_parts = [
        f"# Pull request: {pr.title}",
        f"Author: {pr.author}. Merging {pr.head_ref} into {pr.base_ref}. Head commit {pr.head_sha}.",
        "## Description\n" + (pr.body.strip()[:4000] or "(none)"),
        "## Files changed\n"
        + "\n".join(f"- {p.path} ({p.status}, +{p.additions} -{p.deletions})" for p in patches),
        "## Code graph context for the changed code\n" + graph_context(patches, index),
    ]
    memory = prompt_section(prior or [])
    if memory:
        pr_parts.append(memory)
    diff_text, omitted = _annotated_diffs(patches)
    pr_parts.append(
        "## Diff (left gutter = new-file line number; only these lines can take comments)\n" + diff_text
    )
    if omitted:
        pr_parts.append("Diffs omitted for size (read these files with read_file): " + ", ".join(omitted))
    return [
        {"type": "text", "text": "# Repository context\n\n" + repo_block},
        {"type": "text", "text": "\n\n".join(pr_parts)},
    ]


def graph_context(patches: list[FilePatch], index: RepoIndex) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for p in patches:
        importers = index.importers_of(p.path)
        if importers:
            out.append(f"{p.path} is imported by: {', '.join(importers)}")
        for sym in index.symbols_touching(p.path, p.added):
            if sym.key in seen or len(seen) >= MAX_GRAPH_SYMBOLS:
                continue
            seen.add(sym.key)
            lines = [f"### {sym.kind} {sym.qualname} ({sym.path}:{sym.start}-{sym.end}) - changed"]
            lines += _rel("called by", index.callers_of(sym.name, limit=6))
            lines += _rel("calls", index.callees_of(sym, limit=6))
            lines += _rel("similar", index.similar_to(sym, limit=3))
            out.append("\n".join(lines))
    return "\n\n".join(out) or "(no indexed functions touched - investigate with the tools)"


def _rel(label: str, symbols: list[Symbol]) -> list[str]:
    return [f"- {label}: {s.qualname} at {s.path}:{s.start}  |  {s.signature}" for s in symbols]


def _annotated_diffs(patches: list[FilePatch]) -> tuple[str, list[str]]:
    chunks: list[str] = []
    omitted: list[str] = []
    total = 0
    for p in patches:
        if not p.patch:
            omitted.append(p.path)
            continue
        text = f"### {p.path}\n{p.annotated()}"
        if total + len(text) > MAX_DIFF_CHARS:
            omitted.append(p.path)
            continue
        total += len(text)
        chunks.append(text)
    return "\n\n".join(chunks), omitted


def _read_capped(source: FileSource, path: str, limit: int) -> str:
    try:
        text = source.read(path) or ""
    except ConfigSourceError:
        return ""
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


# ---------- the agent loop ----------


def _tool_defs(tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [{**t, "eager_input_streaming": True} for t in (TOOLS if tools is None else tools)]


def _valid_args(
    name: str,
    args: Any,
    tools: list[dict[str, Any]] | None = None,
    submit_name: str = "submit_review",
) -> str | None:
    """With eager streaming the server no longer validates tool input, so we do."""
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    schema = next((t["input_schema"] for t in (TOOLS if tools is None else tools) if t["name"] == name), None)
    if schema is None:
        return f"Unknown tool {name!r}."
    if name == submit_name:
        return None  # validated by pydantic below, with a precise error
    missing = [k for k in schema.get("required", []) if k not in args]
    unknown = [k for k in args if k not in schema["properties"]]
    if missing or unknown:
        return f"Invalid input: missing {missing}, unexpected {unknown}."
    for k, v in args.items():
        want = schema["properties"][k]["type"]
        if (want == "string" and not isinstance(v, str)) or (want == "integer" and not isinstance(v, int)):
            return f"Invalid input: {k} must be a {want}."
    return None


@dataclass
class LoopState:
    """What a run has used so far. Owned by the caller so it survives a failed run (the spend is real)."""

    usage: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    )
    turns: int = 0
    budget_nudged_at: int | None = None  # the turn after which the model was told to submit (budget)

    @property
    def total_tokens(self) -> int:
        return sum(self.usage.values())


def _with_note(content: str | list[dict[str, Any]], note: str | None) -> str | list[dict[str, Any]]:
    if not note:
        return content
    blocks: list[dict[str, Any]] = (
        [{"type": "text", "text": content}] if isinstance(content, str) else content
    )
    return [*blocks, {"type": "text", "text": note}]


def agent_loop[T](
    client: anthropic.Anthropic,
    settings: Settings,
    *,
    system: str,
    tools: list[dict[str, Any]],
    submit_name: str,
    parse: Callable[[dict[str, Any]], T],
    user_content: list[dict[str, Any]],
    toolbox: Toolbox,
    state: LoopState,
    on_turn: Callable[[int], None] | None = None,
) -> T:
    """Tool loop shared by PR reviews and repository scans. Ends when the model calls `submit_name`.

    `parse` turns the submit input into the result and raises ValidationError when it does not fit.
    With `settings.budget_tokens`, the model is told to submit once two more turns of the size of the
    last one would pass the budget; if it has not submitted one turn after that, the run stops.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    usage = state.usage
    nudged = False
    json_retries = 0
    extra: dict[str, Any] = {}
    if settings.use_fallbacks:
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    tool_defs = _tool_defs(tools)

    for turn in range(1, settings.max_turns + 1):
        if state.budget_nudged_at is not None and turn > state.budget_nudged_at + 1:
            raise ReviewFailed(
                f"the token budget ({settings.budget_tokens or 0:,}) was reached before the model submitted "
                f"({state.total_tokens:,} tokens used)"
            )
        if on_turn:
            on_turn(turn)
        try:
            with client.beta.messages.stream(
                model=settings.model,
                max_tokens=settings.max_tokens,
                system=system,
                tools=tool_defs,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                output_config={"effort": settings.effort},  # type: ignore[arg-type]
                cache_control={"type": "ephemeral"},
                **extra,
            ) as stream:
                response = stream.get_final_message()
            json_retries = 0
        except ValueError:
            # Tool-input JSON the SDK could not parse at all: no tool_use_id
            # to answer, so re-issue the turn (bounded).
            json_retries += 1
            if json_retries > 2:
                raise ReviewFailed("the model kept producing unparseable tool input") from None
            continue

        state.turns = turn
        u = response.usage
        before = state.total_tokens
        usage["input"] += u.input_tokens or 0
        usage["output"] += u.output_tokens or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        this_turn = state.total_tokens - before

        if response.stop_reason == "refusal":
            raise ReviewFailed("the model declined to review this code")
        # Append the assistant turn unchanged (thinking blocks must round-trip).
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # Notes for the model's next turn: the budget, then the turns left.
        notes: list[str] = []
        budget = settings.budget_tokens
        if budget and state.budget_nudged_at is None and state.total_tokens + 2 * this_turn > budget:
            state.budget_nudged_at = turn
            notes.append(
                f"Token budget nearly used ({state.total_tokens:,} of {budget:,}). This is your last turn: "
                f"call {submit_name} now with what you have verified. Do not call any other tool."
            )
        left = settings.max_turns - turn
        if left <= 3 and state.budget_nudged_at != turn:
            notes.append(f"{left} turns left. Finish verifying and call {submit_name}.")
        note = " ".join(notes) or None

        if response.stop_reason == "max_tokens":
            if tool_uses:
                # A tool call cut off mid-input: answer it as an error so the
                # history stays valid, and ask for a smaller step.
                messages.append({"role": "user", "content": _with_note([
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                     "content": "Output limit reached mid-call. Retry with a smaller input."}
                    for b in tool_uses
                ], note)})  # fmt: skip
                continue
            raise ReviewFailed("the response hit max_tokens without submitting a result")

        if not tool_uses:
            if nudged:
                raise ReviewFailed(f"the model finished without calling {submit_name}")
            nudged = True
            nudge = f"Call {submit_name} now with your complete result."
            messages.append({"role": "user", "content": _with_note(nudge, note)})
            continue

        results: list[dict[str, Any]] = []
        submitted: T | None = None
        for b in tool_uses:
            err = _valid_args(b.name, b.input, tools, submit_name)
            if err:
                results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content": err})
                continue
            if b.name == submit_name:
                try:
                    submitted = parse(b.input)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": "Received."})
                except ValidationError as e:
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": f"The result did not match the schema: {e}"})  # fmt: skip
                continue
            try:
                content = toolbox.run(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
            except (ToolError, TypeError) as e:
                results.append(
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content": str(e)}
                )

        if submitted is not None:
            return submitted

        messages.append({"role": "user", "content": _with_note(results, note)})

    raise ReviewFailed(f"no result after {settings.max_turns} turns")


def run_review(
    client: anthropic.Anthropic,
    settings: Settings,
    user_content: list[dict[str, Any]],
    toolbox: Toolbox,
    on_turn: Callable[[int], None] | None = None,
) -> tuple[ReviewResult, int, dict[str, int]]:
    state = LoopState()
    result = agent_loop(
        client, settings, system=SYSTEM_PROMPT, tools=TOOLS, submit_name="submit_review",
        parse=ReviewResult.model_validate, user_content=user_content, toolbox=toolbox, state=state,
        on_turn=on_turn,
    )  # fmt: skip
    return result, state.turns, state.usage


# ---------- after the model ----------


ExistingTitles = list[tuple[Any, set[str]]]


def existing_titles(existing_comments: list[dict[str, Any]]) -> ExistingTitles:
    """(path, title words) of this reviewer's comments already on the PR."""
    from simpli_review.render import parse_finding_marker

    return [
        (c.get("path"), words(parse_finding_marker(c.get("body") or "").get("title", "")))
        for c in existing_comments
        if parse_finding_marker(c.get("body") or "")
    ]


def screen(
    f: Finding, config: Config, feedback: Feedback | None, existing: ExistingTitles
) -> Literal["filtered", "suppressed", "duplicate"] | None:
    """Why `finalize` would leave this finding out, or None when it would keep it."""
    if not config.keeps(f):
        return "filtered"
    if feedback and feedback.suppresses(f):
        return "suppressed"
    tw = words(f.title)
    if any(path == f.path and similarity(tw, ew) >= 0.6 for path, ew in existing):
        return "duplicate"
    return None


def finalize(
    result: ReviewResult,
    *,
    patches: list[FilePatch],
    config: Config,
    feedback: Feedback | None,
    existing_comments: list[dict[str, Any]],
) -> Outcome:
    """Place, filter, de-duplicate and cap the model's findings."""
    by_path = {p.path: p for p in patches}
    outcome = Outcome(result=result)
    existing = existing_titles(existing_comments)

    candidates: list[Finding] = []
    for f in result.findings:
        why = screen(f, config, feedback, existing)
        if why == "filtered":
            outcome.filtered += 1
        elif why == "suppressed":
            outcome.suppressed += 1
        elif why == "duplicate":
            outcome.duplicates += 1
        else:
            candidates.append(f)

    order = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}
    candidates.sort(key=lambda f: (order[f.severity], -f.confidence))
    for f in candidates:
        fp = by_path.get(f.path)
        placed = nearest_commentable(fp, f.line) if fp else None
        if placed is None or len(outcome.posted) >= config.max_comments:
            outcome.unplaced.append(f)
            continue
        end = f.end_line if f.end_line and f.end_line > f.line else None
        moved = placed != f.line
        valid_range = (
            end is not None and fp is not None and all(ln in fp.commentable for ln in range(placed, end + 1))
        )
        outcome.posted.append(
            f.model_copy(update={
                "line": placed,
                "end_line": end if (valid_range and not moved) else None,
                # A suggestion replaces exact lines; if we had to move the
                # comment, the suggestion would replace the wrong ones.
                "suggestion": None if moved or (end and not valid_range) else f.suggestion,
            })
        )  # fmt: skip
    return outcome


def to_github_comments(findings: list[Finding], body_for: Callable[[Finding], str]) -> list[dict[str, Any]]:
    out = []
    for f in findings:
        c: dict[str, Any] = {
            "path": f.path,
            "line": f.end_line or f.line,
            "side": "RIGHT",
            "body": body_for(f),
        }
        if f.end_line:
            c["start_line"] = f.line
            c["start_side"] = "RIGHT"
        out.append(c)
    return out


def usage_line(usage: dict[str, int]) -> str:
    return json.dumps(usage, sort_keys=True)
