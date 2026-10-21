"""Per-repository configuration, read from the repo under review (from the PR's base commit in PR mode;
see config_source.py).

Layout (docs/HOW_IT_WORKS.md):

    .simpli-review/config.json   behaviour, rules, filters
    .simpli-review/rules.md      free-form team rules (appended to instructions)
    .simpli-review/files.json    extra files to always give the reviewer as context

Every field is optional; a repo with no folder gets the defaults.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from simpli_review.config_source import ConfigSourceError, DirSource, FileSource, clean_path
from simpli_review.models import (
    MAX_CHECK_INSTRUCTIONS,
    MAX_CHECK_NAME,
    CheckMode,
    CommentType,
    Finding,
    Severity,
)

CONFIG_DIR = ".simpli-review"
CONFIG_FILES = ("config.json", "rules.md", "files.json")
TEMPLATE_PATH = ".github/pull_request_template.md"
AutoReview = Literal["open", "push"]
BlockOn = Literal["none", "P0", "P1"]
BUILTIN_CHECKS = ("pr-template", "declared-scope")
MAX_CUSTOM_CHECKS = 20  # each one adds up to 1000 characters of instructions to the prompt
DEFAULT_CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "docs/DECISIONS.md")
DEFAULT_IGNORE = "\n".join(
    (
        "*.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "uv.lock",
        "*.min.js",
        "*.map",
        "dist/**",
        "build/**",
        "node_modules/**",
        ".venv/**",
        "*.svg",
        "*.png",
        "*.jpg",
        "*.pdf",
    )
)


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Rule(_Model):
    id: str
    rule: str
    scope: list[str] = Field(default_factory=lambda: ["**"])
    severity: Literal["low", "medium", "high"] = "medium"
    enabled: bool = True

    def applies_to(self, path: str) -> bool:
        return any(match_glob(path, g) for g in self.scope)


class ContextFile(_Model):
    path: str
    description: str = ""


class CustomCheck(_Model):
    """A team-defined pre-merge check the model evaluates against the diff and the code."""

    name: str = Field(min_length=1, max_length=MAX_CHECK_NAME)
    instructions: str = Field(min_length=1, max_length=MAX_CHECK_INSTRUCTIONS)
    mode: CheckMode = "warning"


class BuiltinChecks(_Model):
    """Mode of each built-in check; "off" disables it."""

    pr_template: CheckMode = Field(default="warning", alias="pr-template")
    declared_scope: CheckMode = Field(default="warning", alias="declared-scope")

    def mode_of(self, name: str) -> CheckMode:
        return self.pr_template if name == "pr-template" else self.declared_scope


EFFORTS = ("low", "medium", "high", "xhigh", "max")
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ScanConfig(_Model):
    """`scan` block: limits for the scheduled full-repository scan (`simpli-review scan`)."""

    max_turns: int = Field(default=60, ge=1, le=500, alias="maxTurns")
    effort: Effort = "high"
    # Budget for the whole run: input + cache read + cache write + output tokens, summed over every turn.
    max_input_tokens: int = Field(default=3_000_000, ge=10_000, alias="maxInputTokens")
    max_issues: int = Field(default=5, ge=0, le=50, alias="maxIssues")
    # Issues are opened for findings at or above this severity (P0 is the most severe).
    min_severity: Literal["P0", "P1", "P2"] = Field(default="P1", alias="minSeverity")


class VerifyConfig(_Model):
    """`verify` block: a second, fresh-context check of each serious finding before it is posted.

    Each candidate goes to its own short agent run (read-only tools, no access to the reviewer's
    conversation) that keeps it, lowers its severity one step, or drops it. A verification that fails or
    runs out of time keeps the finding, marked unverified; nothing is ever dropped on an error.
    """

    enabled: bool = True
    model: str = Field(default="claude-sonnet-5-5", pattern=r"^[A-Za-z0-9._:/@-]{1,100}$")
    effort: Effort = "medium"
    severities: list[Literal["P0", "P1", "P2"]] = Field(
        default_factory=lambda: list[Literal["P0", "P1", "P2"]](["P0", "P1"]), min_length=1
    )
    # A finding whose verdict confidence is below this is dropped.
    min_confidence: int = Field(default=3, ge=1, le=5, alias="minConfidence")
    max_turns: int = Field(default=8, ge=1, le=30, alias="maxTurns")
    # Per verification: input + cache read + cache write + output tokens, summed over its turns.
    max_tokens: int = Field(default=250_000, ge=10_000, le=2_000_000, alias="maxTokens")
    timeout_seconds: int = Field(default=240, ge=10, le=1800, alias="timeoutSeconds")
    concurrency: int = Field(default=4, ge=1, le=8)

    @field_validator("severities")
    @classmethod
    def _unique_severities(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("severities must not repeat")
        return v


class Config(_Model):
    # 1 = everything worth saying, 2 = default, 3 = only P0/critical.
    strictness: Literal[1, 2, 3] = 2
    comment_types: list[CommentType] = Field(
        default_factory=lambda: [CommentType.logic, CommentType.syntax, CommentType.style],
        alias="commentTypes",
    )
    instructions: str = ""
    rules: list[Rule] = Field(default_factory=list)
    ignore_patterns: str = Field(default=DEFAULT_IGNORE, alias="ignorePatterns")
    files: list[ContextFile] = Field(default_factory=list)
    # When to review automatically: "open" (opened / reopened / ready for
    # review) and/or "push" (new commits). [] = only on an @mention.
    auto_review: list[AutoReview] = Field(
        default_factory=lambda: list[AutoReview](["open"]), alias="autoReview"
    )
    trigger_on_drafts: bool = Field(default=False, alias="triggerOnDrafts")
    exclude_authors: list[str] = Field(default_factory=list, alias="excludeAuthors")
    labels: list[str] = Field(default_factory=list)
    disabled_labels: list[str] = Field(default_factory=list, alias="disabledLabels")
    max_comments: int = Field(default=15, alias="maxComments")
    min_confidence: int = Field(default=3, alias="minConfidence")
    include_diagram: bool = Field(default=True, alias="includeDiagram")
    learn_from_reactions: bool = Field(default=True, alias="learnFromReactions")
    # Adding this label to a PR asks for a re-review; the reviewer removes it when the run ends.
    re_review_label: str = Field(default="re-review", alias="reReviewLabel")
    # Pre-merge checks: two built in, plus the team's own (evaluated by the model).
    builtin_checks: BuiltinChecks = Field(default_factory=BuiltinChecks, alias="builtinChecks")
    checks: list[CustomCheck] = Field(default_factory=list, max_length=MAX_CUSTOM_CHECKS)
    # Merge blocking: fail the check when a new or still-open finding is at or above this severity.
    block_on: BlockOn = Field(default="none", alias="blockOn")
    # A PR with this label is never blocked; the summary says the block was overridden.
    override_label: str = Field(default="review-override", alias="overrideLabel")
    scan: ScanConfig = Field(default_factory=ScanConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)

    @field_validator("checks")
    @classmethod
    def _unique_check_names(cls, checks: list[CustomCheck]) -> list[CustomCheck]:
        seen: set[str] = set(BUILTIN_CHECKS)
        for c in checks:
            key = c.name.strip().lower()
            if key in seen:
                raise ValueError(f"check name {c.name!r} is duplicated or is a built-in check name")
            seen.add(key)
        return checks

    def ignored(self, path: str) -> bool:
        patterns = [p.strip() for p in self.ignore_patterns.splitlines() if p.strip()]
        return any(match_glob(path, p) for p in patterns if not p.startswith("#"))

    def rules_for(self, paths: list[str]) -> list[Rule]:
        return [r for r in self.rules if r.enabled and any(r.applies_to(p) for p in paths)]

    def keeps(self, finding: Finding) -> bool:
        """Strictness and type filters (`strictness` and `commentTypes`)."""
        if finding.type not in self.comment_types:
            return False
        if finding.confidence < self.min_confidence:
            return False
        if self.strictness == 3:
            return finding.severity == Severity.P0
        if self.strictness == 2:
            return finding.severity in (Severity.P0, Severity.P1) or finding.type != CommentType.style
        return True

    def should_review(self, *, draft: bool, author: str, labels: list[str]) -> tuple[bool, str]:
        if draft and not self.trigger_on_drafts:
            return False, "draft PR (set triggerOnDrafts to review drafts)"
        if author in self.exclude_authors:
            return False, f"author {author} is excluded"
        if self.disabled_labels and set(labels) & set(self.disabled_labels):
            return False, "PR has a label that disables review"
        if self.labels and not set(labels) & set(self.labels):
            return False, "PR has none of the labels that enable review"
        return True, ""


def match_glob(path: str, pattern: str) -> bool:
    """gitignore-ish: `**` crosses directories; a pattern without a leading
    "/" matches at any depth ("node_modules/**" also hits web/node_modules/x)."""
    anchored = pattern.strip().startswith("/")
    pattern = pattern.strip().lstrip("/")
    if pattern.endswith("/"):
        pattern += "**"
    parts = path.split("/")
    candidates = [path] if anchored else ["/".join(parts[i:]) for i in range(len(parts))]
    for candidate in candidates:
        if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(candidate, pattern.replace("**/", "")):
            return True
    return False


def load_config(repo_root: Path) -> Config:
    """Settings from the files on disk. For a pull request use load_config_from(open_base_source(...))."""
    return load_config_from(DirSource(repo_root))


def load_config_from(source: FileSource) -> Config:
    raw: dict[str, object] = {}
    text = source.read(f"{CONFIG_DIR}/config.json")
    if text is not None:
        raw = json.loads(text)
    config = Config.model_validate(raw)
    extra = source.read(f"{CONFIG_DIR}/rules.md")
    if extra is not None and extra.strip():
        config.instructions = "\n\n".join(p for p in (config.instructions, extra.strip()) if p)
    listed_text = source.read(f"{CONFIG_DIR}/files.json")
    if listed_text is not None:
        listed = json.loads(listed_text)
        config.files += [ContextFile.model_validate(f) for f in listed.get("files", [])]
    known = {f.path for f in config.files}
    config.files += [
        ContextFile(path=p, description="project conventions")
        for p in DEFAULT_CONTEXT_FILES
        if p not in known and _exists(source, p)
    ]
    return config


def _exists(source: FileSource, path: str) -> bool:
    try:
        return source.read(path) is not None
    except ConfigSourceError:
        return False


def controlling_paths(config: Config) -> list[str]:
    """Every file whose content steers a review. A PR that edits one of these changes how the
    repository is reviewed after it merges, but not how this PR is reviewed."""
    paths = [f"{CONFIG_DIR}/{name}" for name in CONFIG_FILES]
    paths += [*DEFAULT_CONTEXT_FILES, TEMPLATE_PATH]
    paths += [cf.path for cf in config.files]
    out: list[str] = []
    for p in paths:
        cleaned = clean_path(p)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def config_changes(config: Config, changed: Iterable[str | None]) -> list[str]:
    """The controlling files among the paths a PR touches (new, old and renamed names), in a stable order."""
    touched = {c for c in (clean_path(p) for p in changed if p) if c}
    return [p for p in controlling_paths(config) if p in touched]
