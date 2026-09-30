import unittest
from unittest import mock

from akbot import gh
from akbot.bots import changelog_drafter as cd
from akbot.context import Context
from akbot.decisions import DecisionLog, AUTO, SUGGEST, SKIP
from akbot.jev import FakeJev

RAW_LOG = """Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.7011818Z [FAIL] pending CHANGELOG entries reference work that is NOT in v1.10.0..HEAD:
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.7081277Z     - #3124  - **CI builds the backend's test binaries once per run**
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.7152422Z     -> either the entry belongs to an already-released section
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.7446339Z [FAIL] commits in v1.10.0..HEAD are not described by the pending section:
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.7465818Z     - #3813  feat(repositories): add internal visibility state (#3813)
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.8896300Z     ~ #3971 exempt (dependency bump): chore: bump zstd from 0.13.3 to 0.14.0 (#3971)
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.9224071Z     - #4006  chore: bump rcgen from 0.13.2 to 0.14.10 (#4006)
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:25.9226196Z           not exempt: its subject reads as a dependency bump, but it touches backend/tests/common/ci_oidc_issuer.rs
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:27.3486817Z     -> add a fragment under changes/unreleased/ (changes/README.md), or check whether the entry
Pre-tag readiness\tUNKNOWN STEP\t2026-09-30T18:26:27.3533730Z \x1b[31mNOT READY\x1b[0m to cut from main@7c42891c: 2 blocking problem(s). Fix them on main before tagging.
"""


def lines():
    return [gh.ANSI_RE.sub("", gh.LOG_PREFIX_RE.sub("", ln)).rstrip() for ln in RAW_LOG.splitlines()]


class ParseTests(unittest.TestCase):
    def test_parse_real_shape(self):
        pf = cd.parse_preflight(1, lines())
        self.assertEqual(pf.range, "v1.10.0..HEAD")
        self.assertEqual([u.pr for u in pf.undocumented], [3813, 4006])
        self.assertIn("ci_oidc_issuer.rs", pf.undocumented[1].detail)
        self.assertEqual(pf.undocumented[0].detail, "")
        self.assertEqual(pf.unresolved, ["#3124 - **CI builds the backend's test binaries once per run**"])
        self.assertEqual((pf.verdict, pf.audited_ref, pf.audited_sha), ("NOT READY", "main", "7c42891c"))

    def test_lead_and_slug(self):
        self.assertEqual(cd.lead_sentence("fix(nuget): keep a virtual repository's V2 member (#4328)"),
                         "Keep a virtual repository's V2 member")
        self.assertEqual(cd.slugify("Keep a virtual repository's V2 member"), "keep-a-virtual-repository-s-v2")

    def test_description_skips_link_lines_and_headings(self):
        body = "## Summary\nCloses #12\n\nThe proxy returned an empty 200 for a missing recipe, so clients cached nothing useful and retried forever.\n\n- [ ] tests"
        self.assertTrue(cd.description_from_body(body).startswith("The proxy returned"))

    def test_render_validates(self):
        name, text = cd.render_fragment("Fixed", 3887, 4295, "Conan answers 404 for missing recipes", "Why and what.", True)
        self.assertEqual(name, "3887-conan-answers-404-for-missing-recipes.md")
        self.assertEqual(cd.validate_fragment(name, text), [])
        self.assertIn("issues: [#3887, #4295]", text)
        self.assertIn("(#3887, #4295)", text)
        self.assertIn("Upgrade note", text)

    def test_validate_rejects(self):
        self.assertTrue(cd.validate_fragment("Bad Name.md", "---\nsection: Nope\nissues: []\n---\n- x\n- y\n"))


def fake_pr(n, closes=(), title="fix(conan): answer 404 for missing recipes (#4295)"):
    return {"number": n, "title": title, "body": "Closes #3887\n\nMissing recipes returned an empty 200, so conan cached nothing and never retried; now 404.",
            "author": {"login": "brandonrc"}, "labels": [], "files": [{"path": "backend/src/api/handlers/conan.rs"}],
            "closingIssuesReferences": [{"number": c} for c in closes], "url": f"https://x/pull/{n}",
            "additions": 10, "deletions": 2}


