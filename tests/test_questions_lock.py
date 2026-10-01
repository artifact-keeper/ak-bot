"""Criteria wording is configuration. This lock refuses a reworded question
whose QUESTIONS_VERSION was not bumped. To accept a change:

    RELOCK=1 python -m unittest tests.test_questions_lock

after bumping the bot's QUESTIONS_VERSION."""
import json
import os
import unittest

from akbot.decisions import questions_hash
from akbot.bots import changelog_drafter, red_run_classifier, issue_triage, base_image_bump, pr_review_router, preflight_audit

LOCK = os.path.join(os.path.dirname(__file__), "questions.lock.json")
FIXED_LABELS = {"registry/npm", "registry/maven", "core", "web-ui", "ci"}


def current() -> dict:
    return {
        "changelog-drafter": (changelog_drafter.QUESTIONS_VERSION, questions_hash(changelog_drafter.questions())),
        "red-run-classifier": (red_run_classifier.QUESTIONS_VERSION, questions_hash(red_run_classifier.questions())),
        "issue-triage": (issue_triage.QUESTIONS_VERSION, questions_hash(issue_triage.questions(FIXED_LABELS))),
        "base-image-bump": (base_image_bump.QUESTIONS_VERSION, questions_hash(base_image_bump.questions())),
        "pr-review-router": (pr_review_router.QUESTIONS_VERSION, questions_hash(pr_review_router.questions())),
        "preflight-audit": (preflight_audit.QUESTIONS_VERSION, questions_hash(preflight_audit.questions())),
    }


class QuestionsLockTests(unittest.TestCase):
    def test_locked(self):
        cur = {k: list(v) for k, v in current().items()}
        if os.environ.get("RELOCK") or not os.path.exists(LOCK):
            json.dump(cur, open(LOCK, "w"), indent=2, sort_keys=True)
            return
        locked = json.load(open(LOCK))
        for bot, (version, digest) in cur.items():
            lv, ld = locked.get(bot, (None, None))
            if digest != ld:
                self.assertNotEqual(version, lv, f"{bot}: criteria wording changed but QUESTIONS_VERSION is still {version}; "
                                                 f"bump it and run RELOCK=1")
