"""Entry points.

    simpli-review github            run inside a GitHub Actions job (action.yml)
    simpli-review local --base main review the current branch, print the result
    simpli-review local --base main --json   same, as one JSON object (for agents)
    simpli-review scan              full-repository AI security scan of the checked-out commit (see scan.py)
    simpli-review init              one-command setup of workflows, config and labels (see init.py)

Settings come from the environment (action inputs arrive as INPUT_*):
  INPUT_NAME     heading on the summary comment          (required in github mode)
  INPUT_MENTION  comment text that re-runs a review       (default "@<name, slugified>")
  INPUT_MODEL / INPUT_EFFORT / INPUT_MAX_TURNS            model settings
Nothing product-specific is hard-coded.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

import anthropic

from simpli_review.agent_prompt import AgentTask, combined_details
from simpli_review.blocking import evaluate, open_tasks, still_open_findings
from simpli_review.checks import build_rows, read_pr_template
from simpli_review.config import TEMPLATE_PATH, Config, config_changes, load_config_from
from simpli_review.config_source import (
    CachingSource,
    ConfigSourceError,
    FileSource,
    GitSource,
    open_base_source,
)
from simpli_review.diff import FilePatch, parse_patch, split_unified_diff
from simpli_review.github import GitHub, GitHubError
from simpli_review.index import RepoIndex
from simpli_review.learning import Feedback, past_bot_comments
from simpli_review.memory import (
    PriorFinding,
    SinceLast,
    collect_prior_findings,
    compute_since_last,
    since_last_block,
)
from simpli_review.models import BlockDecision, ReviewResult
from simpli_review.pricing import estimate_micros, format_usd
from simpli_review.render import SUMMARY_MARKER, inline_body, summary_body
from simpli_review.report import build_report
from simpli_review.reviewer import (
    Outcome,
    PullRequest,
    ReviewFailed,
    Settings,
    build_user_message,
    existing_titles,
    finalize,
    repo_context_parts,
    run_review,
    screen,
    to_github_comments,
    usage_line,
)
from simpli_review.scrub import api_error_text
from simpli_review.tools import Toolbox
from simpli_review.verifier import reconcile_summary, verification_json, verify_findings

log = logging.getLogger("simpli_review")
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
PR_EVENTS = ("pull_request", "pull_request_target")
PR_ACTIONS = {"opened", "reopened", "ready_for_review", "synchronize", "labeled"}
EXIT_BLOCKED = 1  # the review finished and its result blocks the merge


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def settings_from_env() -> Settings:
    s = Settings()
    s.model = _env("INPUT_MODEL", s.model)
    s.effort = _env("INPUT_EFFORT", s.effort)
    s.max_turns = int(_env("INPUT_MAX_TURNS", str(s.max_turns)))
    s.use_fallbacks = _env("INPUT_FALLBACKS", "true").lower() != "false"
    return s


def default_mention(name: str) -> str:
    return "@" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "review")


# ---------- GitHub mode ----------


def is_re_review_label(event_name: str, event: dict[str, Any], config: Config) -> bool:
    """True for a pull_request `labeled` event whose label is the configured re-review label."""
    if event_name not in ("pull_request", "pull_request_target") or event.get("action") != "labeled":
        return False
    return (event.get("label") or {}).get("name") == config.re_review_label


def decide_trigger(
    event_name: str, event: dict[str, Any], config: Config, mention: str
) -> tuple[int | None, bool, str]:
    """(PR number or None, manual?, reason when skipped)."""
    if event_name in ("pull_request", "pull_request_target"):
        action = event.get("action", "")
        if action == "labeled":
            # Adding the re-review label is a manual re-run. Only people with triage access or
            # more can label a PR, so no author_association check is needed.
            if is_re_review_label(event_name, event, config):
                return int(event["pull_request"]["number"]), True, ""
            return None, False, "the label added is not the re-review label"
        kind = {"opened": "open", "reopened": "open", "ready_for_review": "open", "synchronize": "push"}.get(
            action
        )
        if kind is None or kind not in config.auto_review:
            return None, False, f"pull_request.{action} is not configured to trigger a review (autoReview)"
        return int(event["pull_request"]["number"]), False, ""
    if event_name == "issue_comment":
        issue = event.get("issue", {})
        comment = event.get("comment", {})
        if "pull_request" not in issue:
            return None, False, "comment is not on a pull request"
        if mention.lower() not in (comment.get("body") or "").lower():
            return None, False, "comment does not mention the reviewer"
        # Reviews spend API credit: only people with write access can ask for one.
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            return None, False, "commenter is not an owner, member or collaborator"
        return int(issue["number"]), True, ""
    return None, False, f"event {event_name} is not handled"


def github_main(dry_run: bool) -> int:
    loaded: list[Config] = []  # the settings the run used (from the base commit), if it got that far
    try:
        return _github_run(dry_run, loaded)
    finally:
        # Success, skip or failure: take the label off so it can be added again. The label name comes
        # from the base settings; when those could not be read, the default name is used. Never the PR's.
        if not dry_run:
            _remove_re_review_label(loaded[0] if loaded else Config())


def _remove_re_review_label(config: Config) -> None:
    try:
        event = json.loads(Path(_env("GITHUB_EVENT_PATH")).read_text(encoding="utf-8"))
        if not is_re_review_label(_env("GITHUB_EVENT_NAME"), event, config):
            return
        GitHub(_env("GITHUB_TOKEN"), _env("GITHUB_REPOSITORY")).remove_label(
            int(event["pull_request"]["number"]), config.re_review_label
        )
    except (OSError, ValueError, KeyError, GitHubError) as e:
        # Cosmetic for this run; never turn a finished review into a failed job.
        log.warning("could not remove the re-review label (%s)", type(e).__name__)


def _load_trusted_config(source: FileSource) -> Config:
    """The settings, rules, context files and PR template from `source`, all read up front so a
    failure surfaces before the review starts (and the answers are cached for the run)."""
    try:
        config = load_config_from(source)
    except (ValueError, TypeError) as e:  # bad JSON, bad values, undecodable text
        raise ConfigSourceError(f"the review settings there are not valid ({type(e).__name__})") from e
    for cf in config.files:
        source.read(cf.path)
    source.read(TEMPLATE_PATH)
    return config


def _settings_unavailable(gh: GitHub, name: str, number: int, why: str, dry_run: bool) -> int:
    """Fail closed: without the trusted settings the review does not run, and the check fails."""
    msg = " ".join(why.split())
    print(f"::error::Review not run: {msg}")
    if dry_run:
        return 0
    try:
        gh.set_status_reaction(number, "confused", _env("INPUT_BOT_LOGIN") or "github-actions[bot]")
        gh.upsert_issue_comment(
            number, SUMMARY_MARKER,
            f"{SUMMARY_MARKER}\n## {name}\n\nThe review did not run: the review settings could not be "
            f"read from the base branch ({msg}). Settings are never taken from the pull request itself, "
            "so this check fails until a review completes. Re-run the failed workflow job to try again.",
        )  # fmt: skip
    except GitHubError as e:
        log.warning("could not post the failure notice (%s)", e.status)
    return 1


def _github_run(dry_run: bool, loaded: list[Config] | None = None) -> int:
    name = _env("INPUT_NAME")
    if not name:
        print("::error::The `name` input is required (it heads the summary comment).")
        return 2
    mention = _env("INPUT_MENTION") or default_mention(name)
    repo_root = Path(_env("GITHUB_WORKSPACE", "."))
    event_name = _env("GITHUB_EVENT_NAME")
    event = json.loads(Path(_env("GITHUB_EVENT_PATH")).read_text(encoding="utf-8"))
    gh = GitHub(_env("GITHUB_TOKEN"), _env("GITHUB_REPOSITORY"))

    # The settings that steer this review come from the PR's base commit, never from its head.
    # Whether an event is a review trigger can depend on them (autoReview, reReviewLabel), so they are
    # loaded first; a comment trigger does not depend on them and is filtered before any API call.
    pr_raw: dict[str, Any] | None = None
    if event_name in PR_EVENTS and event.get("action") in PR_ACTIONS:
        number = int(event["pull_request"]["number"])
        base_sha = str((event["pull_request"].get("base") or {}).get("sha") or "")
    elif event_name == "issue_comment":
        found, _, why = decide_trigger(event_name, event, Config(), mention)
        if found is None:
            print(f"Skipping: {why}.")
            return 0
        number, base_sha = found, ""
    else:
        _, _, why = decide_trigger(event_name, event, Config(), mention)
        print(f"Skipping: {why}.")
        return 0
    try:
        if event_name == "issue_comment":
            pr_raw = gh.pull(number)
            base_sha = str((pr_raw.get("base") or {}).get("sha") or "")
        source = open_base_source(gh, repo_root, base_sha)
        config = _load_trusted_config(source)
    except (ConfigSourceError, GitHubError) as e:
        return _settings_unavailable(gh, name, number, str(e), dry_run)
    if loaded is not None:
        loaded.append(config)

    manual = event_name == "issue_comment"
    if not manual:
        found, manual, why = decide_trigger(event_name, event, config, mention)
        if found is None:
            print(f"Skipping: {why}.")
            return 0

    pr_raw = pr_raw or gh.pull(number)
    config_source = f"{pr_raw['base']['ref']} @ {base_sha[:7]}"
    labels = [lb["name"] for lb in pr_raw.get("labels", [])]
    if not manual:
        ok, reason = config.should_review(
            draft=bool(pr_raw.get("draft")), author=pr_raw["user"]["login"], labels=labels
        )
        if not ok:
            print(f"Skipping: {reason}.")
            return 0

    head_sha = pr_raw["head"]["sha"]
    checked_out = _git(repo_root, "rev-parse", "HEAD")
    if checked_out and checked_out != head_sha:
        print(f"::warning::Workspace is at {checked_out[:7]}, PR head is {head_sha[:7]}; "
              "check out the PR head (see README) so the code graph matches the diff.")  # fmt: skip

    pr = PullRequest(
        title=pr_raw.get("title") or "",
        body=pr_raw.get("body") or "",
        author=pr_raw["user"]["login"],
        base_ref=pr_raw["base"]["ref"],
        head_ref=pr_raw["head"]["ref"],
        head_sha=head_sha,
    )
    files = gh.pull_files(number)
    changes = config_changes(config, [p for f in files for p in (f["filename"], f.get("previous_filename"))])
    patches = [
        parse_patch(
            f["filename"], f.get("patch") or "", f.get("status", "modified"), f.get("previous_filename")
        )
        for f in files
        if not config.ignored(f["filename"])
    ]
    if not patches:
        print("Skipping: every changed file matches ignorePatterns.")
        return 0

    me = None if dry_run else _env("INPUT_BOT_LOGIN") or "github-actions[bot]"
    if not dry_run:
        gh.set_status_reaction(number, "eyes", me)
    try:
        outcome = _review(repo_root, pr, patches, config, gh, number, source=source)
    except (ReviewFailed, anthropic.APIError) as e:
        msg = " ".join((str(e) if isinstance(e, ReviewFailed) else api_error_text(e)).split())
        # A repo that relies on this check to block merges must not pass without a review.
        fail_closed = config.block_on != "none" and config.override_label not in labels
        print(f"::{'error' if fail_closed else 'warning'}::Review failed: {msg}")
        if not dry_run:
            gh.set_status_reaction(number, "confused", me)
            if fail_closed:
                msg += ". Merging is blocked until a review completes"
            gh.upsert_issue_comment(
                number, SUMMARY_MARKER,
                f"{SUMMARY_MARKER}\n## {name}\n\nThe review could not finish: {msg}. "
                f"{_retry_hint(config, mention)}",
            )  # fmt: skip
        return 1 if fail_closed and not dry_run else 0

    review_count = _previous_review_count(gh, number) + 1
    since, tasks, decision = _gate(outcome, config, labels)

    def render() -> str:
        return _render_summary(
            name,
            outcome,
            config,
            head_sha,
            len(patches),
            review_count,
            mention,
            since,
            tasks,
            decision,
            re_review_label=config.re_review_label,
            override_label=config.override_label,
            config_changes=changes,
            config_source=config_source,
        )

    summary = render()
    print(f"Review: {len(outcome.posted)} inline, {len(outcome.unplaced)} in summary, "
          f"{outcome.filtered} filtered, {outcome.suppressed} suppressed, {outcome.duplicates} duplicates, "
          f"{outcome.turns} turns, tokens {usage_line(outcome.usage)}")  # fmt: skip
    print(cost_line(settings_from_env().model, outcome))
    if dry_run:
        print(summary)
        for c in to_github_comments(outcome.posted, inline_body):
            print(f"\n--- {c['path']}:{c['line']}\n{c['body']}")
        return 0

    if outcome.posted:
        comments = to_github_comments(outcome.posted, inline_body)
        try:
            gh.create_review(
                number, head_sha, f"{len(comments)} inline comment(s) — summary below.", comments
            )
        except GitHubError as e:
            if e.status != 422:
                raise
            # GitHub rejected a comment position (diff moved on). Keep the
            # findings: put them in the summary instead of losing them.
            print("::warning::GitHub rejected an inline position; moving comments to the summary.")
            outcome.unplaced = outcome.posted + outcome.unplaced
            outcome.posted = []
            summary = render()
    gh.upsert_issue_comment(number, SUMMARY_MARKER, summary)
    gh.set_status_reaction(number, "+1", me)
    return _exit_code(decision, name)


def cost_line(model: str, outcome: Outcome) -> str:
    """Estimated spend of the review, the reviewer and the verifier priced separately (pricing.py)."""
    parts = [f"reviewer {model} {format_usd(estimate_micros(model, outcome.usage))}"]
    v = outcome.verification
    if v is not None:
        counts = ", ".join(f"{k} {n}" for k, n in v.counts().items())
        cost = format_usd(estimate_micros(v.model, v.usage))
        parts.append(f"verifier {v.model} {cost} (tokens {usage_line(v.usage)}; {counts})")
    return "Estimated cost: " + "; ".join(parts)


def _exit_code(decision: BlockDecision, name: str) -> int:
    """Non-zero turns the Actions check red. Logged as one line, so a reason can't add workflow commands."""
    if decision.overridden_by:
        print(f"Merge block overridden by the {decision.overridden_by!r} label.")
    if not decision.blocking:
        return 0
    print("::error::" + " ".join(f"{name} is blocking the merge: {'; '.join(decision.reasons)}".split()))
    return EXIT_BLOCKED


