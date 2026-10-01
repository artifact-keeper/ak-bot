import unittest
from unittest import mock

from akbot import gh
from akbot.bots import red_run_classifier as rr, issue_triage as it, base_image_bump as bb, \
    pr_review_router as pr, preflight_audit as pa
from akbot.context import Context
from akbot.decisions import DecisionLog, AUTO, SUGGEST, SKIP
from akbot.jev import FakeJev


def ctx(canned=None):
    c = Context("o/r", FakeJev(canned or {}), True, DecisionLog("t", True))
    c.ledger._seen = set()
    return c


NIGHTLY_JOB = {"name": "Nightly", "conclusion": "cancelled", "startedAt": "2026-09-30T07:50:33Z",
               "completedAt": "2026-09-30T08:36:33Z",
               "steps": [{"name": "Run native client smoke tests", "conclusion": "cancelled"},
                         {"name": "Notify on failure", "conclusion": "skipped"}]}
RUN = {"databaseId": 1, "workflowName": "Scheduled Tests", "event": "schedule", "headBranch": "main",
       "conclusion": "cancelled", "createdAt": "2026-09-30T07:50:00Z", "url": "https://x/runs/1", "status": "completed"}


class RedRunTests(unittest.TestCase):
    def test_timeout_and_notify_gap_detected(self):
        f = rr.timing_facts(RUN, [NIGHTLY_JOB])
        self.assertTrue(f["jobs"][0]["looks_like_timeout"])
        self.assertTrue(f["jobs"][0]["notify_step_skipped"])
        self.assertEqual(f["jobs"][0]["minutes"], 46.0)

    @mock.patch.object(gh, "repo_labels", return_value={"ci", "type:bug"})
    @mock.patch.object(rr, "digest")
    @mock.patch.object(gh, "rerun_failed")
    def test_timeout_never_auto_reruns(self, rerun, digest, _):
        c = ctx({"cause": {"type": "choice", "choice": "timeout", "probabilities": {"timeout": 0.95}, "confidence": 0.95},
                 "rerun_would_pass": {"type": "noul", "noul": 0.9}})
        state = {"run": {"attempt": 1}, "timing": rr.timing_facts(RUN, [NIGHTLY_JOB]), "failed_log_tail": []}
        rr.act(c, RUN, state, c.jev.ask(state, rr.questions()))
        rerun.assert_not_called()
        self.assertIn("would append to digest", c.log.records[0].action)

    @mock.patch.object(gh, "repo_labels", return_value={"ci", "type:bug"})
    @mock.patch.object(gh, "create_issue")
    def test_regression_auto_opens_issue_only_in_real_mode(self, create, _):
        c = ctx({"cause": {"type": "choice", "choice": "regression", "probabilities": {"regression": 0.9}, "confidence": 0.9},
                 "rerun_would_pass": {"type": "noul", "noul": 0.1}})
        state = {"run": {"attempt": 1}, "timing": {"jobs": []}, "failed_log_tail": ["boom"]}
        rr.act(c, dict(RUN, conclusion="failure"), state, c.jev.ask(state, rr.questions()))
        create.assert_not_called()          # dry run
        self.assertEqual(c.log.records[0].tier, AUTO)
        self.assertTrue(c.ledger.has("red-run-1-a1"))

    @mock.patch.object(gh, "repo_labels", return_value={"ci", "type:bug"})
    @mock.patch.object(rr, "digest")
    @mock.patch.object(gh, "rerun_failed")
    def test_publish_workflows_are_never_rerun(self, rerun, digest, _):
        c = ctx({"cause": {"type": "choice", "choice": "flake", "probabilities": {"flake": 0.99}, "confidence": 0.99},
                 "rerun_would_pass": {"type": "noul", "noul": 0.99}})
        c.dry_run = False
        state = {"run": {"attempt": 1}, "timing": {"jobs": []}, "failed_log_tail": []}
        run = dict(RUN, workflowName="Docker Publish", conclusion="failure", event="push", headBranch="v1.2.3")
        with mock.patch.object(gh, "comment"), mock.patch.object(c.ledger, "record"):
            rr.act(c, run, state, c.jev.ask(state, rr.questions()))
        rerun.assert_not_called()
        digest.assert_called()
        run = dict(RUN, workflowName="CI", conclusion="failure", event="push", headBranch="main")
        with mock.patch.object(c.ledger, "record"):
            rr.act(c, run, state, c.jev.ask(state, rr.questions()))
        rerun.assert_called_once()


