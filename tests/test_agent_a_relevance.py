"""Tests for Agent A's service-line relevance, evidence-strength labelling and quote-length rules (scripted model)."""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from agent_a import pipeline as A  # noqa: E402
from core.orchestrator import Run  # noqa: E402
from test_agents import DOC, FakeLLM  # noqa: E402

SERVICE = "supply chain and distribution"


def handler(prompt):
    if "Plan research" in prompt:
        return {"angles": {a: [f"q {a}"] for a in A.ANGLES}}
    if "Extract up to 6" in prompt:
        return {"findings": [
            {"claim": "Revenue grew 11% to Rs 1,840 crore", "source_id": re.search(r"\[(S\d+)\]", prompt).group(1), "quote": "Revenue from operations grew 11 percent to Rs 1,840 crore"},
            {"claim": "Net debt is Rs 410 crore", "source_id": re.search(r"\[(S\d+)\]", prompt).group(1), "quote": "Net debt stands at Rs 410 crore"},
            {"claim": "Short fragment fact", "source_id": re.search(r"\[(S\d+)\]", prompt).group(1), "quote": "Rs 1,840 crore"},  # 3 words: too short to be evidence
        ]}
    if "VERIFIED FINDINGS" in prompt:
        return {"key_facts": [{"text": "Revenue grew 11% to Rs 1,840 crore", "finding_ids": ["F1"]}],
                "pain_points": [{"hypothesis": "Working capital is tied up in the distribution chain", "why": "net debt stands at Rs 410 crore", "service_fit": "distribution redesign", "finding_ids": ["F2"]},
                                {"hypothesis": "Share price volatility after results", "why": "revenue grew 11%", "service_fit": "none", "finding_ids": ["F1"]}]}
    if "strict fact-checker" in prompt:
        out = []
        for n, line in re.findall(r"#(\d+) \((?:key_fact|pain_point)\) CLAIM: ([^\n]*)", prompt):
            out.append({"n": int(n), "supported": True, "relevant": "Share price" not in line, "issue": ""})
        return {"verdicts": out}
    if "executive summary" in prompt:
        return {"summary": "Summary."}
    if "first-contact package" in prompt:
        return {"pitch_outline": ["a"], "proposed_scope": [], "discovery_questions": ["q"], "email": {"subject": "S", "body": "B"}}
    raise AssertionError("unexpected prompt: " + prompt[:80])