def _gate(
    outcome: Outcome, config: Config, labels: list[str]
) -> tuple[SinceLast | None, list[AgentTask], BlockDecision]:
    """Per-PR memory, every open finding, and whether this review blocks the merge."""
    since = compute_since_last(
        outcome.prior, outcome.result.previous_findings, len(outcome.posted) + len(outcome.unplaced)
    )
    tasks = open_tasks(outcome.posted + outcome.unplaced, still_open_findings(since))
    return since, tasks, evaluate(config, tasks=tasks, checks=outcome.checks, labels=labels)


def _render_summary(
    name: str,
    outcome: Outcome,
    config: Config,
    head_sha: str,
    files_reviewed: int,
    review_count: int,
    mention: str,
    since: SinceLast | None,
    tasks: list[AgentTask],
    decision: BlockDecision,
    re_review_label: str | None = None,
    override_label: str | None = None,
    config_changes: list[str] | None = None,
    config_source: str = "",
) -> str:
    return summary_body(
        name=name, result=outcome.result, posted=outcome.posted, unplaced=outcome.unplaced,
        suppressed=outcome.suppressed, head_sha=head_sha, files_reviewed=files_reviewed,
        include_diagram=config.include_diagram, review_count=review_count, mention=mention,
        since_last=since_last_block(since), re_review_label=re_review_label, checks=outcome.checks,
        block=decision, agent_prompt=combined_details(tasks), override_label=override_label,
        config_changes=config_changes, config_source=config_source, verification=outcome.verification,
    )  # fmt: skip