class TriageTests(unittest.TestCase):
    LABELS = {"type:bug", "registry/npm", "regression", "priority:p1", "core"}

    @mock.patch.object(gh, "comment")
    @mock.patch.object(gh, "add_labels")
    def test_auto_labels_and_priority_only_proposed(self, add, comment, ):
        c = ctx({"type": {"type": "choice", "choice": "bug", "probabilities": {"bug": 0.9}, "confidence": 0.9},
                 "area": {"type": "choice", "choice": "npm", "probabilities": {"npm": 0.7}, "confidence": 0.7},
                 "priority": {"type": "score", "score": 2.3, "probabilities": {}, "confidence": 0.8},
                 "regression": {"type": "noul", "noul": 0.97}})
        issue = {"number": 5, "title": "t", "body": "b", "labels": [], "author": {"login": "u"}, "createdAt": "x", "url": "u"}
        it.act(c, issue, c.jev.ask({}, it.questions(self.LABELS)), self.LABELS)
        rec = c.log.records[0]
        self.assertEqual(rec.tier, AUTO)
        self.assertIn("would label type:bug, regression", rec.action)
        self.assertIn("would comment proposal", rec.action)   # registry/npm (0.70) and priority:p1 proposed, not applied

    def test_untriaged_filter(self):
        self.assertTrue(it.is_untriaged({"labels": [], "author": {"login": "x"}}))
        self.assertFalse(it.is_untriaged({"labels": [{"name": "type:bug"}], "author": {"login": "x"}}))
        self.assertFalse(it.is_untriaged({"labels": [{"name": "registry/npm"}], "author": {"login": "x"}}))
        self.assertFalse(it.is_untriaged({"labels": [], "author": {"login": "github-actions[bot]"}}))
        self.assertFalse(it.is_untriaged({"labels": [], "author": {"login": "app/ak-jev-bot"}}))
        self.assertFalse(it.is_untriaged({"labels": [], "author": {"login": "x", "is_bot": True}}))

    @mock.patch.object(gh, "comment")
    @mock.patch.object(gh, "add_labels")
    def test_priority_only_proposal_is_not_commented(self, add, comment):
        c = ctx({"priority": {"type": "score", "score": 2.6, "probabilities": {"2": 0.3, "3": 0.6}, "confidence": 0.8}})
        issue = {"number": 6, "title": "t", "body": "b", "labels": [], "author": {"login": "u"}, "createdAt": "x", "url": "u"}
        it.act(c, issue, c.jev.ask({}, it.questions(self.LABELS)), self.LABELS)
        rec = c.log.records[0]
        self.assertEqual(rec.tier, SUGGEST)
        self.assertIn("priority:p0", rec.action)
        self.assertNotIn("would comment", rec.action)

    @mock.patch.object(gh, "repo_labels", return_value=set())
    @mock.patch.object(gh, "list_issues")
    def test_one_bad_subject_does_not_abort_the_batch(self, li, _):
        from akbot.jev import JevError
        li.return_value = [{"number": n, "title": "t", "body": "b", "labels": [], "author": {"login": "u"},
                            "createdAt": "x", "url": "u"} for n in (1, 2, 3)]
        c = ctx()
        calls = {"n": 0}
        def ask(state, q):
            calls["n"] += 1
            if calls["n"] == 2:
                raise JevError(422, "too big")
            return FakeJev().ask(state, q)
        c.jev.ask = ask
        it.run(c)
        self.assertEqual(calls["n"], 3)
        self.assertTrue(any("skipped: JevError" in r.action for r in c.log.records))
        self.assertTrue(c.ledger.has("triage-1") and c.ledger.has("triage-3") and not c.ledger.has("triage-2"))


