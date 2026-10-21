> Part of **Simpli Gatehouse**, the review and security gates from Simpli Software.
> Its sibling, [simpli-software/simpli-guard](https://github.com/simpli-software/simpli-guard), scans every pull request for security problems.

# simpli-review

A pull-request reviewer for GitHub with knowledge of the whole codebase, built on Claude. It runs as a
GitHub Action, so there is no server to host, and it needs one Anthropic API key that you bring yourself.
[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) describes the design in detail. Built by Simpli Software.

On each PR it:

1. Builds a code graph of the repository at the PR's head: functions, classes, calls and imports.
2. Gives Claude the diff plus graph context for every changed function (callers, callees, similar
   functions, importers), your team rules, and your convention docs.
3. Lets Claude investigate with read-only tools (read, search, find definition, find callers, git
   history) and confirm each finding against the code before it reports it.
4. Filters the findings by strictness, confidence and your team's past 👍/👎 reactions, and removes any
   already posted.
5. Posts **one summary comment**, updated in place on later runs. It has a summary, a 0–5 confidence
   score, a table of pre-merge checks, a file table and a diagram. **Inline comments** carry P0/P1/P2
   badges, suggestion blocks you can apply in one click, and a "Prompt for AI agents" block.
6. Optionally fails the check when a finding or check is bad enough to block the merge.

## Add it to a repository

### Quick setup

From the root of the repository you want reviewed (you need `python` 3.12 or later and, for the extras, the
[GitHub CLI](https://cli.github.com) signed in with `gh auth login`):

```bash
pipx install git+https://github.com/simpli-software/simpli-review   # or: uvx --from git+https://github.com/simpli-software/simpli-review simpli-review init
simpli-review init
```

Prefer a form? The [Simpli Gatehouse config builder](https://simpli-software.github.io/simpli-gatehouse/) builds the same files in your browser, pinned to the latest release.

It asks a few questions and Enter accepts the default shown in brackets:

| Question | Default |
|---|---|
| Review heading name | `Code review` |
| Strictness 1-3 | `2` |
| Block merges on `none`, `P0` or `P1` | `P0` |
| Built-in checks `pr-template` and `declared-scope`: `off`, `warning` or `error` | `warning` |
| Add the monthly AI scan (first Monday of the month) | yes |
| Scan budget, in tokens | `3,000,000` |
| Ignore patterns | the built-in list plus lockfiles and build directories found in the repository |
| Extra instructions, one line | none |

Then it writes `.github/workflows/code-review.yml`, `.github/workflows/ai-scan.yml` (if you chose the scan)
and `.simpli-review/config.json` (only the fields that differ from the defaults). The `simpli-review` action is pinned by
commit hash and release tag, looked up through GitHub's public API for the latest release (a token is sent only if
`GITHUB_TOKEN` or `GH_TOKEN` is set). If the lookup fails it writes `<commit-sha> # v1.x` and prints how to fill it in. With
`gh` available it creates the `re-review`, `review-override` and `no-review` labels (existing ones are left alone;
without `gh` it prints the commands) and checks that the `ANTHROPIC_API_KEY` secret exists. It never reads, prints or
sets a secret value: if the secret is missing it prints `gh secret set ANTHROPIC_API_KEY` for you to run. Finally it prints a
checklist. Only maintainers should add the `review-override` label, since it lets a PR through a blocking review.

| Flag | Meaning |
|---|---|
| `--yes`, `--non-interactive` | Take every default without asking. |
| `--force` | Overwrite files that already exist. Without it nothing is overwritten: a short diff is shown and the file is skipped. |
| `--dry-run` | Print what would be written; change nothing. |
| `--no-labels` | Do not create labels (and do not write `disabledLabels`, which makes the `no-review` label work). |
| `--ref owner/repo@sha` | Pin this exact build instead of the latest release. |
| `--repo PATH` | Repository root, default the current directory. |

The manual steps follow, if you prefer to do it by hand.

### Manual setup

1. Add a secret named `ANTHROPIC_API_KEY`, at repository or organization level.
2. Copy [examples/code-review.yml](examples/code-review.yml) to `.github/workflows/code-review.yml`,
   set `name:` to the heading you want on the summary comment, and replace `<commit-sha>` in the
   `uses:` line (`simpli-software/simpli-review`) with the full commit hash of the release you
   want. Keep the version in the trailing comment. Pin by hash, not by tag, so a moved tag cannot change
   what runs in your pipeline.
3. Create a label named `re-review` in the repository (any colour). Open a PR.
4. To ask for another review, add the `re-review` label to the PR. The reviewer removes the label when
   it finishes (also if the review fails), so you can add it again for the next round.

Only people who can label a PR (triage access or above) can ask for a review this way, and each review
spends API credit. Labels replace the old comment trigger: an `issue_comment` workflow starts a skipped
Actions run on every comment in every PR, which adds up. The template also skips draft PRs and PRs that
only change `*.md` or `docs/**` files (a `re-review` label on such a PR does nothing; remove
`paths-ignore` from the workflow if you want those reviewed).

To use another label name, set `reReviewLabel` in `.simpli-review/config.json` and change the name in the
workflow's `if:` to match. The `mention` input and `issue_comment` handling still work for repositories
that kept the old workflow, but the template no longer uses them.

## Configure it (optional)

Create `.simpli-review/` in the reviewed repository. Every field is optional.

`config.json`:

```json
{
  "strictness": 2,
  "commentTypes": ["logic", "syntax", "style"],
  "autoReview": ["open"],
  "triggerOnDrafts": false,
  "excludeAuthors": ["dependabot[bot]"],
  "labels": [],
  "disabledLabels": ["no-review"],
  "ignorePatterns": "*.lock\ndist/**\nmigrations/versions/*.py",
  "maxComments": 15,
  "minConfidence": 3,
  "includeDiagram": true,
  "learnFromReactions": true,
  "blockOn": "P1",
  "overrideLabel": "review-override",
  "builtinChecks": {"pr-template": "warning", "declared-scope": "warning"},
  "checks": [
    {"name": "tests-added", "instructions": "Every new branch in backend code has a test.", "mode": "error"}
  ],
  "instructions": "We are a FastAPI + React codebase. Prices are stored as integer cents.",
  "rules": [
    {"id": "repository-layer", "rule": "Database access goes through the repository layer, never directly from route handlers.", "scope": ["backend/**/*.py"], "severity": "high"},
    {"id": "no-pii-logs", "rule": "Never log emails, phones, tokens or payloads.", "severity": "high"}
  ]
}
```

| Field | Meaning |
|---|---|
| `strictness` | 1 = everything worth saying. 2 = default, drops P2 style comments. 3 = P0 only. |
| `commentTypes` | Which types to keep: `logic`, `syntax`, `style`. |
| `autoReview` | `open` reviews on opened, reopened or ready for review. `push` also reviews on new commits. `[]` means only on mention. |
| `minConfidence` | The model rates each finding 1–5; anything lower is dropped. |
| `maxComments` | Cap on inline comments. Extra findings are listed in the summary. |
| `reReviewLabel` | PR label that asks for a re-review. Default `re-review`. The reviewer removes it after the run. |
| `learnFromReactions` | Use past 👍/👎 on this reviewer's comments. See below. |
| `blockOn` | `none` (default), `P0` or `P1`. Fail the Actions check when a new or still-open finding is at or above this severity. See [Block merges](#block-merges). |
| `overrideLabel` | PR label that skips blocking. Default `review-override`. |
| `builtinChecks` | Mode of each built-in pre-merge check: `{"pr-template": "warning", "declared-scope": "warning"}`. Each is `off`, `warning` (default) or `error`. |
| `scan` | Limits for the scheduled full-repository scan: `maxTurns`, `effort`, `maxInputTokens`, `maxIssues`, `minSeverity`. See [AI scan](#ai-scan-scheduled-full-repository-security-scan). |
| `verify` | Second check of each serious finding before it is posted (on by default; `{"enabled": false}` turns it off). Each P0/P1 finding goes to a fresh, short run (default `claude-sonnet-5-5`, effort `medium`) that sees only that finding, the diff of its file, the repository context from the base branch and the read-only tools, and keeps it, lowers it one severity or drops it. With `blockOn` set, a finding at or above it is never dropped or lowered below it: the verifier can only dispute it, the comment shows why, and it still blocks until someone adds the override label. Options: `model`, `effort`, `severities` (default `["P0", "P1"]`), `minConfidence` (verdicts below it drop the finding; default 3), `maxTurns` (8), `maxTokens` (250000 per finding), `timeoutSeconds` (240), `concurrency` (4). A check that fails or times out keeps the finding, unverified, and the summary says so. Dropped findings are listed, collapsed, in the summary with the verifier's reason; they are not posted and do not count as dismissed. When anything was dropped or downgraded, the summary and confidence score are rewritten to match the findings that remain. The job log prints the reviewer's and the verifier's estimated cost separately. |
| `checks` | Your own pre-merge checks, up to 20: `{"name", "instructions", "mode"}`. Name up to 50 characters, instructions up to 1000, mode `off`, `warning` (default) or `error`. See [Pre-merge checks](#pre-merge-checks). |

- `rules.md`: free-form team rules, appended to `instructions`.
- `files.json`: `{"files": [{"path": "docs/ARCHITECTURE.md", "description": "..."}]}`. These files are
  always given as context. `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` and `docs/DECISIONS.md` are
  included automatically when present.

### Settings come from the base branch

A pull request can edit any file in it, including these. If the reviewer read its settings from the PR's own
commit, a PR could set `blockOn` to `none`, add `**` to `ignorePatterns`, rewrite the rules or plant
instructions in `CLAUDE.md`, and then pass its own review. So in PR mode the reviewer reads all of the
following from the PR's **base commit** (the tip of the branch the PR merges into), never from the PR:

- `.simpli-review/config.json`, `rules.md` and `files.json`
- the context files: `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/DECISIONS.md` and every file listed in `files.json`
- `.github/pull_request_template.md` (the `pr-template` check)

The files are fetched with the GitHub contents API at the base commit, using the workflow token (read access to
contents is enough, and it works with a shallow checkout and `persist-credentials: false`). If the API fails,
the reviewer uses `git show` when the base commit is in the checkout. If neither works, the review does not run
and the check **fails** with the reason in the summary and the job log; it never falls back to the PR's copy.
Re-run the job to try again. The same happens when the base settings are not valid JSON or fail validation.

If a PR changes any of those files, the summary gets a "Configuration changes in this PR" section listing
them. The changes are reviewed like any other code (the settings that apply to the review can still ignore
them), and they take effect once the PR is merged, so the next PR is reviewed with the new settings. The
code under review, the code graph and the tools still see the PR's head.

Scan mode reads the checked-out default branch, which is already the trusted copy. The `local` command reads
from its `--base` ref (see [Review a branch locally](#review-a-branch-locally)).

## How it learns

React 👎 on a comment that wasn't useful and 👍 on one that was. On later reviews:

- Recent 👍 and 👎 examples are shown to the model.
- A new finding similar to three or more 👎 comments is suppressed. Similarity is word overlap, so this
  needs no extra service.
- Security, data-loss, concurrency, resource-leak, null-safety and input-validation findings are never
  suppressed.

## Pre-merge checks

The summary comment has a table of checks, each with a result (pass, fail or inconclusive), its mode and a
one-line reason.

| Check | How it is decided |
|---|---|
| `pr-template` | No model. If the reviewed repository has `.github/pull_request_template.md`, every `##` heading in it must appear in the PR body with content that is not the template's own placeholder or comment text, and not just `TODO` or `TBD`. `None` and `n/a` count as filled. The reason names the missing and empty headings. Without a template the row is left out. |
| `declared-scope` | The model compares the PR description with the diff and fails when the diff has changes the description does not declare: new or changed migrations, new environment or config variables, new or changed public or unauthenticated endpoints, and changes clearly outside the stated purpose. |
| your own (`checks`) | The model evaluates each against the diff and the code with the same read-only tools as the rest of the review. It must say `inconclusive` rather than guess. A check the model does not report on shows as inconclusive. |

Modes: `off` does not run the check, `warning` shows it in the table, `error` also blocks the merge when it fails
(see below). A check that is `inconclusive` never blocks. Built-ins default to `warning`; set them in
`builtinChecks`. Check names are unique (case-insensitive) and cannot reuse a built-in name. A bad value in
`checks` stops the run with a config error, like any other invalid field.

Model-evaluated checks add one `checks` array to the `submit_review` tool, validated like the rest of the review.
They cost no extra turns; they are part of the one review.

## Block merges

With `blockOn` set to `P0` or `P1`, the reviewer exits non-zero, after it has posted the summary and the inline
comments, when either is true:

- a finding at or above that severity is open. That means newly posted (inline or in the summary) or an earlier
  finding on this PR that the per-PR memory still counts as open: the model said `still_present`, or it did
  not re-check it and the thread is not resolved or downvoted. Findings dropped by `strictness`,
  `minConfidence` or team feedback never block.
- a check in `error` mode failed.

The job and its check go red, and the summary opens with "Merge blocked" and the reasons. Make the check a
required status check in branch protection to stop the merge. When `blockOn` is set, a review that cannot
finish (API error such as an empty credit balance, refusal, no result) also turns the check red, so a PR
never passes without a review; the summary and the job log carry the API's own error message. With the
default `blockOn: "none"`, or with the override label on the PR, a failed review is a warning and a green
job. The exception is a review that cannot read the [base-branch settings](#settings-come-from-the-base-branch):
it does not yet know `blockOn` or the override label, so it always fails the check. `--dry-run` never fails the job.

To let one PR through, add the `overrideLabel` label (default `review-override`). The next review does not
fail, and the summary says the block was overridden and by which label. The label is read from the PR the
reviewer fetches at the start of the run, so the example workflow's permissions do not change. Adding the
label does not start a review (the example workflow only reacts to `re-review`); add `re-review` afterwards
to refresh the check.

## Prompt for AI agents

Every inline comment ends with a collapsed "Prompt for AI agents" block, and the summary has one combined block for
every open finding (new ones, and earlier ones that are still open). It is plain text built from the finding by
code, not by the model: where (`In <path> around line <n>`), severity and title, the explanation, the
suggested fix when there is one, and a closing "Then verify" step. Copy it into a coding agent. Emails,
token-like strings and HTML comments are removed first, and each block is capped in length.

## AI scan: scheduled full-repository security scan

`mode: scan` reviews the whole repository at the checked-out commit instead of a pull request: no diff, a
security and data-integrity pass over the code as it stands. It is meant to run on a schedule (the example: the first Monday of each month), plus by
hand, and it opens a few GitHub issues for the worst findings. The default stays PR review.

It reuses the review machinery (agent loop, read-only tools, code graph, prompt caching, `rules`,
`instructions`, `CLAUDE.md` and the other context files) with its own prompt. It looks for: authentication,
sessions and RBAC; tenant isolation and row-level security; injection (SQL, command, template, SSRF, path);
secrets handling; personal data in logs; payment and webhook verification; public or unauthenticated
endpoints; unsafe deserialization; missing input validation; and races on money or state. The model starts
from a prioritised map built from the code graph, with no extra model call: risky files first (auth, tenancy,
webhooks, payments, migrations, routes, workers), the entry points it found (routes, handlers, workers,
webhooks) and every indexed file. Every finding must cite the `file:line` evidence it read and a 1-5
confidence. Findings below `minConfidence`, on a file or line that does not exist, or without `file:line`
evidence are dropped. Secret-looking values are removed from everything the scan writes.

### Set it up

1. Add the `ANTHROPIC_API_KEY` secret (as for reviews).
2. Copy [examples/ai-scan.yml](examples/ai-scan.yml) to `.github/workflows/ai-scan.yml`. It checks every Monday at
   09:17 UTC and runs on the first Monday of the month, and from **Actions > AI scan > Run workflow**, where `effort` and `max-turns` can be overridden.
   Pin the `simpli-review` step (`<commit-sha>` in the template) to the commit hash of a
   release that includes scan mode and keep the version in the trailing comment. The workflow needs only `contents: read` and `issues: write`.
3. Run it by hand once and read the spend (below) before you keep the schedule.

The `ai-scan` label is created on the first run that opens an issue.

### Configure it

In `.simpli-review/config.json` (every field optional; the values shown are the defaults):

```json
{"scan": {"maxTurns": 60, "effort": "high", "maxInputTokens": 3000000, "maxIssues": 5, "minSeverity": "P1"}}
```

| Field | Meaning |
|---|---|
| `maxTurns` | Most investigation turns. The `max-turns` action input overrides it. |
| `effort` | `low`, `medium`, `high`, `xhigh` or `max`. The `effort` action input overrides it. |
| `maxInputTokens` | Token budget for the whole run (see below). |
| `maxIssues` | Most issues opened per run. |
| `minSeverity` | Only findings at this severity or worse (`P0` worst) become issues. |

`minConfidence`, `ignorePatterns`, `rules`, `instructions` and `files.json` apply to scans as they do to reviews.
Use the `model` input to pick a cheaper model; the estimate below knows `claude-opus-5-5` and `claude-sonnet-5-5`.

### Cost controls

- **Token budget.** The run adds up every token the API reports: input, cache read, cache write and output.
  When two more turns the size of the last one would pass `maxInputTokens`, the model is told to submit now
  and not to call another tool. If it has not submitted one turn after that, the scan stops and is reported as
  incomplete (the findings of an unfinished scan are lost, the spend is not). Because the check looks one
  turn ahead, a run normally ends inside the budget; a single turn bigger than the one before it can pass it
  by that turn.
- **Turns and effort.** `maxTurns` (the model is also reminded when 3 turns are left) and `effort` bound the
  work. Lower effort and fewer turns cost less and find less.
- **Time and overlap.** The example job has `timeout-minutes: 45` and a concurrency group, so two scans never
  run at once.
- **Issue cap.** At most `maxIssues` issues per run; the rest are only in the report.

### Read the spend

Every run, including a failed one, prints and writes: model, effort, turns used of the maximum, input, output,
cache-read and cache-write tokens, and an **estimated cost in USD**. The same table is in the job summary
(**Actions > the run > Summary**), in the log, and in the `simpli-review-scan` artifact:
`simpli-review-scan.json` (findings, usage, cost, commit, duration, what happened to each finding) and
`simpli-review-scan.md`. The cost is an estimate: the tokens priced at the list prices in
`src/simpli_review/pricing.py` (source and date are in that file). It does not include discounts or
a different model chosen by the server-side refusal fallback; the Anthropic usage page has the real figure. A
model that is not in the table shows `unknown` instead of a guess. A scan that does not finish makes the job
fail (red) after the report is written, so the schedule tells you.

### Issues

For findings at or above `minSeverity`, the scan opens at most `maxIssues` issues, worst first, labelled
`ai-scan`, titled `[AI scan] <title>`. Each has the severity, links to `file:line` at the scanned commit, the
explanation and evidence, a collapsed "Prompt for AI agents" block (the same renderer as PR reviews) and a hidden
fingerprint: the file path plus the normalised words of the title, with no line numbers.

- A finding whose fingerprint matches an **open** issue, or an issue closed as **not planned**, is skipped. The
  same file with a similar title counts as a match, because the model words one problem differently each week.
  An issue closed as completed does not suppress it: if the problem is back, it gets a new issue.
- Scans never close, comment on or edit issues, because they are not deterministic. A finding seen again adds
  nothing. Close an issue as *not planned* to stop it being raised again.
- Secret values are removed from titles, text and evidence before anything is written, and `@mentions` are
  defused so an issue cannot ping people.

`--dry-run` (the `dry-run` input) opens nothing and writes the report with what it would have opened.

## Review a branch locally

```bash
uv venv && uv pip install -e .
ANTHROPIC_API_KEY=... simpli-review local --base origin/main
```

This prints the summary and the inline comments. It doesn't touch GitHub. The PR-template check needs a PR
body, so it is skipped here; the other checks and `blockOn` apply, and the command exits non-zero when the review
would block.

As in PR mode, the settings, rules and context files are read from the `--base` ref with `git show`, not from your
branch or working tree, so a branch cannot loosen its own review. To try a settings change, merge it into the
base first. If the ref or the settings cannot be read the command stops with exit code 2.

For agents that review their own branch before opening a PR, add `--json`:

```bash
ANTHROPIC_API_KEY=... simpli-review local --base origin/main --json > review.json
```

Stdout is exactly one JSON object; progress and token usage go to stderr. Keys: `summary`, `confidence_score`,
`confidence_reason`, `findings` (each with `severity`, `type`, `category`, `path`, `line`, `end_line`, `title`,
`body`, `suggestion`, `confidence`, `placement` and `agent_prompt`), `checks` (`name`, `result`, `mode`,
`reason`), `blocking` (true or false), `reasons` (why it blocks) and `agent_prompt` (all open findings in one
prompt), `config_source` (where the settings came from, for example `origin/main@1a2b3c4`) and
`config_changes` (settings or context files your branch edits; they are not applied to this review). The exit
code is non-zero when `blocking` is true. If the review cannot run, the object is
`{"error": "...", "blocking": false}` and the exit code is 1 (review failed) or 2 (no merge base).

## Cost and limits

You bring your own Anthropic API key, so the only bill is the one Anthropic sends you for the tokens the
reviewer uses. There is no per-seat or per-repository fee.

- **Model:** default `claude-opus-5-5` with `effort: high`. Server-side refusal fallback is on.
- **Turns:** at most 40 investigation turns (scans: see [AI scan](#ai-scan-scheduled-full-repository-security-scan)). Prompt caching keeps re-sent context cheap.
- **What a review costs** depends on PR size and how much the model reads. The job log prints token
  usage per review. Set `effort: medium` or lower `max-turns` to spend less.
- **Large diffs:** above 250 KB are listed but not inlined; the model reads those files with tools
  instead.
- **Indexing:** files over 400 KB, binaries and `.env`-style files are not indexed and can't be read.
- **Language support:** the graph is exact for Python and approximate for JS/TS. Other languages are
  covered by text search only.

## Compared with hosted reviewers

Facts, not a ranking:

- **Where it runs.** In your own GitHub Actions runner. There is no hosted service, dashboard or account.
  Your code goes to the Anthropic API under your own key and nowhere else.
- **What you pay.** Anthropic's token prices, billed to your key, and the Actions minutes. Budget caps and a
  spend report are built in (see [Cost controls](#cost-controls)). There is no seat licence.
- **What it does not have.** No IDE plugin, no web dashboard, no embeddings index of the repository, no
  generated tests or fix commits (it never pushes), and no chat commands. Re-review is a label.
- **Languages.** The code graph is exact for Python and approximate for JavaScript and TypeScript. Other
  languages are covered by text search only.
- **Model.** Claude only.

## Support

Best effort, no SLA. Issues are welcome (bug reports and feature requests, using the templates). To
report a vulnerability, see [SECURITY.md](SECURITY.md) and do not open a public issue.

Pull requests: until 2027-01-18 (90 days after the 2026-10-20 public launch) this project takes issues only, and outside
pull requests are not accepted yet. After that they are accepted with a Developer Certificate of Origin
sign-off on every commit (`git commit -s`, see <https://developercertificate.org>) and no CLA. CI on fork
pull requests runs on `pull_request` without secrets, workflows never use `pull_request_target`, a
maintainer approves CI runs for first-time contributors, and every outside pull request gets a senior
security review before merge, because the action runs inside your CI with your API keys. Details are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Develop

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy src && pytest -q
```

Tests use a scripted fake Claude client, so they make no API calls.

## License

MIT, see [LICENSE](LICENSE). Built by Simpli Software.
