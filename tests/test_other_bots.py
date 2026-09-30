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
        self.assertTrue(c.ledger.has("red-run-1"))


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
        self.assertFalse(it.is_untriaged({"labels": [], "author": {"login": "github-actions[bot]"}}))


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
                 "blocker": {"type": "choice", "choice": "missing_linked_issue", "probabilities": {"missing_linked_issue": 0.7}, "confidence": 0.7},
                 "risk": {"type": "score", "score": 1.0, "probabilities": {}, "confidence": 0.6}})
        p = {"number": 9, "headRefOid": "abcdef1234567890", "labels": [], "url": "u"}
        pr.act(c, p, c.jev.ask({}, pr.questions()))
        self.assertEqual(c.log.records[0].tier, SUGGEST)
        self.assertIn("would comment blocker", c.log.records[0].action)
        self.assertTrue(c.ledger.has("pr-router-9-abcdef1234"))


class PreflightAuditTests(unittest.TestCase):
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