def _review(
    repo_root: Path,
    pr: PullRequest,
    patches: list[FilePatch],
    config: Config,
    gh: GitHub | None,
    number: int | None = None,
    *,
    local: bool = False,
    progress: TextIO | None = None,
    source: FileSource | None = None,
) -> Outcome:
    index = RepoIndex(repo_root, config.ignored)
    feedback = None
    if gh is not None and config.learn_from_reactions:
        try:
            feedback = Feedback.from_history(past_bot_comments(gh.recent_repo_review_comments()))
        except GitHubError as e:
            log.warning("could not read past review comments (%s); reviewing without feedback", e.status)
    existing: list[dict[str, Any]] = []
    prior = []
    if gh is not None and number is not None:
        existing = gh.pull_review_comments(number)
        prior = _prior_findings(gh, number, existing)
    user = build_user_message(
        pr=pr,
        patches=patches,
        index=index,
        config=config,
        repo_root=repo_root,
        feedback=feedback,
        prior=prior,
        source=source,
    )
    client = anthropic.Anthropic()
    settings = settings_from_env()
    result, turns, usage = run_review(
        client, settings, user, Toolbox(repo_root, index),
        on_turn=lambda t: print(f"turn {t}", flush=True, file=progress or sys.stdout),
    )  # fmt: skip
    verification = None
    if config.verify.enabled:
        # Only what finalize would keep is worth checking: filtered, suppressed and repeated ones go anyway.
        seen = existing_titles(existing)
        # Team instructions, rules and context files as the reviewer got them: from the base commit.
        parts = repo_context_parts(config, repo_root, [p.path for p in patches], source)
        context = "# Repository context\n\n" + "\n\n".join(parts) if parts else ""
        kept, verification = verify_findings(
            client, settings, result.findings, patches=patches, config=config,
            make_toolbox=lambda: Toolbox(repo_root, index), context=context,
            should_verify=lambda f: screen(f, config, feedback, seen) is None,
        )  # fmt: skip
        result = result.model_copy(update={"findings": kept})
        result = reconcile_summary(client, settings, result, verification, Toolbox(repo_root, index))
    outcome = finalize(result, patches=patches, config=config, feedback=feedback, existing_comments=existing)
    outcome.turns, outcome.usage = turns, usage
    outcome.verification = verification
    outcome.prior = prior
    # A local review has no PR description, so the PR-template check does not apply to it.
    outcome.checks = build_rows(
        config, result.checks, read_pr_template(source or repo_root), None if local else pr.body
    )
    return outcome