TRACKER = """### What the watch found

```
Digest-pinned external base images under /x/docker:

  ghcr.io/artifact-keeper/trivy
    pinned digest : sha256:29a423b8a34a642b78c1c6d3677759cb41a8aa42df53fd542ba44d745d954410
    newest tag    : v0.74.0-r3 (registry 0.74.0-r3) — pinned
    VULNERABLE  : 1 CRITICAL/HIGH fixed finding(s)
      - HIGH CVE-2026-14456 in openssl-libs 1:3.5.5-6.el9_8 (fixed in 1:3.5.8-1.el9_8)
```
[View run](https://github.com/artifact-keeper/artifact-keeper/actions/runs/36721931735) · updated 2026-09-30
"""


class ChangelogRunTests(unittest.TestCase):
    def test_marker_is_keyed_on_content_and_open_drafts_count_as_existing(self):
        from akbot.bots import changelog_drafter as cd
        c = ctx()
        c.ledger._seen = {"changelog-4006-4090"}
        pf = cd.Preflight(99, "v1..HEAD", "NOT READY", "main", "abc", [cd.Undocumented(4090, "x"), cd.Undocumented(4006, "y")])
        with mock.patch.object(cd, "pick_run", return_value=(pf, "u")), \
             mock.patch.object(gh, "api", return_value=[]), \
             mock.patch.object(cd, "open_draft_fragments", return_value=set()), \
             mock.patch.object(cd, "decide_one") as decide:
            cd.run(c)
        decide.assert_not_called()


class UndoTests(unittest.TestCase):
    def test_undo_effects_dry_run(self):
        from akbot import undo
        self.assertIn("would remove ['type:bug']", undo.undo_effect({"kind": "label", "args": ["o/r", 5, ["type:bug"]]}, True))
        self.assertIn("would delete comment 77", undo.undo_effect(
            {"kind": "comment", "args": ["o/r", 5, "body"], "result": "https://github.com/o/r/issues/5#issuecomment-77"}, True))
        self.assertIn("would retract", undo.undo_effect({"kind": "open", "result": "https://github.com/o/r/pull/8"}, True))
        self.assertIn("cannot undo", undo.undo_effect({"kind": "rerun", "result": "u"}, True))


class BaseImageTests(unittest.TestCase):
    def test_parse_tracker(self):
        rep = bb.parse_report(TRACKER)
        self.assertEqual(rep["run_id"], 36721931735)
        img = rep["images"][0]
        self.assertEqual(img["image"], "ghcr.io/artifact-keeper/trivy")
        self.assertTrue(img["pinned_is_newest"])
        self.assertEqual(img["newest_tag"], "v0.74.0-r3")
        self.assertEqual(img["findings"][0]["cve"], "CVE-2026-14456")
        self.assertEqual(img["findings"][0]["fixed_in"], "1:3.5.8-1.el9_8")

    def test_stale_pin_detected(self):
        rep = bb.parse_report(TRACKER.replace("— pinned", "— STALE: pinned digest is 0.74.0-r2"))
        self.assertFalse(rep["images"][0]["pinned_is_newest"])

    def test_bump_patch(self):
        self.assertEqual(bb.bump_patch("1.2.11\n"), "1.2.12")


