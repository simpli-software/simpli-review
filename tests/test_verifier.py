"""The verifier pass: verdicts, what they do to findings, failure handling, concurrency, config, summary."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from simpli_review import verifier as v
from simpli_review.config import Config, Rule, VerifyConfig, load_config
from simpli_review.diff import parse_patch
from simpli_review.index import RepoIndex
from simpli_review.memory import collect_prior_findings, summary_findings
from simpli_review.models import Finding, ReviewResult, Severity
from simpli_review.render import summary_body
from simpli_review.reviewer import Settings
from simpli_review.tools import Toolbox
from tests.conftest import FakeClient, _Stream, message, text, tool_use

PATCH = "@@ -4,3 +4,3 @@\n def charge(amount):\n-    cents = amount * 100\n+    cents = to_cents(amount)\n     return send(cents)"  # noqa: E501


def _f(title: str = "Negative amounts round the wrong way", severity: str = "P1", **kw: Any) -> Finding:
    base = {"path": "app/billing.py", "line": 5, "end_line": None, "severity": severity, "type": "logic",
            "category": "correctness", "title": title, "body": "Why it fails.", "evidence": "app/money.py:2",
            "suggestion": None, "confidence": 4, "rule_id": None}  # fmt: skip
    return Finding.model_validate({**base, **kw})


def _verdict(
    verdict: str = "keep", confidence: int = 4, reason: str = "read app/money.py:2"
) -> SimpleNamespace:
    payload = {"verdict": verdict, "confidence": confidence, "reason": reason}
    return message(tool_use("v", v.SUBMIT, payload))


def _config(**verify: Any) -> Config:
    return Config.model_validate({"verify": {"enabled": True, **verify}})


def _run_one(
    repo: Path, responses: list[SimpleNamespace], f: Finding | None = None, config: Config | None = None
):
    client = FakeClient(responses)
    cfg = config or _config()
    settings = v.verifier_settings(Settings(), cfg.verify)
    toolbox = Toolbox(repo, RepoIndex(repo))
    checked = v.verify_one(client, settings, f or _f(), parse_patch("app/billing.py", PATCH), cfg, toolbox)
    return checked, client


# ---------- verdicts ----------


def test_verdict_parsing_and_a_tool_call_before_it(repo: Path):
    checked, client = _run_one(
        repo, [message(tool_use("r", "read_file", {"path": "app/money.py"})), _verdict("keep", 5, "ok\nline")]
    )
    assert (checked.status, checked.confidence, checked.reason) == ("kept", 5, "ok line")
    assert checked.finding == _f() and checked.turns == 2
    req = client.requests[0]
    assert req["model"] == "claude-sonnet-5-5" and req["output_config"] == {"effort": "medium"}
    names = [t["name"] for t in req["tools"]]
    assert v.SUBMIT in names and "submit_review" not in names and "read_file" in names
    assert "app/money.py (lines 1-2 of 2)" in client.requests[1]["messages"][-1]["content"][0]["content"]


def test_a_verdict_outside_the_schema_is_sent_back_and_retried(repo: Path):
    bad = message(tool_use("v1", v.SUBMIT, {"verdict": "maybe", "confidence": 9, "reason": "x"}))
    checked, client = _run_one(repo, [bad, _verdict("drop", 1, "already validated at app/api.py:6")])
    assert checked.status == "dropped" and checked.finding is None
    retry = client.requests[1]["messages"][-1]["content"][0]
    assert retry["is_error"] and "schema" in retry["content"]


@pytest.mark.parametrize(
    ("severity", "verdict", "confidence", "status", "after"),
    [
        ("P1", "keep", 4, "kept", "P1"),
        ("P1", "keep", 2, "dropped", None),  # below minConfidence 3
        ("P1", "keep", 3, "kept", "P1"),
        ("P1", "downgrade", 4, "downgraded", "P2"),
        ("P0", "downgrade", 4, "downgraded", "P1"),
        ("P2", "downgrade", 4, "kept", "P2"),  # nothing below P2
        ("P0", "drop", 5, "dropped", None),
    ],
)
def test_apply_verdict(severity, verdict, confidence, status, after):
    got, f = v.apply_verdict(
        _f(severity=severity), v.Verdict(verdict=verdict, confidence=confidence, reason="r"), 3
    )
    assert got == status
    assert (f.severity.value if f else None) == after


def test_the_prompt_holds_the_base_context_the_finding_and_its_diff(repo: Path):
    from simpli_review.config_source import DirSource
    from simpli_review.reviewer import repo_context_parts

    cfg = Config.model_validate({"rules": [{"id": "money", "rule": "Use integer cents"}]})
    cfg.files = load_config(repo).files  # CLAUDE.md from the fixture repo
    context = (
        "# Repository context"
        + chr(10) * 2
        + (chr(10) * 2).join(repo_context_parts(cfg, repo, ["app/billing.py"], DirSource(repo)))
    )
    f = _f(rule_id="money", end_line=6)
    first, second = v.build_prompt(f, parse_patch("app/billing.py", PATCH), cfg, context)
    assert first["text"] == context and "Money is integer cents." in first["text"]
    text_ = second["text"]
    assert f.title in text_ and "line 5-6" in text_ and "Severity: P1" in text_
    assert "to_cents(amount)" in text_  # the annotated diff
    assert "[money]" in text_ and "Use integer cents" in text_
    assert f.evidence not in text_  # the reviewer's own reasoning trail is not handed over
    (only,) = v.build_prompt(_f(path="app/api.py"), None, Config())
    assert "does not change this file" in only["text"]


def test_the_verifier_prompt_is_generic():
    # No wording from the benchmark cases or their traps (evals/cases): the prompt must not be tuned to them.
    low = v.VERIFY_SYSTEM.lower()
    for word in ("tenant", "rls", "row level", "webhook", "stripe", "f-string", "subscription", "dotfile",
                 "invoice", "sms", "float", "cents", "email", "replay", "idempot"):  # fmt: skip
        assert word not in low, word


# ---------- failures keep the finding ----------


class _Boom:
    def __init__(self, exc: Exception) -> None:
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.exc = exc

    def _stream(self, **_kw: Any) -> Any:
        raise self.exc


def test_an_api_error_keeps_the_finding_unverified(repo: Path, caplog):
    import anthropic
    import httpx

    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.invalid"))
    cfg = _config()
    checked = v.verify_one(_Boom(err), v.verifier_settings(Settings(), cfg.verify), _f(), None, cfg,
                           Toolbox(repo, RepoIndex(repo)))  # fmt: skip
    assert (checked.status, checked.finding, checked.confidence) == ("unverified", _f(), None)
    assert checked.reason == "APIConnectionError"
    assert "kept, unverified" in caplog.text and _f().title not in caplog.text


def test_running_out_of_turns_or_answering_in_text_keeps_the_finding(repo: Path):
    read = message(tool_use("r", "read_file", {"path": "app/money.py"}))
    checked, _ = _run_one(repo, [read, read], config=_config(maxTurns=2))
    assert checked.status == "unverified" and checked.finding == _f() and checked.usage["input"] == 200
    checked, _ = _run_one(repo, [message(text("looks fine"), stop="end_turn")] * 2)
    assert checked.status == "unverified"


def test_a_timeout_keeps_the_finding(repo: Path, monkeypatch):
    clock = iter([0.0, 0.0, 1000.0, 1000.0])
    monkeypatch.setattr(v, "_now", lambda: next(clock))
    read = message(tool_use("r", "read_file", {"path": "app/money.py"}))
    checked, client = _run_one(repo, [read, _verdict("drop", 1)], config=_config(timeoutSeconds=10))
    assert (checked.status, checked.reason, len(client.requests)) == ("unverified", "timed out", 1)


# ---------- the whole pass ----------


class _Router:
    """Answers each verification by the finding title in its prompt; records how many run at once."""

    def __init__(self, verdicts: dict[str, dict[str, Any]], delay: float = 0.0) -> None:
        self.verdicts, self.delay = verdicts, delay
        self.lock = threading.Lock()
        self.active = self.peak = self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw: Any) -> _Stream:
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            prompt = " ".join(b["text"] for b in kw["messages"][0]["content"])
            title = next(t for t in self.verdicts if f"Title: {t}\n" in prompt)
            return _Stream(message(tool_use("v", v.SUBMIT, self.verdicts[title])))
        finally:
            with self.lock:
                self.active -= 1


def _pass(repo: Path, client: Any, findings: list[Finding], config: Config, **kw: Any):
    idx = RepoIndex(repo)
    return v.verify_findings(client, Settings(), findings, patches=[parse_patch("app/billing.py", PATCH)],
                             config=config, make_toolbox=lambda: Toolbox(repo, idx), **kw)  # fmt: skip


def test_verify_findings_applies_verdicts_in_order_and_skips_what_it_should(repo: Path):
    keep = {"verdict": "keep", "confidence": 4, "reason": "r"}
    client = _Router({
        "a": keep, "b": {"verdict": "drop", "confidence": 1, "reason": "handled at app/api.py:6"},
        "c": {"verdict": "downgrade", "confidence": 4, "reason": "edge case only"}, "skip": keep,
    })  # fmt: skip
    findings = [_f("a"), _f("b"), _f("p2 note", "P2"), _f("c", "P0"), _f("skip")]
    out, ver = _pass(repo, client, findings, _config(), should_verify=lambda f: f.title != "skip")
    assert [(f.title, f.severity.value) for f in out] == [("a", "P1"), ("p2 note", "P2"), ("c", "P1"),
                                                          ("skip", "P1")]  # fmt: skip
    assert client.calls == 3  # not the P2, not the one should_verify refused
    assert ver.counts() == {"kept": 1, "downgraded": 1, "dropped": 1, "disputed": 0, "unverified": 0}
    assert ver.usage["input"] == 300 and ver.model == "claude-sonnet-5-5"
    assert [c.original.title for c in ver.by_status("dropped")] == ["b"]


def test_concurrency_is_bounded(repo: Path):
    titles = [f"finding {i}" for i in range(6)]
    client = _Router({t: {"verdict": "keep", "confidence": 4, "reason": "r"} for t in titles}, delay=0.15)
    out, ver = _pass(repo, client, [_f(t) for t in titles], _config(concurrency=2))
    assert client.peak == 2 and len(out) == 6 and ver.counts()["kept"] == 6
    client1 = _Router({t: {"verdict": "keep", "confidence": 4, "reason": "r"} for t in titles}, delay=0.02)
    _pass(repo, client1, [_f(t) for t in titles], _config(concurrency=1))
    assert client1.peak == 1


def test_severities_setting_chooses_what_is_checked(repo: Path):
    client = _Router({t: {"verdict": "drop", "confidence": 1, "reason": "r"} for t in ("x", "y")})
    out, _ = _pass(repo, client, [_f("x", "P0"), _f("y", "P1")], _config(severities=["P0"]))
    assert [f.title for f in out] == ["y"] and client.calls == 1


# ---------- config ----------


def test_verify_config_defaults_and_validation():
    d = Config().verify
    assert (d.enabled, d.model, d.severities, d.min_confidence, d.max_turns, d.concurrency) == (
        True, "claude-sonnet-5-5", ["P0", "P1"], 3, 8, 4,
    )  # fmt: skip
    c = Config.model_validate({"verify": {"enabled": True, "model": "claude-opus-5-5", "minConfidence": 4,
                                          "maxTurns": 5, "severities": ["P0"]}})  # fmt: skip
    assert (c.verify.model, c.verify.min_confidence, c.verify.max_turns, c.verify.severities) == (
        "claude-opus-5-5", 4, 5, ["P0"],
    )  # fmt: skip
    for bad in ({"minConfidence": 0}, {"minConfidence": 6}, {"maxTurns": 0}, {"severities": []},
                {"severities": ["P3"]}, {"severities": ["P1", "P1"]}, {"concurrency": 9}, {"model": "a b"},
                {"model": ""}, {"effort": "huge"}, {"maxTokens": 5}, {"timeoutSeconds": 1}):  # fmt: skip
        with pytest.raises(ValidationError):
            VerifyConfig.model_validate(bad)


def test_verify_config_is_read_from_the_config_file(repo: Path):
    (repo / ".simpli-review").mkdir()
    (repo / ".simpli-review" / "config.json").write_text(json.dumps({"verify": {"enabled": True}}))
    assert load_config(repo).verify.enabled is True


# ---------- the summary ----------


def _verification() -> v.Verification:
    dropped = _f("Ghost bug that is handled", path="app/api.py")
    lowered = _f("Overstated issue", "P0")
    return v.Verification("claude-sonnet-5-5", [
        v.Checked(dropped, "dropped", None, 1, "checked at app/api.py:6"),
        v.Checked(lowered, "downgraded", lowered.model_copy(update={"severity": Severity.P1}), 4, "rare"),
        v.Checked(_f("Unchecked"), "unverified", _f("Unchecked"), None, "timed out"),
        v.Checked(_f("Fine one"), "kept", _f("Fine one"), 5, "confirmed"),
    ])  # fmt: skip


def test_the_summary_lists_dropped_candidates_collapsed_and_memory_ignores_them():
    result = ReviewResult(summary="s", confidence_score=3, confidence_reason="r", files=[], findings=[])
    far = _f("Far away problem", path="b.py").model_copy(update={"line": 90})
    out = summary_body(name="N", result=result, posted=[], unplaced=[far], suppressed=0, head_sha="abcdef1",
                       files_reviewed=1, include_diagram=False, review_count=1, mention="@n",
                       verification=_verification())  # fmt: skip
    assert "<details><summary>1 candidate dropped by the verifier, 1 downgraded</summary>" in out
    assert "Ghost bug that is handled. Reason: checked at app/api.py:6" in out
    assert "Downgraded P0 to P1" in out and "1 finding could not be verified and was kept" in out
    assert "Fine one" not in out  # kept findings are posted as usual, not listed here
    # Dropped and downgraded entries are never read back as earlier findings (so never "dismissed").
    assert summary_findings(out) == [("P1", "b.py", 90, "Far away problem")]
    assert [p.title for p in collect_prior_findings([], set(), out)] == ["Far away problem"]
    only_kept = v.Verification("m", [v.Checked(_f(), "kept", _f(), 4, "")])
    plain = summary_body(name="N", result=result, posted=[], unplaced=[], suppressed=0, head_sha="abcdef1",
                         files_reviewed=1, include_diagram=False, review_count=1, mention="@n",
                         verification=only_kept)  # fmt: skip
    assert "verifier" not in plain


def test_verification_json_and_cost_line():
    from simpli_review.cli import cost_line
    from simpli_review.reviewer import Outcome

    ver = _verification()
    ver.checked[0].usage = {"input": 1000, "output": 100, "cache_read": 0, "cache_write": 0}
    doc = v.verification_json(ver)
    assert doc is not None and doc["counts"] == {
        "kept": 1,
        "downgraded": 1,
        "dropped": 1,
        "disputed": 0,
        "unverified": 1,
    }
    first = doc["items"][0]
    assert (first["status"], first["original_severity"], first["severity"], first["reason"]) == (
        "dropped", "P1", None, "checked at app/api.py:6",
    )  # fmt: skip
    assert v.verification_json(None) is None
    result = ReviewResult(summary="s", confidence_score=3, confidence_reason="r", files=[], findings=[])
    outcome = Outcome(result=result, usage={"input": 1000, "output": 0, "cache_read": 0, "cache_write": 0})
    assert cost_line("claude-opus-5-5", outcome) == "Estimated cost: reviewer claude-opus-5-5 $0.00"
    outcome.verification = ver
    line = cost_line("claude-opus-5-5", outcome)
    assert "verifier claude-sonnet-5-5 $0.00" in line and "dropped 1" in line


def test_rules_reach_the_prompt_only_when_enabled():
    cfg = Config(rules=[Rule(id="r1", rule="No raw SQL", enabled=False)])
    assert "No raw SQL" not in v.build_prompt(_f(rule_id="r1"), None, cfg)[0]["text"]


# ---------- the summary and confidence score after the pass ----------


def _result(findings: list[Finding]) -> ReviewResult:
    return ReviewResult(
        summary="Two risks: the ghost bug and a real one.",
        confidence_score=2,
        confidence_reason="The ghost bug is serious.",
        files=[],
        findings=findings,
    )


def test_the_summary_is_left_alone_when_nothing_changed(repo: Path):
    ver = v.Verification("m", [v.Checked(_f(), "kept", _f(), 4, "ok")])
    client = FakeClient([])
    out = v.reconcile_summary(client, Settings(), _result([_f()]), ver, Toolbox(repo, RepoIndex(repo)))
    assert out == _result([_f()]) and client.requests == [] and ver.summary == "unchanged"


def test_the_summary_and_score_are_rewritten_after_drops_and_downgrades(repo: Path):
    ver = _verification()
    remaining = [c.finding for c in ver.checked if c.finding is not None]
    update = {"summary": "One real risk.", "confidence_score": 4, "confidence_reason": "Only a P1 remains."}
    client = FakeClient([message(tool_use("s", v.SUMMARY_SUBMIT, update))])
    out = v.reconcile_summary(client, Settings(), _result(remaining), ver, Toolbox(repo, RepoIndex(repo)))
    assert (out.summary, out.confidence_score, out.confidence_reason) == (
        "One real risk.", 4, "Only a P1 remains.",
    )  # fmt: skip
    assert out.findings == remaining and ver.summary == "rewritten" and ver.usage["input"] >= 100
    req = client.requests[0]
    prompt = req["messages"][0]["content"][0]["text"]
    assert "dropped: Ghost bug that is handled (reason: checked at app/api.py:6)" in prompt
    assert "lowered P0 to P1: Overstated issue" in prompt and "Two risks" in prompt and "2/5" in prompt
    assert "Ghost bug" not in prompt.split("## Findings that remain")[1]
    assert req["output_config"] == {"effort": "low"} and [t["name"] for t in req["tools"]] == [
        v.SUMMARY_SUBMIT
    ]


def test_a_failed_rewrite_keeps_the_text_and_says_it_predates_the_pass(repo: Path, caplog):
    ver = _verification()
    out = v.reconcile_summary(FakeClient([]), Settings(), _result([]), ver, Toolbox(repo, RepoIndex(repo)))
    assert out.summary == _result([]).summary and out.confidence_score == 2
    assert out.confidence_reason.startswith("The ghost bug is serious. (Written before verification")
    assert "dropped 1 and downgraded 1" in out.confidence_reason and ver.summary == "noted"
    assert "could not update the summary" in caplog.text


def test_the_summary_section_says_when_the_summary_was_updated():
    from simpli_review.render import verification_section

    ver = _verification()
    assert "were updated after these changes" not in verification_section(ver)
    ver.summary = "rewritten"
    assert "The summary and confidence score above were updated after these changes." in (
        verification_section(ver)
    )


# ---------- blocking findings: the verifier can dispute them, never waive them ----------


@pytest.mark.parametrize(
    ("block_on", "severity", "verdict", "status", "after"),
    [
        ("P0", "P0", "drop", "disputed", "P0"),
        ("P0", "P0", "downgrade", "disputed", "P0"),  # P1 would fall below blockOn P0
        ("P0", "P1", "downgrade", "downgraded", "P2"),  # below blockOn: as before
        ("P0", "P1", "drop", "dropped", None),
        ("P1", "P0", "downgrade", "downgraded", "P1"),  # still at blockOn
        ("P1", "P1", "downgrade", "disputed", "P1"),
        ("P1", "P1", "drop", "disputed", "P1"),
        ("P1", "P2", "drop", "dropped", None),
        ("none", "P0", "drop", "dropped", None),
        ("none", "P0", "downgrade", "downgraded", "P1"),
    ],
)
def test_a_blocking_finding_is_disputed_not_dropped(block_on, severity, verdict, status, after):
    got, f = v.apply_verdict(_f(severity=severity), v.Verdict(verdict=verdict, confidence=4, reason="r"), 3,
                             block_on)  # fmt: skip
    assert got == status and (f.severity.value if f else None) == after
    low, _ = v.apply_verdict(_f(severity="P0"), v.Verdict(verdict="keep", confidence=1, reason="r"), 3, "P0")
    assert low == "disputed"  # a confidence under minConfidence is a drop, so it is disputed too


def test_a_disputed_p0_keeps_blocking_and_carries_the_reason(repo: Path):
    from simpli_review.cli import _gate
    from simpli_review.render import inline_body
    from simpli_review.reviewer import Outcome

    cfg = Config.model_validate({"blockOn": "P0", "overrideLabel": "ship-it", "verify": {"enabled": True}})
    p0 = _f(severity="P0")
    checked, _ = _run_one(repo, [_verdict("drop", 1, "the caller validates it at app/api.py:6")], p0, cfg)
    assert checked.status == "disputed" and checked.finding is not None
    assert checked.finding.severity == Severity.P0 and checked.finding.title == p0.title
    note = (
        "Second check disputes this: the caller validates it at app/api.py:6. "
        "Add the `ship-it` label if you agree."
    )
    assert checked.finding.body.endswith(note) and note in inline_body(checked.finding)
    result = ReviewResult(summary="s", confidence_score=1, confidence_reason="r", files=[], findings=[])
    _, _, decision = _gate(Outcome(result=result, posted=[checked.finding]), cfg, [])
    assert decision.blocking
    _, _, overridden = _gate(Outcome(result=result, posted=[checked.finding]), cfg, ["ship-it"])
    assert not overridden.blocking and overridden.overridden_by == "ship-it"


def test_disputed_findings_are_shown_and_do_not_trigger_the_summary_rewrite(repo: Path):
    from simpli_review.render import verification_section

    p0 = _f("Blocking bug", "P0")
    ver = v.Verification("m", [v.Checked(p0, "disputed", p0, 1, "guarded at app/api.py:6")])
    text_ = verification_section(ver, "review-override")
    assert (
        "**Disputed:** the second check disputes 1 finding at the blocking severity. It still blocks" in text_
    )
    assert "add the `review-override` label" in text_
    assert "- Disputed P0: `app/billing.py:5` — Blocking bug. Second check: guarded at app/api.py:6" in text_
    assert summary_findings("### Issues outside the changed lines\n\n" + text_) == []
    client = FakeClient([])
    out = v.reconcile_summary(client, Settings(), _result([p0]), ver, Toolbox(repo, RepoIndex(repo)))
    assert client.requests == [] and out == _result([p0]) and ver.summary == "unchanged"
