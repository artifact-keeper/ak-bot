"""Thin client for TypeSafe's System One endpoint (the JEV model).

    POST {base_url}/v1/systemone
    {"model": "jev-latest", "state": <str|obj|list>, "questions": {id: Question}}

Question shapes (docs.typesafe.ai/api):
    choice  {"type":"choice","instructions":..., "criteria": {option: description}}
    score   {"type":"score", "instructions":..., "criteria": [level0, level1, ...]}
    noul    {"type":"noul",  "instructions":..., "criteria": {"true":..,"false":..}}  (criteria optional)

Answer shapes:
    choice  {"type":"choice","choice":opt,"probabilities":{opt:p},"confidence":c}
    score   {"type":"score","score":x,"legend":{..},"probabilities":{..},"confidence":c}
    noul    {"type":"noul","noul":p}

There are no priors, weights or temperature. What you can change is the
state, the criteria wording, the instructions, and your own thresholds.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
RETRYABLE = {429, 500, 502, 503, 504, 529}


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, levels: list[str]) -> dict:
    if not 2 <= len(levels) <= 10:
        raise ValueError("score needs 2-10 ordered levels")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true or "", "false": false or ""}
    return q


class JevError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"JEV HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


@dataclass
class Answer:
    id: str
    type: str
    choice: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    score: float | None = None
    legend: dict[str, Any] = field(default_factory=dict)
    noul: float | None = None
    raw: dict = field(default_factory=dict)

    @property
    def certainty(self) -> float:
        """One 0-1 number usable for routing regardless of question type.

        choice: the probability JEV put on the option it chose. A probability
               has a fixed meaning and is what a calibration plot can test;
               JEV's `confidence` (distribution concentration) is kept in the
               record but not routed on.
        score:  JEV's `confidence`; callers that need "level k or above" use
               mass_at_least(k), which is the sound quantity for a rubric.
        noul:   |2p-1|, distance from the 0.5 "cannot tell" point, so that a
               0.90 auto line means p >= 0.95 (or <= 0.05). Our derivation.
        An answer with neither field routes to skip and is flagged (see
        `missing_fields`).
        """
        if self.type == "noul":
            return abs(2 * self.noul - 1) if self.noul is not None else 0.0
        if self.type == "choice":
            if self.choice is not None and self.probabilities:
                return float(self.probabilities.get(self.choice, 0.0))
            return float(self.confidence or 0.0)
        return float(self.confidence or 0.0)

    @property
    def missing_fields(self) -> bool:
        if self.type == "noul":
            return self.noul is None
        if self.type == "choice":
            return self.choice is None or not self.probabilities
        return self.score is None or self.confidence is None

    @property
    def yes(self) -> bool:
        """Strictly above the 0.5 'cannot tell' point; exactly 0.5 is not a yes."""
        return self.type == "noul" and (self.noul or 0.0) > 0.5

    @property
    def score_or_nan(self) -> float:
        return float("nan") if self.score is None else float(self.score)

    @property
    def noul_or_nan(self) -> float:
        return float("nan") if self.noul is None else float(self.noul)

    def mass_at_least(self, level: int) -> float:
        """Probability mass on rubric levels >= `level` (score answers)."""
        return sum(v for k, v in self.probabilities.items() if str(k).isdigit() and int(k) >= level)

    @classmethod
    def parse(cls, qid: str, data: dict) -> "Answer":
        return cls(
            id=qid,
            type=data.get("type", ""),
            choice=data.get("choice"),
            probabilities=dict(data.get("probabilities") or {}),
            confidence=data.get("confidence"),
            score=data.get("score"),
            legend=dict(data.get("legend") or {}),
            noul=data.get("noul"),
            raw=data,
        )


@dataclass
class Result:
    model: str
    answers: dict[str, Answer]
    usage: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def __getitem__(self, qid: str) -> Answer:
        return self.answers[qid]


class JevClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("TYPESAFE_MODEL") or DEFAULT_MODEL
        # A 12 KB diff plus three questions can take longer than the headline
        # latency; a tight timeout resends (and is billed) up to max_retries times.
        self.timeout = timeout if timeout is not None else float(os.environ.get("TYPESAFE_TIMEOUT", "30"))
        self.max_retries = max_retries

    @property
    def name(self) -> str:
        return f"jev:{self.model}"

    def ask(self, state: Any, questions: dict[str, dict]) -> Result:
        payload = json.dumps({"model": self.model, "state": state, "questions": questions}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/systemone",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ak-bot/0.1",
            },
        )
        attempt = 0
        while True:
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode()
                elapsed = (time.monotonic() - t0) * 1000
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise JevError(200, f"malformed response: {e}") from None
                if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
                    raise JevError(200, f"malformed response: {raw[:200]}")
                answers = {qid: Answer.parse(qid, a) for qid, a in body["answers"].items() if isinstance(a, dict)}
                missing = set(questions) - set(answers)
                if missing:
                    raise JevError(200, f"response lacks answers for {sorted(missing)}")
                return Result(model=body.get("model", self.model), answers=answers,
                              usage=body.get("usage") or {}, elapsed_ms=elapsed)
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code in RETRYABLE and attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt, e.headers.get("Retry-After"))
                    continue
                raise JevError(e.code, text) from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
                timed_out = isinstance(e, TimeoutError) or "timed out" in str(e).lower() \
                    or isinstance(getattr(e, "reason", None), TimeoutError)
                if timed_out:
                    # A client-side timeout does not cancel server-side work; a
                    # resend would be billed again. Fail the subject instead.
                    raise JevError(0, f"timeout after {self.timeout}s (not retried)") from None
                if attempt < self.max_retries:
                    attempt += 1
                    self._backoff(attempt, None)
                    continue
                raise JevError(0, str(e)) from None

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        delay = min(8.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.3)
        if retry_after and retry_after.isdigit():
            delay = max(delay, min(30.0, float(retry_after)))
        time.sleep(delay)


class FakeJev:
    """Deterministic stand-in for tests and for --dry-run without an API key.

    `canned` maps question id -> answer dict in JEV's wire shape. Anything not
    canned gets a maximally uncertain answer (uniform choice, noul 0.5), so a
    dry run never routes to the auto tier by accident.
    """

    def __init__(self, canned: dict[str, dict] | None = None, model: str = "fake-jev"):
        self.canned = canned or {}
        self.model = model
        self.calls: list[tuple[Any, dict]] = []

    @property
    def name(self) -> str:
        return f"fake:{self.model}"

    def ask(self, state: Any, questions: dict[str, dict]) -> Result:
        self.calls.append((state, questions))
        answers = {}
        for qid, q in questions.items():
            if qid in self.canned:
                answers[qid] = Answer.parse(qid, self.canned[qid])
                continue
            t = q["type"]
            if t == "choice":
                opts = list(q["criteria"])
                p = 1.0 / len(opts)
                answers[qid] = Answer.parse(qid, {"type": "choice", "choice": opts[0],
                                                  "probabilities": {o: p for o in opts}, "confidence": 0.0})
            elif t == "score":
                n = len(q["criteria"])
                answers[qid] = Answer.parse(qid, {"type": "score", "score": (n - 1) / 2,
                                                  "legend": {str(i): lvl for i, lvl in enumerate(q["criteria"])},
                                                  "probabilities": {str(i): 1 / n for i in range(n)},
                                                  "confidence": 0.0})
            else:
                answers[qid] = Answer.parse(qid, {"type": "noul", "noul": 0.5})
        return Result(model=self.model, answers=answers)


class BudgetExceeded(JevError):
    pass


class Budgeted:
    """Per-run call cap and usage accounting around any client.

    The cap is the whole daily-budget mechanism: calls/day <= sum over bots of
    cap x runs/day, and tests/test_budget.py asserts that sum. No counter
    service, no shared state.
    """

    def __init__(self, inner, max_calls: int):
        self.inner = inner
        self.max_calls = max_calls
        self.calls = 0
        self.state_bytes = 0
        self.usage: dict[str, float] = {}
        self.elapsed_ms = 0.0
        self.missing_field_answers = 0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", "")

    def ask(self, state: Any, questions: dict[str, dict]) -> Result:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(0, f"per-run JEV budget of {self.max_calls} calls reached")
        self.calls += 1
        self.state_bytes += len(json.dumps(state, default=str))
        r = self.inner.ask(state, questions)
        self.elapsed_ms += r.elapsed_ms
        for k, v in (r.usage or {}).items():
            if isinstance(v, (int, float)):
                self.usage[k] = self.usage.get(k, 0) + v
        self.missing_field_answers += sum(1 for a in r.answers.values() if a.missing_fields)
        return r

    def summary(self) -> dict:
        return {"jev_calls": self.calls, "state_bytes": self.state_bytes, "usage": self.usage,
                "elapsed_ms": round(self.elapsed_ms, 1), "missing_field_answers": self.missing_field_answers}


def client_from_env(dry_run: bool, max_calls: int | None = None) -> Budgeted:
    if os.environ.get("TYPESAFE_API_KEY"):
        inner = JevClient()
    elif dry_run:
        inner = FakeJev()
    else:
        raise RuntimeError("TYPESAFE_API_KEY is not set; pass --dry-run to use the fake model")
    cap = max_calls if max_calls is not None else int(os.environ.get("AK_BOT_MAX_JEV_CALLS", "25"))
    return Budgeted(inner, cap)
