import unittest

from akbot.decisions import Thresholds, AUTO, SUGGEST, SKIP, redact, DecisionLog
from akbot.jev import FakeJev, choice, noul, score, Answer


class TierTests(unittest.TestCase):
    def test_tiers(self):
        t = Thresholds(0.85, 0.6)
        self.assertEqual(t.tier(0.9), AUTO)
        self.assertEqual(t.tier(0.85), AUTO)
        self.assertEqual(t.tier(0.7), SUGGEST)
        self.assertEqual(t.tier(0.59), SKIP)

    def test_choice_certainty_is_probability_of_choice(self):
        a = Answer.parse("q", {"type": "choice", "choice": "x", "probabilities": {"x": 0.7, "y": 0.3}, "confidence": 0.99})
        self.assertAlmostEqual(a.certainty, 0.7)
        self.assertFalse(a.missing_fields)
        self.assertTrue(Answer.parse("q", {"type": "choice"}).missing_fields)
        s = Answer.parse("q", {"type": "score", "score": 2.4, "probabilities": {"0": 0.1, "1": 0.1, "2": 0.3, "3": 0.5}, "confidence": 0.5})
        self.assertAlmostEqual(s.mass_at_least(2), 0.8)

    def test_noul_certainty_is_distance_from_half(self):
        self.assertAlmostEqual(Answer.parse("q", {"type": "noul", "noul": 0.5}).certainty, 0.0)
        self.assertAlmostEqual(Answer.parse("q", {"type": "noul", "noul": 0.95}).certainty, 0.9)
        self.assertAlmostEqual(Answer.parse("q", {"type": "noul", "noul": 0.05}).certainty, 0.9)

    def test_redact(self):
        self.assertNotIn("ghp_", redact("token ghp_abcdefghijklmnopqrstuvwxyz1234 here"))
        self.assertIn("[redacted]", redact("Authorization: Bearer abcdefghijklmnopqrstu"))

    def test_fake_jev_is_uncertain_by_default(self):
        r = FakeJev().ask({"x": 1}, {"a": choice("?", {"x": "", "y": ""}), "b": noul("?"), "c": score("?", ["l", "h"])})
        self.assertEqual(Thresholds().tier(r["a"].certainty), SKIP)
        self.assertEqual(Thresholds().tier(r["b"].certainty), SKIP)
        self.assertEqual(r["c"].score, 0.5)

    def test_log_markdown(self):
        log = DecisionLog("t", True)
        r = FakeJev({"a": {"type": "choice", "choice": "x", "probabilities": {"x": 0.9, "y": 0.1}, "confidence": 0.9}}).ask(
            {}, {"a": choice("?", {"x": "", "y": ""})})
        log.add("PR #1", "v", r.model, r.answers, AUTO, "did it")
        self.assertIn("| PR #1 | auto | did it | a=x (0.9) |", log.to_markdown())
        self.assertIn('"tier": "auto"', log.to_jsonl())


if __name__ == "__main__":
    unittest.main()


class LedgerTests(unittest.TestCase):
    def test_month_keys(self):
        from akbot.context import month_key, previous_month_key
        import calendar
        jan = calendar.timegm((2026, 1, 15, 0, 0, 0))
        self.assertEqual(month_key(jan), "2026-01")
        self.assertEqual(previous_month_key(jan), "2025-12")

    def test_reads_current_previous_and_legacy_and_closes_older(self):
        from unittest import mock
        from akbot import gh
        from akbot.context import Ledger, month_key, previous_month_key
        open_issues = [
            {"number": 1, "title": "ak-bot ledger"},
            {"number": 2, "title": f"ak-bot ledger {previous_month_key()}"},
            {"number": 4, "title": "ak-bot ledger 2020-01"},
            {"number": 9, "title": "ak-bot ledger is noisy"},           # a human issue: never matched
        ]
        closed = [{"number": 3, "title": f"ak-bot ledger {month_key()}", "state": "closed"}]
        comments = {1: [{"body": "x\n<sub>ak-bot-id: a</sub>"}], 2: [{"body": "ak-bot-id: b"}],
                    3: [{"body": "ak-bot-id: c · ak-bot-run: 7"}], 4: [{"body": "ak-bot-id: old"}], 9: [{"body": "ak-bot-id: human"}]}
        calls = []
        with mock.patch.object(gh, "api", return_value=open_issues), \
             mock.patch.object(gh, "issue_search", return_value=closed), \
             mock.patch.object(gh, "issue_comments", side_effect=lambda r, n: comments[n]), \
             mock.patch.object(gh, "comment"), \
             mock.patch.object(gh, "gh", side_effect=lambda *a, **k: calls.append(a)):
            led = Ledger("o/r", dry_run=False)
            self.assertEqual(led.seen(), {"a", "b", "c"})     # closed current-month ledger still read
            self.assertEqual(led.number(), 3)                 # and reopened rather than recreated
        self.assertTrue(any("close" in a and "4" in a for a in calls))
        self.assertTrue(any("reopen" in a and "3" in a for a in calls))
        self.assertFalse(any("9" in a for a in calls))

    def test_halt_blocks_act_and_record(self):
        import os
        from unittest import mock
        from akbot.context import Context
        c = Context("o/r", FakeJev(), False, DecisionLog("t", False))
        c.ledger._seen = set()
        hit = []
        with mock.patch.dict(os.environ, {"AK_BOT_HALT": "stop"}):
            self.assertEqual(c.act("label x", lambda: hit.append(1)), "would label x")
            c.ledger.record("m", "t")
        self.assertEqual(hit, [])
        self.assertEqual(c.log.pending_effects, [])