class PrRouterTests(unittest.TestCase):
    @mock.patch.object(gh, "repo_labels", return_value={"needs-maintainer-review"})
    @mock.patch.object(gh, "comment")
    @mock.patch.object(gh, "add_labels")
    def test_blocker_comment_at_suggest(self, add, comment, _):
        c = ctx({"readiness": {"type": "score", "score": 1.2, "probabilities": {}, "confidence": 0.7},
                 "blocker": {"type": "choice", "choice": "no_tests", "probabilities": {"no_tests": 0.75}, "confidence": 0.7},
                 "risk": {"type": "score", "score": 1.0, "probabilities": {}, "confidence": 0.6}})
        p = {"number": 9, "headRefOid": "abcdef1234567890", "labels": [], "url": "u"}
        pr.act(c, p, c.jev.ask({}, pr.questions()))
        self.assertEqual(c.log.records[0].tier, SUGGEST)      # external comments need 0.70 to suggest, 0.90 to assert
        self.assertIn("would comment blocker", c.log.records[0].action)
        self.assertTrue(c.ledger.has("pr-router-9-abcdef1234"))

    @mock.patch.object(gh, "repo_labels", return_value=set())
    def test_linked_issue_blocker_is_logged_not_commented(self, _):
        c = ctx({"blocker": {"type": "choice", "choice": "missing_linked_issue", "probabilities": {"missing_linked_issue": 0.95}, "confidence": 0.9}})
        p = {"number": 9, "headRefOid": "abcdef1234567890", "labels": [], "url": "u"}
        pr.act(c, p, c.jev.ask({}, pr.questions()))
        self.assertIn("linked-issue gate already says so", c.log.records[0].action)

    @mock.patch.object(gh, "repo_labels", return_value={"needs-maintainer-review"})
    def test_ready_requires_mass_on_top_level(self, _):
        c = ctx({"readiness": {"type": "score", "score": 2.55, "probabilities": {"2": 0.45, "3": 0.55}, "confidence": 0.9},
                 "blocker": {"type": "choice", "choice": "none", "probabilities": {"none": 0.95}, "confidence": 0.9}})
        p = {"number": 9, "headRefOid": "abcdef1234567890", "labels": [], "url": "u"}
        pr.act(c, p, c.jev.ask({}, pr.questions()))
        self.assertEqual(c.log.records[0].tier, SKIP)

    def test_say_updates_instead_of_reposting(self):
        c = ctx()
        c.dry_run = False
        prev = {"id": 42, "body": "old\n\n" + pr.MARK}
        with mock.patch.object(pr, "own_comment", return_value=prev), \
             mock.patch.object(gh, "update_comment", return_value="42") as upd, \
             mock.patch.object(gh, "comment") as new:
            self.assertEqual(pr.say(c, {"number": 1}, "new text"), "42")
            self.assertEqual(pr.say(c, {"number": 1}, "old"), "unchanged")
        upd.assert_called_once()
        new.assert_not_called()


class PreflightAuditTests(unittest.TestCase):
    @mock.patch.object(gh, "create_issue")
    @mock.patch.object(gh, "run_artifacts", return_value=[])
    @mock.patch.object(gh, "run_log", return_value=[])
    @mock.patch.object(gh, "list_runs")
    def test_cancelled_runs_and_missing_logs_never_open_issues(self, lr, log, arts, create):
        lr.return_value = [
            {"databaseId": 1, "status": "completed", "conclusion": "cancelled", "event": "schedule", "headBranch": "main", "headSha": "x", "url": "u"},
            {"databaseId": 2, "status": "completed", "conclusion": "failure", "event": "schedule", "headBranch": "main", "headSha": "x", "url": "u"},
        ]
        c = ctx()
        c.dry_run = False
        pa.run(c, limit=5)
        create.assert_not_called()
        self.assertFalse(c.ledger.has("preflight-audit-1"))
        self.assertFalse(c.ledger.has("preflight-audit-2"))    # log unavailable: retried next time

    @mock.patch.object(gh, "create_issue")
    @mock.patch.object(gh, "run_artifacts", return_value=[])
    @mock.patch.object(gh, "run_log", return_value=["5) CHANGELOG", "[FAIL] x", "nothing conclusive here"])
    @mock.patch.object(gh, "list_runs")
    def test_no_verdict_line_is_recorded_not_filed(self, lr, log, arts, create):
        lr.return_value = [{"databaseId": 3, "status": "completed", "conclusion": "failure", "event": "schedule",
                            "headBranch": "main", "headSha": "x", "url": "u"}]
        c = ctx()
        pa.run(c, limit=1)
        create.assert_not_called()
        self.assertTrue(c.ledger.has("preflight-audit-3"))

    def test_ready_needs_matching_artifact(self):
        run = {"conclusion": "success", "event": "workflow_dispatch"}
        lines = ["READY to cut from main@" + "a" * 40 + ": no blocking problems."]
        _, probs = pa.deterministic(run, lines, [{"name": "release-preflight-" + "a" * 40}])
        self.assertEqual(probs, [])
        _, probs = pa.deterministic(run, lines, [{"name": "release-preflight-" + "b" * 40}])
        self.assertTrue(any("evidence artifact names" in p for p in probs))
        _, probs = pa.deterministic(run, lines, [])
        self.assertTrue(any("0 evidence" in p for p in probs))

    def test_not_ready_must_not_upload(self):
        run = {"conclusion": "failure", "event": "schedule"}
        lines = ["NOT READY to cut from main@7c42891c: 2 blocking problem(s)."]
        _, probs = pa.deterministic(run, lines, [])
        self.assertEqual(probs, [])
        _, probs = pa.deterministic(run, lines, [{"name": "release-preflight-" + "c" * 40}])
        self.assertTrue(probs)
        _, probs = pa.deterministic(dict(run), ["NOT READY to cut from release/1.10.x@7c42891c: x"], [])
        self.assertTrue(any("not main" in p for p in probs))