class TestRelevance(unittest.TestCase):
    def run_a(self, h=handler):
        llm = FakeLLM(h)
        run = Run("a", "t", {})
        return llm, run, A.run_agent(run, llm, "Bharat Agro Foods", SERVICE, DOC, use_web=False, auto_approve=True)

    def test_service_line_angle_always_searches_company_plus_service_line(self):
        _, run, _ = self.run_a()
        plan = next(e for e in run.events if e["actor"] == "Planner" and e["kind"] == "plan")["data"]
        self.assertEqual(plan["service"][0], f"Bharat Agro Foods {SERVICE}")
        self.assertIn("service", A.ANGLES)

    def test_irrelevant_pain_point_is_rejected_even_though_supported(self):
        _, run, res = self.run_a()
        hyps = [p["hypothesis"] for p in res["pain_points"]]
        self.assertIn("Working capital is tied up in the distribution chain", hyps)
        self.assertNotIn("Share price volatility after results", hyps)
        self.assertTrue(any(e["actor"] == "Critic" and "outside the service line" in str(e["data"]) for e in run.events))

    def test_synthesis_and_email_prompts_carry_the_service_line_constraints(self):
        llm, _, _ = self.run_a()
        syn = next(p for p in llm.prompts if "VERIFIED FINDINGS" in p)
        self.assertIn(SERVICE, syn)
        self.assertIn("Do NOT list things", syn)
        self.assertRegex(syn, r"F1 \[\w+\|strong\]: ")  # every finding is tagged with the angle that found it and its evidence strength
        em = next(p for p in llm.prompts if "first-contact package" in p)
        self.assertIn(f"Offer ONLY {SERVICE} work", em)
        self.assertIn("do not mention share-price moves", em)

    def test_short_quote_fragments_are_dropped(self):
        _, run, res = self.run_a()
        claims = " ".join(e["claim"] for p in res["key_facts"] + res["pain_points"] for e in p["evidence"])
        self.assertNotIn("Short fragment fact", claims)
        self.assertTrue(any("dropped as ungrounded" in e["msg"] and e["data"]["dropped"] >= 1 for e in run.events))

    def test_evidence_level_is_strong_for_documents(self):
        _, _, res = self.run_a()
        self.assertTrue(all(x["evidence_level"] == "strong" for x in res["key_facts"] + res["pain_points"]))
        self.assertIn("_(strong)_", res["markdown"])

    def test_news_only_evidence_is_labelled_headline(self):
        registry = A.Sources()
        news = registry.add({"title": "Headline", "url": "http://n", "text": "Titan shares fall 4 percent after the Q2 business update on Tuesday.", "kind": "news"})
        wiki = registry.add({"title": "Wikipedia: X", "url": "http://w", "text": "More than 80 percent of its total revenue comes from the jewellery segment.", "kind": "wiki"})

        def h(prompt):
            return {"findings": [
                {"claim": "shares fell", "source_id": news, "quote": "Titan shares fall 4 percent after the Q2 business update"},
                {"claim": "jewellery dominates", "source_id": wiki, "quote": "More than 80 percent of its total revenue comes from the jewellery segment"}]}
        out = A.extract_findings(Run("a", "t", {}), FakeLLM(h), "profile", [news, wiki], registry)
        self.assertEqual({f["source_id"]: f["strength"] for f in out}, {news: "headline", wiki: "strong"})

    def test_figures_not_in_evidence_are_still_rejected_in_code(self):
        by_id = {"F1": {"claim": "Revenue grew 11%", "quote": "Revenue grew 11 percent last year", "source_id": "S1"}}
        brief = {"key_facts": [{"text": "Revenue grew 18% last year", "finding_ids": ["F1"]}], "pain_points": []}
        v = A.critique(Run("a", "t", {}), FakeLLM(lambda p: {"verdicts": []}), brief, by_id, SERVICE)
        self.assertFalse(v[0]["ok"])
        self.assertIn("18", v[0]["issue"])

    def test_critic_fails_closed_when_it_returns_nothing(self):
        by_id = {"F1": {"claim": "Working capital days rose", "quote": "Net working capital days rose from 58 to 74", "source_id": "S1"}}
        brief = {"key_facts": [], "pain_points": [{"hypothesis": "Working capital is stretched", "why": "days rose", "finding_ids": ["F1"]}]}
        v = A.critique(Run("a", "t", {}), FakeLLM(lambda p: {"verdicts": []}), brief, by_id, SERVICE)
        self.assertFalse(v[0]["ok"])


class TestOverclaimGuard(unittest.TestCase):
    brief = {"summary": "s", "pain_points": [{"hypothesis": "Operational strain from expansion"}], "key_facts": []}

    def test_hypothesis_stated_as_fact_triggers_a_rewrite(self):
        calls = []

        def h(prompt):
            calls.append(prompt)
            body = "It is clear that you are struggling with stock." if len(calls) == 1 else "We would be keen to understand whether expansion has strained inventory."
            return {"pitch_outline": ["a"], "proposed_scope": [], "discovery_questions": [], "email": {"subject": "S", "body": body}}
        run = Run("a", "t", {})
        out = A.draft_proposal(run, FakeLLM(h), "X", SERVICE, self.brief)
        self.assertEqual(len(calls), 2)
        self.assertIn("overconfident phrase", calls[1])
        self.assertNotIn("clear", out["email"]["body"].lower())
        self.assertNotIn("email_warning", out)
        self.assertTrue(any(e["kind"] == "retry" for e in run.events))

    def test_persistent_overclaim_is_flagged_not_hidden(self):
        h = lambda prompt: {"pitch_outline": [], "proposed_scope": [], "discovery_questions": [], "email": {"subject": "S", "body": "Clearly you are facing problems."}}  # noqa: E731
        out = A.draft_proposal(Run("a", "t", {}), FakeLLM(h), "X", SERVICE, self.brief)
        self.assertIn("overconfident", out["email_warning"])

    def test_careful_draft_passes_first_time(self):
        calls = []

        def h(prompt):
            calls.append(1)
            return {"pitch_outline": ["a"], "proposed_scope": [], "discovery_questions": [], "email": {"subject": "S", "body": "We noticed 77 new stores and would like to understand how inventory is coping."}}
        out = A.draft_proposal(Run("a", "t", {}), FakeLLM(h), "X", SERVICE, self.brief)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("email_warning", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