class DecideTests(unittest.TestCase):
    def ctx(self, canned):
        return Context("o/r", FakeJev(canned), True, DecisionLog("t", True))

    @mock.patch.object(gh, "pr_diff", return_value="diff")
    @mock.patch.object(gh, "pr_view")
    def test_auto_draft(self, pv, _):
        pv.return_value = fake_pr(4295, closes=(3887,))
        ctx = self.ctx({"section": {"type": "choice", "choice": "Fixed", "probabilities": {"Fixed": 0.92}, "confidence": 0.9},
                        "user_visible": {"type": "noul", "noul": 0.95}, "breaking": {"type": "noul", "noul": 0.05}})
        d = cd.decide_one(ctx, cd.Undocumented(4295, "fix(conan): ..."), set())
        self.assertEqual(d.tier, AUTO)
        self.assertEqual(d.name, "3887-answer-404-for-missing-recipes.md")
        self.assertIn("section: Fixed", d.text)
        self.assertNotIn("Upgrade note", d.text)
        self.assertEqual(ctx.log.records[0].tier, AUTO)

    @mock.patch.object(gh, "pr_diff", return_value="diff")
    @mock.patch.object(gh, "pr_view")
    def test_not_user_visible_is_a_human_call(self, pv, _):
        pv.return_value = fake_pr(4006, title="chore: bump rcgen (#4006)")
        ctx = self.ctx({"section": {"type": "choice", "choice": "none_of_the_above", "probabilities": {"none_of_the_above": 0.8}, "confidence": 0.8},
                        "user_visible": {"type": "noul", "noul": 0.1}})
        d = cd.decide_one(ctx, cd.Undocumented(4006, "chore: bump rcgen (#4006)", "touches tests"), set())
        self.assertEqual(d.name, "")
        self.assertIn("human call", d.note)

    @mock.patch.object(gh, "pr_diff", return_value="diff")
    @mock.patch.object(gh, "pr_view")
    def test_existing_fragment_skips(self, pv, _):
        pv.return_value = fake_pr(4295, closes=(3887,))
        ctx = self.ctx({"section": {"type": "choice", "choice": "Fixed", "probabilities": {"Fixed": 0.92}, "confidence": 0.9},
                        "user_visible": {"type": "noul", "noul": 0.95}})
        d = cd.decide_one(ctx, cd.Undocumented(4295, "x"), {"3887-something.md"})
        self.assertEqual(d.tier, SKIP)

    @mock.patch.object(gh, "pr_diff", return_value="diff")
    @mock.patch.object(gh, "pr_view")
    def test_uncertain_section_is_listed_not_drafted(self, pv, _):
        pv.return_value = fake_pr(1)
        ctx = self.ctx({"user_visible": {"type": "noul", "noul": 0.95}})   # section left uniform
        d = cd.decide_one(ctx, cd.Undocumented(1, "x"), set())
        self.assertEqual(d.tier, SKIP)
        self.assertEqual(d.name, "")


if __name__ == "__main__":
    unittest.main()


class DescriptionTests(unittest.TestCase):
    def test_strips_bold_and_prefers_prose_over_list_items(self):
        body = ("## Summary\n\n- **Rebased onto current `main`** (69 commits). Conflicts resolved.\n"
                "- **Migration renumbered** 235 -> 245.\n\n"
                "Orphaned repository tokens are recovered by migration 237, so tokens whose repositories were deleted stay restricted.\n")
        d = cd.description_from_body(body)
        self.assertTrue(d.startswith("Orphaned repository tokens"))
        self.assertNotIn("**", d)

    def test_list_item_is_fallback_and_length_capped(self):
        body = "- **Only a list item here** that is long enough to be used as the seed for the draft.\n"
        d = cd.description_from_body(body)
        self.assertTrue(d.startswith("Only a list item here"))
        long = "Sentence one is here and long enough. " * 30
        self.assertLessEqual(len(cd.description_from_body(long)), 400)
        self.assertTrue(cd.description_from_body(long).endswith("."))