if __name__ == "__main__":
    unittest.main()


class ApprovalTests(unittest.TestCase):
    LABELS = {"type:bug", "registry/npm", "priority:p1", "core"}

    def _c(self):
        c = ctx()
        c.dry_run = False
        return c

    @mock.patch.object(gh, "repo_owner", return_value="brandonrc")
    @mock.patch.object(gh, "update_comment")
    @mock.patch.object(gh, "add_labels", return_value=["type:bug", "registry/npm"])
    def test_thumbs_up_from_owner_applies_proposed_labels(self, add, upd, _):
        body = "ak-bot triage proposal (not applied): `type:bug` (0.7), `registry/npm` (0.65), `priority:p1` (0.6)"
        with mock.patch.object(gh, "search_issues_with_comment", return_value=[5]), \
             mock.patch.object(gh, "issue_comments", return_value=[{"id": 11, "body": body}]), \
             mock.patch.object(gh, "comment_reactions", return_value=[{"content": "+1", "user": {"login": "brandonrc"}}]):
            n = it.apply_approved(self._c(), self.LABELS)
        self.assertEqual(n, 1)
        add.assert_called_once_with("o/r", 5, ["type:bug", "registry/npm", "priority:p1"])
        self.assertIn("applied by @brandonrc", upd.call_args[0][2])

    @mock.patch.object(gh, "repo_owner", return_value="brandonrc")
    @mock.patch.object(gh, "update_comment")
    @mock.patch.object(gh, "add_labels")
    def test_strangers_and_handled_comments_are_ignored(self, add, upd, _):
        body = "ak-bot triage proposal (not applied): `type:bug` (0.7)"
        with mock.patch.object(gh, "search_issues_with_comment", return_value=[5, 6]), \
             mock.patch.object(gh, "issue_comments", side_effect=lambda r, n: [{"id": n, "body": body if n == 5 else body + "\n<sub>applied by @x via 👍</sub>"}]), \
             mock.patch.object(gh, "comment_reactions", return_value=[{"content": "+1", "user": {"login": "someone-else"}}]):
            n = it.apply_approved(self._c(), self.LABELS)
        self.assertEqual(n, 0)
        add.assert_not_called()

    @mock.patch.object(gh, "repo_owner", return_value="brandonrc")
    @mock.patch.object(gh, "update_comment")
    @mock.patch.object(gh, "add_labels")
    def test_thumbs_down_dismisses(self, add, upd, _):
        body = "ak-bot triage proposal (not applied): `type:bug` (0.7)"
        with mock.patch.object(gh, "search_issues_with_comment", return_value=[5]), \
             mock.patch.object(gh, "issue_comments", return_value=[{"id": 11, "body": body}]), \
             mock.patch.object(gh, "comment_reactions", return_value=[{"content": "-1", "user": {"login": "brandonrc"}}]):
            c = self._c()
            it.apply_approved(c, self.LABELS)
        add.assert_not_called()
        self.assertIn("dismissed by @brandonrc", upd.call_args[0][2])
        self.assertIn("proposal -1", c.log.records[0].action)