def _prior_findings(gh: GitHub, number: int, existing: list[dict[str, Any]]) -> list[PriorFinding]:
    """Memory of this reviewer's earlier findings. Any read failure means a fresh review, not a failed one."""
    try:
        resolved = gh.resolved_comment_ids(number)
    except (GitHubError, KeyError, TypeError) as e:
        log.warning("could not read resolved threads (%s); treating none as resolved", type(e).__name__)
        resolved = set()
    summary = ""
    try:
        for c in gh.issue_comments(number):
            if SUMMARY_MARKER in (c.get("body") or ""):
                summary = c.get("body") or ""
                break
    except GitHubError as e:
        log.warning("could not read the earlier summary (%s)", e.status)
    return collect_prior_findings(existing, resolved, summary)


def _retry_hint(config: Config, mention: str) -> str:
    return f"Add the `{config.re_review_label}` label to try again (or comment `{mention}`)."


def _previous_review_count(gh: GitHub, number: int) -> int:
    for c in gh.issue_comments(number):
        body = c.get("body") or ""
        if SUMMARY_MARKER in body:
            m = re.search(r"review #(\d+)", body)
            return int(m.group(1)) if m else 1
    return 0


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


# ---------- local mode ----------


def local_main(base: str, repo: str, as_json: bool = False) -> int:
    """Review the current branch. With `as_json`, stdout is one JSON object; progress goes to stderr."""
    root = Path(repo).resolve()
    merge_base = _git(root, "merge-base", base, "HEAD")
    if not merge_base:
        return _local_error(f"Cannot find a merge base between {base} and HEAD.", 2, as_json)
    # Same rule as a PR: settings, rules and context files come from the base ref, not this branch.
    try:
        source = CachingSource(GitSource(root, base).verify())
        config = _load_trusted_config(source)
    except ConfigSourceError as e:
        return _local_error(f"Cannot read the review settings from {base}: {e}", 2, as_json)
    diff = _git(root, "diff", "--unified=3", merge_base, "HEAD")
    all_patches = split_unified_diff(diff)
    changes = config_changes(config, [p for fp in all_patches for p in (fp.path, fp.previous_path)])
    patches = [p for p in all_patches if not config.ignored(p.path)]
    if not patches:
        if as_json:
            empty = ReviewResult(
                summary="No changes to review.", confidence_score=5, confidence_reason="Nothing changed.",
                files=[], findings=[],
            )  # fmt: skip
            print(json.dumps(build_report(result=empty, posted=[], unplaced=[], checks=[],
                                          decision=BlockDecision(blocking=False), tasks=[],
                                          config_source=source.label, config_changes=changes)))  # fmt: skip
        else:
            print("No changes to review.")
        return 0
    pr = PullRequest(
        title=_git(root, "log", "-1", "--format=%s"),
        body=_git(root, "log", "--format=- %s", f"{merge_base}..HEAD"),
        author=_git(root, "log", "-1", "--format=%an"),
        base_ref=base,
        head_ref=_git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        head_sha=_git(root, "rev-parse", "HEAD"),
    )
    try:
        outcome = _review(
            root,
            pr,
            patches,
            config,
            gh=None,
            local=True,
            progress=sys.stderr if as_json else None,
            source=source,
        )
    except ReviewFailed as e:
        return _local_error(f"Review failed: {e}", 1, as_json)
    # No PR here, so no labels: an override label never applies to a local review.
    since, tasks, decision = _gate(outcome, config, [])
    print(f"tokens: {usage_line(outcome.usage)}", file=sys.stderr)
    print(cost_line(settings_from_env().model, outcome), file=sys.stderr)
    if as_json:
        print(json.dumps(build_report(result=outcome.result, posted=outcome.posted, unplaced=outcome.unplaced,
                                      checks=outcome.checks, decision=decision, tasks=tasks,
                                      config_source=source.label, config_changes=changes,
                                      verification=verification_json(outcome.verification))))  # fmt: skip
        return EXIT_BLOCKED if decision.blocking else 0
    name = _env("INPUT_NAME", "Code review")
    print(_render_summary(name, outcome, config, pr.head_sha, len(patches), 1, default_mention(name),
                          since, tasks, decision, config_changes=changes,
                          config_source=source.label))  # fmt: skip
    for f in outcome.posted:
        print(f"\n--- {f.path}:{f.line}\n{inline_body(f)}")
    return EXIT_BLOCKED if decision.blocking else 0


