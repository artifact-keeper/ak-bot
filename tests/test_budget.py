"""The daily JEV budget, enforced in CI: per-run caps x runs/day must stay
under the ceiling. Change a cron or a cap and this test must change with it."""
import re
import glob
import unittest

from akbot.cli import BOTS

DAILY_CEILING = 400


def runs_per_day(workflow_file: str) -> float:
    text = open(workflow_file).read()
    total = 0.0
    for cron in re.findall(r"cron: '([^']+)'", text):
        minute, hour, dom, month, dow = cron.split()
        if hour.startswith("*/"):
            per_day = 24 / int(hour[2:])
        elif hour == "*":
            per_day = 24
        else:
            per_day = len(hour.split(","))
        if dow not in ("*",) and "-" in dow:
            lo, hi = dow.split("-")
            per_day *= (int(hi) - int(lo) + 1) / 7
        elif dow not in ("*",):
            per_day *= len(dow.split(",")) / 7
        total += per_day
    return total


class BudgetTests(unittest.TestCase):
    def test_daily_ceiling(self):
        total = 0.0
        for f in glob.glob(".github/workflows/*.yml"):
            bot = f.rsplit("/", 1)[-1][:-4]
            if bot not in BOTS:
                continue
            cap = BOTS[bot][2]
            total += cap * runs_per_day(f)
        self.assertLessEqual(total, DAILY_CEILING, f"worst-case JEV calls/day {total:.0f} exceeds {DAILY_CEILING}")


class JevClientTests(unittest.TestCase):
    def _client(self):
        import os
        from unittest import mock
        from akbot.jev import JevClient
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}):
            return JevClient(timeout=1, max_retries=2)

    def _resp(self, body: bytes):
        import io
        r = io.BytesIO(body)
        r.__enter__ = lambda s: s
        r.__exit__ = lambda s, *a: None
        return r

    def test_retries_503_then_succeeds(self):
        import json
        import urllib.error
        from unittest import mock
        from akbot.jev import noul
        c = self._client()
        ok = json.dumps({"model": "jev-1", "answers": {"q": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 3}}).encode()
        err = urllib.error.HTTPError("u", 503, "busy", {}, self._resp(b"busy"))
        with mock.patch("urllib.request.urlopen", side_effect=[err, self._resp(ok)]), mock.patch("time.sleep"):
            r = c.ask({"s": 1}, {"q": noul("?")})
        self.assertEqual(r["q"].noul, 0.9)
        self.assertEqual(r.model, "jev-1")

    def test_422_is_not_retried(self):
        import urllib.error
        from unittest import mock
        from akbot.jev import JevError, noul
        c = self._client()
        err = urllib.error.HTTPError("u", 422, "bad", {}, self._resp(b"bad"))
        with mock.patch("urllib.request.urlopen", side_effect=[err, err]) as uo:
            with self.assertRaises(JevError) as cm:
                c.ask({}, {"q": noul("?")})
        self.assertEqual(cm.exception.status, 422)
        self.assertEqual(uo.call_count, 1)

    def test_timeout_is_not_retried_and_missing_answer_raises(self):
        import json
        from unittest import mock
        from akbot.jev import JevError, noul
        c = self._client()
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as uo:
            with self.assertRaises(JevError):
                c.ask({}, {"q": noul("?")})
        self.assertEqual(uo.call_count, 1)
        body = json.dumps({"answers": {}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=self._resp(body)):
            with self.assertRaises(JevError):
                c.ask({}, {"q": noul("?")})
        with mock.patch("urllib.request.urlopen", return_value=self._resp(b"<html>")):
            with self.assertRaises(JevError):
                c.ask({}, {"q": noul("?")})

    def test_budget_cap(self):
        from akbot.jev import Budgeted, BudgetExceeded, FakeJev, noul
        b = Budgeted(FakeJev(), 2)
        b.ask({}, {"q": noul("?")}); b.ask({}, {"q": noul("?")})
        with self.assertRaises(BudgetExceeded):
            b.ask({}, {"q": noul("?")})
        self.assertEqual(b.summary()["jev_calls"], 2)
