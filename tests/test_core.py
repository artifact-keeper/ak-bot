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
