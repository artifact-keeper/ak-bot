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
        issues = [
            {"number": 1, "title": "ak-bot ledger", "state": "open"},
            {"number": 2, "title": f"ak-bot ledger {previous_month_key()}", "state": "open"},
            {"number": 3, "title": f"ak-bot ledger {month_key()}", "state": "open"},
            {"number": 4, "title": "ak-bot ledger 2020-01", "state": "open"},
        ]
        comments = {1: [{"body": "x\n<sub>ak-bot-id: a</sub>"}], 2: [{"body": "ak-bot-id: b"}], 3: [{"body": "ak-bot-id: c"}], 4: [{"body": "ak-bot-id: old"}]}
        closed = []
        with mock.patch.object(gh, "issue_search", return_value=issues), \
             mock.patch.object(gh, "issue_comments", side_effect=lambda r, n: comments[n]), \
             mock.patch.object(gh, "comment"), \
             mock.patch.object(gh, "gh", side_effect=lambda *a, **k: closed.append(a)):
            led = Ledger("o/r", dry_run=False)
            self.assertEqual(led.seen(), {"a", "b", "c"})
            self.assertEqual(led.number(), 3)
        self.assertEqual(len(closed), 1)
        self.assertIn("4", closed[0])