def _local_error(message: str, code: int, as_json: bool) -> int:
    print(message, file=sys.stderr)
    if as_json:
        print(json.dumps({"error": message, "blocking": False}))
    return code


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="simpli-review")
    sub = parser.add_subparsers(dest="mode", required=True)
    g = sub.add_parser("github", help="run inside a GitHub Actions job")
    g.add_argument("--dry-run", action="store_true", help="print the review instead of posting it")
    loc = sub.add_parser("local", help="review the current branch against a base")
    loc.add_argument("--base", default="origin/main")
    loc.add_argument("--repo", default=".")
    loc.add_argument("--json", action="store_true", dest="as_json",
                     help="print one JSON object (findings, checks, blocking) instead of text")  # fmt: skip
    sc = sub.add_parser(
        "scan", help="scan the whole repository at the checked-out commit for security problems"
    )
    sc.add_argument("--dry-run", action="store_true", help="do not open issues; say which would be opened")
    sc.add_argument("--repo", default=".")
    ini = sub.add_parser("init", help="set up simpli-review in the repository in the current directory")
    ini.add_argument("--yes", "--non-interactive", action="store_true", dest="yes",
                     help="take every default without asking")  # fmt: skip
    ini.add_argument("--force", action="store_true", help="overwrite files that already exist")
    ini.add_argument("--dry-run", action="store_true", help="print what would be written, change nothing")
    ini.add_argument("--no-labels", action="store_true", help="do not create (or print commands for) labels")
    ini.add_argument(
        "--ref", metavar="OWNER/REPO@SHA", help="pin this exact build instead of the latest release"
    )
    ini.add_argument("--repo", default=".", help="repository root (default: the current directory)")
    args = parser.parse_args(argv)
    if args.mode == "init":
        from simpli_review.init import InitOptions, run_init

        return run_init(
            InitOptions(
                root=Path(args.repo),
                yes=args.yes,
                force=args.force,
                dry_run=args.dry_run,
                no_labels=args.no_labels,
                ref=args.ref,
            )
        )
    if args.mode == "github":
        return github_main(args.dry_run)
    if args.mode == "scan":
        from simpli_review.scan import scan_main

        return scan_main(repo=args.repo, dry_run=args.dry_run)
    return local_main(args.base, args.repo, args.as_json)


if __name__ == "__main__":
    sys.exit(main())
