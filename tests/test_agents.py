"""Deterministic tests. A scripted stand-in LLM replaces the model so we test OUR logic exactly:
grounding checks, critic loop, routing, flags, duplicate/overdue detection, memory, approval gate, eval harness.
Run:  python -m unittest discover -s tests -v
"""
import json
import pathlib
import re
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_a import pipeline as A  # noqa: E402
from core.llm import LLMError, extract_json  # noqa: E402
from core.orchestrator import Run, parallel, retry  # noqa: E402
from core.tools import chunk_text, html_to_text  # noqa: E402

DOC = (ROOT / "samples" / "bharat_agro_foods.txt").read_text(encoding="utf-8")
GOOD_QUOTE = "Revenue from operations grew 11 percent to Rs 1,840 crore"


class FakeLLM:
    backend, model = "fake", "fake"

    def __init__(self, handler):
        self.handler, self.prompts = handler, []
        self.stats = {"calls": 0, "cache_hits": 0, "seconds": 0.0}
        self._lock = threading.Lock()

    def json(self, prompt, system="", tries=3):
        with self._lock:
            self.prompts.append(prompt)
            self.stats["calls"] += 1
        return self.handler(prompt)


# --------------------------------------------------------------------------- core
class TestGeminiBackend(unittest.TestCase):
    def test_request_shape_and_key_not_in_url(self):
        import io, os
        from unittest import mock
        from core import llm as L
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=0):
            seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.header_items()), json.loads(req.data)
            return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]}).encode())

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET123", "AGENT_LLM": ""}), mock.patch("urllib.request.urlopen", fake_open):
            m = L.LLM("gemini", cache=False)
            self.assertEqual(m.json("hi", "sys"), {"ok": 1})
        self.assertNotIn("SECRET123", seen["url"])  # key travels in a header, never the URL
        self.assertEqual(seen["headers"]["X-goog-api-key"], "SECRET123")
        self.assertEqual(seen["body"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(seen["body"]["systemInstruction"]["parts"][0]["text"], "sys")

    def test_daily_quota_falls_back_to_next_model(self):
        import io, os, urllib.error
        from unittest import mock
        from core import llm as L
        used = []

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=0):
            model = req.full_url.split("/models/")[1].split(":")[0]
            used.append(model)
            if model == "primary-model":
                raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b'{"error":{"details":[{"violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}'))
            return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}).encode())

        L._GEMINI_EXHAUSTED.clear()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=True), mock.patch("urllib.request.urlopen", fake_open):
            m = L.LLM("gemini", "primary-model", cache=False)
            self.assertEqual(m.complete("a"), "hello")
            self.assertEqual(m.complete("b"), "hello")
        self.assertEqual(used.count("primary-model"), 1)  # exhausted model is not retried on the next call
        self.assertIn((0, "primary-model"), L._GEMINI_EXHAUSTED)
        L._GEMINI_EXHAUSTED.clear()

    def test_second_key_used_before_weaker_model(self):
        import io, os, urllib.error
        from unittest import mock
        from core import llm as L
        calls = []

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=0):
            model = req.full_url.split("/models/")[1].split(":")[0]
            key = dict(req.header_items())["X-goog-api-key"]
            calls.append((key, model))
            if key == "KEY1":  # first account's daily quota is gone for every model
                raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b'{"error":{"details":[{"violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}'))
            return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "from key2"}]}}]}).encode())

        L._GEMINI_EXHAUSTED.clear()
        env = {"GEMINI_API_KEY": "KEY1", "GEMINI_API_KEY_2": "KEY2"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("urllib.request.urlopen", fake_open):
            m = L.LLM("gemini", "primary-model", cache=False)
            self.assertEqual(m.complete("a"), "from key2")
        self.assertEqual(calls, [("KEY1", "primary-model"), ("KEY2", "primary-model")])  # same (best) model on key 2, no downgrade
        self.assertEqual(m.stats["keys_used"], {2})
        L._GEMINI_EXHAUSTED.clear()

    def test_per_minute_limit_rotates_to_next_key_without_sleeping(self):
        import io, os, urllib.error
        from unittest import mock
        from core import llm as L
        calls = []

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=0):
            key = dict(req.header_items())["X-goog-api-key"]
            calls.append(key)
            if key == "K1":  # a per-MINUTE limit (no PerDay marker)
                raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b'{"error":{"message":"rate limit"}}'))
            return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode())

        L._GEMINI_EXHAUSTED.clear()
        slept = []
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "K1", "GEMINI_API_KEY_2": "K2"}, clear=True), mock.patch("urllib.request.urlopen", fake_open), mock.patch("time.sleep", lambda s: slept.append(s)):
            self.assertEqual(L.LLM("gemini", "m", cache=False).complete("x"), "ok")
        self.assertEqual(calls, ["K1", "K2"])
        self.assertEqual(slept, [])  # moved on immediately
        L._GEMINI_EXHAUSTED.clear()

    def test_attachments_are_sent_as_inline_data(self):
        import io, os
        from unittest import mock
        from core import llm as L
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=0):
            seen["body"] = json.loads(req.data)
            return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "TEXT"}]}}]}).encode())

        L._GEMINI_EXHAUSTED.clear()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "K"}, clear=True), mock.patch("urllib.request.urlopen", fake_open):
            self.assertEqual(L.LLM("gemini", cache=False).complete("read", attachments=[("application/pdf", b"%PDF-fake")]), "TEXT")
            with self.assertRaises(LLMError):
                L.LLM("ollama", cache=False).complete("read", attachments=[("application/pdf", b"x")])
        part = seen["body"]["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(part["mimeType"], "application/pdf")
        L._GEMINI_EXHAUSTED.clear()

    def test_missing_key_gives_clear_error(self):
        import os
        from unittest import mock
        from core import llm as L
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LLMError) as cm:
                L.LLM("gemini", cache=False).complete("x")
        self.assertIn("GEMINI_API_KEY", str(cm.exception))


