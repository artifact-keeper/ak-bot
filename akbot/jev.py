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

        choice/score: the model's own `confidence` (distribution concentration).
        noul: |2p-1|, i.e. distance from the 0.5 "cannot tell" point. This is
        our derivation, not a JEV field; documented so it can be revisited.
        """
        if self.type == "noul" and self.noul is not None:
            return abs(2 * self.noul - 1)
        return float(self.confidence or 0.0)

    @property
    def yes(self) -> bool:
        return self.type == "noul" and (self.noul or 0.0) >= 0.5

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
        timeout: float = 5.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("TYPESAFE_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
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
                    body = json.loads(resp.read().decode())
                elapsed = (time.monotonic() - t0) * 1000
                answers = {qid: Answer.parse(qid, a) for qid, a in (body.get("answers") or {}).items()}
                missing = set(questions) - set(answers)
                if missing:
                    raise JevError(200, f"response lacks answers for {sorted(missing)}")
                return Result(model=body.get("model", self.model), answers=answers,
                              usage=body.get("usage") or {}, elapsed_ms=elapsed)
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code in RETRYABLE and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(8.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.3))
                    continue
                raise JevError(e.code, text) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(8.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.3))
                    continue
                raise JevError(0, str(e)) from None


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


def client_from_env(dry_run: bool):
    if os.environ.get("TYPESAFE_API_KEY"):
        return JevClient()
    if dry_run:
        return FakeJev()
    raise RuntimeError("TYPESAFE_API_KEY is not set; pass --dry-run to use the fake model")
