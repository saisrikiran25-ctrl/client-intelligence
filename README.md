# Client Intelligence Agent

A multi-step AI agent, built from scratch (custom orchestrator, no agent framework), that turns a company name into a **cited client briefing and a reviewed outreach email**. It has a live trace UI, a human approval gate and automated evals.

| | |
|---|---|
| Problem | Hours of manual prospect research before a first meeting |
| Architecture | Supervisor + parallel researchers + critic loop |
| Key idea | Every claim is quote-verified against source text **in code**, then LLM-judged |
| Human in the loop | Approves the outreach email before anything can be sent |
| Output | Cited client brief, pitch outline, scope, discovery questions and an outreach email |

Principle: **the model reads and writes, code decides, a human approves.**

Companion project: [document-to-decision-agent](https://github.com/saisrikiran25-ctrl/document-to-decision-agent) (accounts-payable automation built on the same core).

## How it works
1. **Research (parallel)**: six research angles run at once: profile, news, sector, competitors, signals, and one dedicated to the service line you type.
2. **Real sources**: the full Wikipedia article, Wikidata facts, the company's own website, live Google News headlines (older than 18 months are dropped; each source shows its date), and any document you paste or upload.
3. **Synthesize**: the model proposes findings and hypotheses, each with a quote from its source.
4. **Verify (code)**: a finding needs a quote that exists in the fetched text, quotes under 5 words are rejected, and every figure in a claim must appear in its cited evidence.
5. **Critic loop**: a critic checks the claims and sends weak ones back; the draft is revised until it passes.
6. **Human gate**: you approve the outreach email. Nothing is sent without that approval.

What stays honest:
- **Evidence is labelled**: each fact shows *strong* (Wikipedia, Wikidata, the company's site or your document) or *headline-only* (news headlines). Most news-based items are headline-only; say so when presenting.
- **Stays on your service line**: the synthesizer may only propose problems that service line can address, the critic rejects anything outside it, and the email may only offer that service. Hypotheses are never stated as facts in the email (an overconfident draft is rewritten or flagged for you to edit).

## Quick start
Requires Python 3.10+ and `pip install pypdf` (reads uploaded PDFs). Everything else is standard library.

```bash
cp .env.example .env        # then add your free Gemini key(s)
python server.py            # open http://localhost:8765
```

On Windows you can double-click `start_demo.bat`. Keys are read from the local `.env` file (git-ignored). **Never commit or share `.env`.**

Command line:
```bash
python cli.py a --company "Bharat Agro Foods" --doc samples/bharat_agro_foods.txt --llm gemini --no-web
```

| Backend | Setup |
|---|---|
| `gemini` | Free key from Google AI Studio in `.env` (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, ...). Default. |
| `claude` | Claude Code CLI logged in. Untested here. |
| `ollama` | Local model (`qwen2.5:3b-instruct`). Free and offline, but weak. |
| `replay` | Answers only from the on-disk cache: instant, deterministic demos. |

**Gemini free tier:** about 20 requests per day per model per key. The backend rotates keys on the same model before falling back to a weaker model, and caches every response.

## Demo script (about 3 minutes)
Company `Bharat Agro Foods`, *Load sample doc*, run. Watch the parallel researchers, ungrounded claims being rejected, the critic sending claims back, then approve the email. *Replay a saved run* (the recorded runs in `runs/`) is the offline fallback.

## Tests and evals
```bash
python -m unittest discover -s tests     # 32 deterministic tests, no model needed
python -m agent_a.evals --llm gemini     # grounding / recall / hallucination-trap scores
```
Results (Gemini, Nov 2026):

| Eval | Result |
|---|---|
| 2 cases (no web, provided document) | **2/2**: grounding 100%, required-fact recall 75% and 100%, 0 hallucination-trap hits |
| Live on Asian Paints (real company, no cache) | 41 real sources, 28 quote-verified findings, critic rejected 3 of 8 claims in round 1 and the revised draft passed 8/8 |

Known limits: evals are small; news evidence is headline-level (article bodies are not fetched); the web search tool returns nothing (Wikipedia, Wikidata, the company site, live news and pasted documents work); Wikidata can be wrong for some companies; the Claude CLI backend is untested; the free Gemini tier is quota-limited.

## Email (opt-in, off by default)
The UI has a Gmail setup dialog (needs a Gmail **App password**, never your normal password). A real send needs an explicit confirmation, only an approved email is eligible, and a recipient must be provided: the agent never guesses an address. A demo redirect address sends everything to one inbox, marked `[DEMO]`. The mailer is tested against a local SMTP sink; no real email is sent in tests.

## Layout
```
core/        llm.py (backends, key rotation, cache), orchestrator.py (Run, trace, gate, parallel),
             tools.py (free web tools), docs.py, exports.py, mailer.py
agent_a/     pipeline.py (the agent), evals.py
ui/          agent_a.html, style.css, common.js, mail.js
samples/     fictional company document
runs/        recorded runs (for replay) and eval results
docs/        presentation deck (.pptx), infographic (.html), speaker notes
server.py cli.py start_demo.bat tests/
```
`core/` is shared with the companion repo, so it also contains code used by the other agent (for example Agent B's output exports).

## Notes
- The sample company data is fictional.
- The mailer's own tests (`test_mailer.py`) live in the companion repo, because they exercise the shared mailer with that agent's fixtures.