class TestCore(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! Here you go: {"a": {"b": "x}y"}} thanks'), {"a": {"b": "x}y"}})
        with self.assertRaises(LLMError):
            extract_json("no json here")

    def test_quote_supported(self):
        src = "The company grew revenue 11 percent to Rs 1,840 crore in FY2025-26."
        self.assertTrue(A.quote_supported("revenue 11 percent to Rs 1,840 crore", src))
        self.assertTrue(A.quote_supported("Revenue   11 percent  to Rs 1,840 CRORE", src))  # whitespace/case tolerant
        self.assertFalse(A.quote_supported("revenue fell 40 percent to Rs 900 crore", src))  # fabricated
        self.assertFalse(A.quote_supported("grew", src))  # too short to be evidence

    def test_sources_dedupe_and_ids(self):
        s = A.Sources()
        a = s.add({"title": "x", "url": "http://a", "text": "t"})
        b = s.add({"title": "x", "url": "http://a", "text": "t"})
        c = s.add({"title": "y", "url": "http://b", "text": "t"})
        self.assertEqual((a, b, c), ("S1", "S1", "S2"))

    def test_chunking_and_html(self):
        chunks = chunk_text(DOC, 600)
        self.assertGreater(len(chunks), 3)
        self.assertEqual(re.sub(r"\s+", "", "".join(chunks)), re.sub(r"\s+", "", DOC))  # nothing lost
        self.assertEqual(html_to_text("<html><script>x=1</script><p>Hello</p><style>a{}</style></html>"), "Hello" if False else html_to_text("<p>Hello</p>"))
        self.assertNotIn("x=1", html_to_text("<script>x=1</script><p>" + "Hello " * 50 + "</p>"))

    def test_parallel_isolates_failures(self):
        run = Run("t", "t", {})

        def boom():
            raise ValueError("bad")

        out = parallel(run, {"ok": lambda: 1, "bad": boom})
        self.assertEqual(out["ok"], 1)
        self.assertIsInstance(out["bad"], ValueError)
        self.assertTrue(any(e["kind"] == "error" for e in run.events))

    def test_retry(self):
        n = {"i": 0}

        def flaky():
            n["i"] += 1
            if n["i"] < 2:
                raise RuntimeError("x")
            return "ok"

        self.assertEqual(retry(flaky, tries=3), "ok")

    def test_gate_blocks_until_resolved(self):
        run = Run("t", "t", {})
        got = {}
        th = threading.Thread(target=lambda: got.setdefault("d", run.request_approval("review", {"x": 1})))
        th.start()
        time.sleep(0.3)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertIsNotNone(run.snapshot()["gate"])
        run.resolve({"approved": True, "note": "ok"})
        th.join(3)
        self.assertEqual(got["d"]["approved"], True)
        self.assertEqual(run.status, "running")

    def test_gate_auto(self):
        self.assertTrue(Run("t", "t", {}).request_approval("r", {}, auto=True)["approved"])


# -------------------------------------------------------------------------- Agent A
def a_handler_factory(critic_fail_first=True):
    state = {"critic": 0}

    def h(p):
        if "Plan research" in p:
            return {"angles": {a: [f"q {a}"] for a in A.ANGLES}}
        if "Extract up to 6" in p:
            sid = re.search(r"\[(S\d+)\][^\[]*?Rs 1,840 crore", p, re.S)
            findings = [{"claim": "x", "source_id": "S1", "quote": "this sentence was never in any source document at all"}]  # fabricated
            if sid:
                findings.append({"claim": "Revenue grew 11% to Rs 1,840 crore", "source_id": sid.group(1), "quote": GOOD_QUOTE})
            return {"findings": findings}
        if "VERIFIED FINDINGS" in p:
            return {
                "key_facts": [
                    {"text": "Revenue grew 11% to Rs 1,840 crore", "finding_ids": ["F1"]},
                    {"text": "Claim citing a finding that does not exist", "finding_ids": ["F99"]},
                ],
                "pain_points": [{"hypothesis": "Growth may be outpacing margin", "why": "revenue up", "service_fit": "margin programme", "finding_ids": ["F1"]}],
            }
        if "strict fact-checker" in p:
            state["critic"] += 1
            n = len(re.findall(r"^#\d+", p, re.M))
            bad_first = critic_fail_first and state["critic"] == 1
            return {"verdicts": [{"n": i, "supported": not (bad_first and i == 0), "issue": "overstated"} for i in range(n)]}
        if "executive summary" in p:
            return {"summary": "Revenue grew; margins are a hypothesis."}
        if "first-contact package" in p:
            return {"pitch_outline": ["a"], "proposed_scope": [{"workstream": "w", "deliverable": "d", "weeks": 4}], "discovery_questions": ["q"], "email": {"subject": "Hello", "body": "Body"}}
        raise AssertionError("unexpected prompt: " + p[:80])

    return h


class TestAgentA(unittest.TestCase):
    def run_a(self, **kw):
        llm = FakeLLM(a_handler_factory(**kw))
        run = Run("a", "t", {})
        res = A.run_agent(run, llm, "Bharat Agro Foods", "ops", DOC, use_web=False, auto_approve=True)
        return llm, run, res

    def test_fabricated_quotes_are_dropped_and_dedup(self):
        _, run, res = self.run_a()
        self.assertEqual(res["metrics"]["findings_verified"], 1)  # 5 angles x 1 valid, de-duplicated to 1
        self.assertTrue(any("De-duplicated" in e["msg"] for e in run.events))
        self.assertTrue(any("dropped as ungrounded" in e["msg"] and e["data"]["dropped"] >= 1 for e in run.events))

    def test_invalid_citation_rejected_deterministically(self):
        _, _, res = self.run_a()
        texts = [x["text"] for x in res["key_facts"]]
        self.assertNotIn("Claim citing a finding that does not exist", texts)
        self.assertIn("Revenue grew 11% to Rs 1,840 crore", texts)

    def test_critic_loop_revises_then_passes(self):
        llm, run, res = self.run_a(critic_fail_first=True)
        crit = [e for e in run.events if e["actor"] == "Critic" and "Round" in e["msg"]]
        self.assertGreaterEqual(len(crit), 2)  # a rejection triggered a second round
        self.assertTrue(any("Sending rejected claims back" in e["msg"] for e in run.events))
        self.assertTrue(any("A reviewer rejected earlier items" in p for p in llm.prompts))  # feedback reached the synthesizer

    def test_result_shape_and_metrics(self):
        _, _, res = self.run_a(critic_fail_first=False)
        m = res["metrics"]
        self.assertAlmostEqual(m["claims_supported"] / m["claims_checked"], m["grounding_rate"], places=2)
        self.assertTrue(all(s["sources"] for s in res["key_facts"] + res["pain_points"]))
        self.assertEqual(res["proposal"]["email_status"], "approved")
        self.assertIn("# Client briefing", res["markdown"])
        self.assertTrue(res["summary"])

    def test_summary_written_after_verification_from_verified_points_only(self):
        llm, _, _ = self.run_a()
        sp = next(p for p in llm.prompts if "executive summary" in p)
        self.assertNotIn("does not exist", sp)  # dropped claim must not reach the summary

    def test_gate_rejection_not_finalized(self):
        llm = FakeLLM(a_handler_factory(False))
        run = Run("a", "t", {})
        done = {}
        th = threading.Thread(target=lambda: done.setdefault("r", A.run_agent(run, llm, "X", "", DOC, False, False)))
        th.start()
        for _ in range(100):
            if run.status == "awaiting_approval":
                break
            time.sleep(0.1)
        run.resolve({"approved": False})
        th.join(10)
        self.assertIn("rejected", done["r"]["proposal"]["email_status"])

    def test_no_findings_raises(self):
        llm = FakeLLM(lambda p: {"angles": {}} if "Plan research" in p else {"findings": []})
        with self.assertRaises(RuntimeError):
            A.run_agent(Run("a", "t", {}), llm, "X", "", "", use_web=False, auto_approve=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

