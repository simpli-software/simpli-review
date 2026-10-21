# How the reviewer works

This describes the design as built: what runs on each pull request, what it can see, how its findings are
filtered, and what it deliberately does not do. File and function names in the last column point at the code.

## The design in one paragraph

A review is one agent loop. Before the loop starts, the code builds an in-memory graph of the repository at the
PR's head commit and seeds the conversation with the diff plus graph context for each changed function. Claude
then investigates with read-only tools until it submits a structured review. Code, not the model, filters that
review (strictness, confidence, team feedback, duplicates), renders it and posts it. Everything the model reads
is confined to the checked-out repository. The review's own settings are the one exception to "the PR's head":
they come from the base commit, so a PR cannot loosen its own review (see "Settings from the base branch" below).

## What it does

| Area | Behaviour | Where |
|---|---|---|
| Code graph | An in-memory graph of the repository: Python is parsed with `ast`, JS/TS with regexes. Each changed function is seeded with its callers, callees, similar functions and the files that import it. Everything else is searchable as text. | `index.py`, `reviewer.graph_context` |
| Agent loop | The model investigates with `read_file`, `search_code`, `find_definition`, `find_callers`, `list_directory` and `git_history` until it calls `submit_review`. Every tool is read-only and confined to the repository; secret files are refused. | `reviewer.run_review`, `tools.py` |
| Challenging its own findings | Part of the system prompt. Every finding must cite evidence (the `file:line` it read) and a confidence from 1 to 5; anything under `minConfidence` (default 3) is dropped. | `reviewer.SYSTEM_PROMPT`, `config.keeps` |
| Verifier pass (on by default) | Unless `verify.enabled` is false, every finding at the configured severities that `finalize` would keep goes to its own fresh-context run: a different prompt (try to disprove it), the same read-only tools without `submit_review`, and only that finding (title, body, path, lines, severity), the diff of its file, the text of a team rule it cites, and the same repository context the reviewer got (team instructions, rules, context files), read from the base commit so the PR cannot rewrite the conventions its findings are judged by. The reviewer's evidence and conversation are not passed on. It answers `submit_verdict` with keep, downgrade (one step) or drop, a 1-5 confidence and a one-line reason; a confidence under `verify.minConfidence` drops it. The verifier reads code the PR controls, so it can never unblock a merge: with `blockOn` set, a finding at or above it is never dropped or lowered below it. Such a verdict marks it disputed instead; it keeps its severity and still blocks, its inline comment and the summary carry the second check's reason, and only the override label waives it (the same rule as any other block). Below `blockOn`, and with `blockOn: none`, verdicts apply as described. Runs at most `verify.concurrency` at a time, each capped in turns, tokens and wall time. Any failure keeps the finding unverified (never a silent drop) and the summary says how many. Dropped findings appear only in a collapsed summary section with their reason, outside the list the per-PR memory reads, so they are never posted, learned from or counted as dismissed. When the pass dropped or downgraded anything, one more short call (same model, effort low, no repository tools) rewrites the summary, confidence score and reason from the findings that remain, keeping the reviewer's words where they still hold; the verifier section says so. If that call fails, the reviewer's text stays and its confidence reason says it was written before verification. | `verifier.py`, `cli._review`, `render.verification_section` |
| Prompt caching | The stable system prompt and repository context come first, with top-level cache control, so the later turns of one review re-read cached tokens. | `reviewer.run_review` |
| Output | One summary comment, updated in place on each run: a summary, a 0-5 confidence score, a table of pre-merge checks, a file table, an optional diagram and a footer with the reviewed commit. Inline comments carry a P0/P1/P2 badge, a type (logic, syntax, style) and a suggestion block that GitHub can apply in one click. A suggestion is dropped if its comment had to move to another line, because it would replace the wrong lines. | `render.summary_body`, `render.inline_body`, `reviewer.finalize` |
| Status | A reaction on the PR shows progress: eyes while running, thumbs-up when done, confused on failure. Earlier status reactions from the reviewer are removed so only the latest shows. | `github.set_status_reaction` |
| Configuration | A `.simpli-review/` folder in the reviewed repository: `config.json`, `rules.md` and `files.json`. See the README for every field. | `config.py` |
| Settings from the base branch | A PR can edit its own settings, so it must not be able to steer its own review. In PR mode the settings, rules, context files (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/DECISIONS.md`, `files.json` entries) and the PR template are read from the base commit, never from the head: the GitHub contents API at `pull_request.base.sha` first (works with a shallow checkout and no persisted credentials), then `git show` if the commit is local. The commit is checked once up front, so a 404 on a path can only mean the file does not exist. Anything unreadable fails the review and the check (no silent fallback to the head, and no `blockOn` is known to soften it). Everything is fetched before the review starts and cached, so a run sees one consistent snapshot. When the PR edits any of these files, the summary lists them under "Configuration changes in this PR": they apply after merge, not to this review, and findings in them are not softened. `local` reads from its `--base` ref the same way; `scan` reads the checked-out default branch. The code, the graph and the tools still see the head. | `config_source.py`, `config.load_config_from`, `cli._github_run` |
| Learning from reactions | Past comments from this reviewer, with their reactions, go into the prompt as "rejected" and "valued" examples. A new finding similar to three or more downvoted ones is suppressed, except security, data loss, concurrency, resource leaks, null safety and input validation. Similarity is word overlap rather than embeddings. | `learning.py` |
| Per-PR memory on re-reviews | Before a re-review, the reviewer collects its own earlier findings on this PR: inline comments (found by their hidden marker) plus the "outside the changed lines" list in its previous summary, up to the 30 most recent. Each carries file, line, severity, title, whether its thread is resolved (read with GraphQL), whether it has more thumbs-down than thumbs-up, and up to 3 short human replies (200 characters each, emails and token-like strings removed, treated as untrusted). They go into a "Previous findings on this PR" section of the PR block, not the cached repository block. The model reports each one as fixed, still present or dismissed in `previous_findings`, and is told not to re-raise resolved, downvoted or answered findings unless its new evidence is stronger. The summary gets a "Since last review" block: fixed, dismissed, still open (up to 10 listed) and new findings. A finding the model does not report on stays open, unless its thread was already settled. Comment bodies are never logged. | `memory.py`, `github.resolved_comment_ids`, `cli._prior_findings` |
| Triggers | `autoReview` (`open`, `push`), `triggerOnDrafts`, author and label filters, and a re-review request: adding the `re-review` label (name set by `reReviewLabel`) to the PR. The reviewer removes the label when the run ends, so it can be added again. A label is used instead of an `@mention` comment because an `issue_comment` workflow starts a skipped Actions run on every PR comment; comment handling stays in code for older workflows (it needs write access, because a review spends API credit). The example workflow also skips drafts and docs-only PRs (`paths-ignore`). | `cli.decide_trigger` |
| De-duplication | A finding already posted on this PR (same file, similar title) is not posted again. | `reviewer.finalize` |

## Pre-merge checks and merge blocking

| Feature | Behaviour | Where |
|---|---|---|
| Pre-merge checks | A table in the summary: `pr-template` (no model; compares the PR body with `.github/pull_request_template.md`), `declared-scope` (the model compares the description with the diff) and the team's own checks from config, each `off`, `warning` or `error`. The model returns its verdicts in a `checks` array on `submit_review`, within the same single review; a check it skips shows as inconclusive. | `checks.py`, `models.ModelCheck` |
| Merge blocking | `blockOn` (`none`, `P0`, `P1`). After the summary and comments are posted, the CLI exits non-zero when a new or still-open finding (per-PR memory) is at or above it, or an `error`-mode check failed. The `overrideLabel` label skips the block and the summary says so. A failed review (API error, refusal, no result) exits 1 when `blockOn` is set and the override label is absent, and exits 0 otherwise; it also exits 1 when the base settings cannot be read (see "Settings from the base branch"). | `blocking.py`, `cli._exit_code` |
| Prompt for AI agents | A collapsed block on every inline comment and one combined block in the summary, rendered by code from the finding (no schema change, no model call) and scrubbed with the same redaction as human replies in the per-PR memory. | `agent_prompt.py`, `scrub.py` |
| `local --json` | One JSON object on stdout with the summary, findings (with `agent_prompt`), checks and `blocking` plus reasons; non-zero exit when blocking. Meant for coding agents that review their own branch before opening a PR. | `report.py`, `cli.local_main` |

## Scan mode

`simpli-review scan` (action input `mode: scan`) reviews the whole repository at the checked-out commit for
security and data-integrity problems, on a schedule (monthly in the example workflow). It uses the same agent loop, read-only tools, code
graph and context files as a review, with a security prompt and a prioritised repository map (entry points,
authentication and tenancy modules, migrations) as the starting context.

Three controls keep its cost visible and bounded: a token budget, an estimated-cost report (job summary and
`simpli-review-scan.json`/`.md`) and an issue cap. Issues are de-duplicated by a fingerprint of the file path and
the words of the title, and are never closed automatically. See the README's "AI scan" section for the full
description.

| Part | Where |
|---|---|
| Orchestration, budget | `scan.py`, `reviewer.agent_loop` |
| Prioritised map | `scan_map.py` |
| Issues and fingerprints | `scan_issues.py` |
| Report and price table | `scan_report.py`, `pricing.py` |

## Deliberately not built

- **Embeddings and natural-language descriptions of code.** The agent's tools plus the graph stand in for
  semantic search.
- **An auto-written knowledge base.** Instead, `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`,
  `docs/DECISIONS.md` and anything in `files.json` are given to the reviewer as context.
- **Detecting which comments were addressed across PRs.** Within one PR the model judges fixed or still
  present against the new code (see per-PR memory above); across PRs only reactions are learned from.
- **Inferring rules from what human reviewers say.**
- **A swarm with one agent per hypothesis.** This uses one agent loop.
- **Cross-repository context, auto-approve and PR description updates.**
- **Generated unit tests and docstrings.** The reviewer reads and comments; it does not write code for the PR.
- **Autopilot fix commits.** It never pushes. "Prompt for AI agents" hands the fix to a person or an agent that
  has its own review step.
- **Chat commands.** Re-review is a label, for the reasons given under Triggers above.
- **A review-effort estimate and slop detection.**
- **Bundled linters.** A review is for what linters and the repository's own CI cannot see. Run a security
  scanner and your normal CI alongside it.
