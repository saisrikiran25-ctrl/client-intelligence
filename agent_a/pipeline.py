"""Agent A: Client Intelligence & Proposal Agent.

Supervisor + parallel researchers + grounded critic loop + human approval gate.

Grounding is verified DETERMINISTICALLY wherever possible: a researcher's quote must
literally exist in its source text, otherwise the finding is thrown away. Only the
synthesis step (which is inference) is judged by an LLM critic.
"""
from __future__ import annotations

import difflib
import re
import threading

from core import tools
from core.llm import LLM
from core.orchestrator import Run, parallel

ANGLES = {
    "profile": "company profile, business model, financial performance",
    "news": "recent news, leadership moves, strategic announcements",
    "sector": "industry trends and regulation affecting the company in India",
    "competitors": "competitors and market position",
    "signals": "hiring, technology and expansion signals",
    "service": "operational problems, initiatives, investments and risks in the service area the client would buy",
}
STRONG_KINDS = ("wiki", "wikidata", "company-site", "document")  # full-text sources; news is headline-level
MIN_QUOTE_WORDS = 5
# a hypothesis must never be presented to the client as established fact
OVERCLAIM = re.compile(r"\b(it is clear|clearly|obviously|undoubtedly|evidently|you are (?:struggling|facing|suffering)|is struggling|has introduced significant)\b", re.I)

SYS = "You are a precise senior research analyst at an Indian management consulting firm. You never invent facts. Reply with JSON only."


# --------------------------------------------------------------------- helpers
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%.,-]", " ", s.lower())).strip()


def quote_supported(quote: str, source_text: str, threshold: float = 0.85) -> bool:
    """True if `quote` appears (near-)verbatim in the source text."""
    q, s = _norm(quote), _norm(source_text)
    if len(q) < 12:
        return False
    if q in s:
        return True
    m = difflib.SequenceMatcher(None, s, q, autojunk=False).find_longest_match(0, len(s), 0, len(q))
    return m.size / len(q) >= threshold


class Sources:
    """Thread-safe source registry that hands out stable S1, S2... ids."""

    def __init__(self):
        self._lock = threading.Lock()
        self.items: dict[str, dict] = {}
        self._by_key: dict[str, str] = {}

    def add(self, item: dict) -> str:
        key = item.get("url") or item["title"] + item["text"][:60]
        with self._lock:
            if key in self._by_key:
                return self._by_key[key]
            sid = f"S{len(self.items) + 1}"
            self.items[sid] = {**item, "id": sid}
            self._by_key[key] = sid
            return sid


def _score(chunk: str, words: set[str]) -> int:
    return len(words & set(_norm(chunk).split()))


# ----------------------------------------------------------------------- stages
def plan(run: Run, llm: LLM, company: str, service_line: str, doc_hint: str) -> dict:
    default = {
        "profile": [f"{company} company overview", f"{company} annual report revenue"],
        "news": [company],
        "sector": [f"{company} industry India outlook regulation"],
        "competitors": [f"{company} competitors India"],
        "signals": [f"{company} hiring technology expansion"],
        "service": [],
    }
    focus = (service_line or "operations").strip()
    prompt = (
        f"Company: {company}\nService line of interest: {service_line or 'general'}\n"
        f"Known context: {doc_hint[:300] or 'none'}\n\n"
        "Plan research for a first-meeting briefing. For each angle give 1-2 short web search queries.\n"
        f"Angles: {', '.join(ANGLES)}.\n"
        f'The "service" angle must be about problems and initiatives in: {focus}.\n'
        'Reply as JSON: {"angles": {"profile": ["..."], "news": ["..."], "sector": ["..."], "competitors": ["..."], "signals": ["..."], "service": ["..."]}}'
    )
    try:
        got = llm.json(prompt, SYS)["angles"]
        queries = {a: [q for q in got.get(a, []) if isinstance(q, str)][:2] or default[a] for a in ANGLES}
    except Exception as e:  # noqa: BLE001
        run.log("Planner", "retry", f"planner JSON failed ({e}); using default query plan")
        queries = default
    # deterministic guarantee: the service-line angle always searches for the company together with the service line itself
    queries["service"] = list(dict.fromkeys([f"{company} {focus}", f"{company} {focus} challenges"] + queries.get("service", [])))[:3]
    run.log("Planner", "plan", f"Decomposed into {len(queries)} research angles", queries)
    return queries


def collect(run: Run, company: str, use_web: bool) -> list[dict]:
    """Fetch the shared, real source pool once: Wikipedia article, Wikidata facts, the company's own website."""
    if not use_web:
        return []
    pool: list[dict] = []
    wp = tools.wikipedia_full(company)
    if wp and wp["text"]:
        for i, c in enumerate(tools.chunk_text(wp["text"], 1500)[:14]):
            pool.append({"title": f"{wp['title']} (part {i + 1})", "url": wp["url"], "text": c, "kind": "wiki"})
    wd = tools.wikidata_facts(company)
    if wd:
        pool.append(wd)
    site = tools.official_site((wd or {}).get("website"))
    pool += site
    run.log("Collector", "tool", f"Fetched {len(pool)} real source passages: Wikipedia {'yes' if wp else 'no'}, Wikidata {'yes' if wd else 'no'}, official website pages {len(site)}", [p["title"] for p in pool][:8])
    return pool


def gather(run: Run, angle: str, queries: list[str], doc_chunks: list[str], registry: Sources, use_web: bool, pool: list[dict] | None = None) -> list[str]:
    """Collect source ids for one angle: live news for its queries, the best-matching passages of the shared pool, and the user's documents."""
    actor = f"Researcher:{angle}"
    ids: list[str] = []
    words = set(_norm(" ".join(queries) + " " + ANGLES[angle]).split())
    if use_web:
        for q in queries:
            batch = tools.news_rss(q, 5)
            run.log(actor, "tool", f"live news search \"{q}\" -> {len(batch)} headlines", [b["title"] for b in batch][:5])
            ids += [registry.add(b) for b in batch if b["text"]]
        ranked = sorted(pool or [], key=lambda c: -_score(c["text"], words))
        picked = [c for c in ranked if _score(c["text"], words) > 0][:3]
        picked += [c for c in (pool or []) if c["kind"] in ("wikidata", "company-site") and c not in picked][:2 if angle == "profile" else 1]
        for c in picked:
            ids.append(registry.add(dict(c)))
        run.log(actor, "tool", f"selected {len(picked)} passages from Wikipedia / Wikidata / company website", [c["title"] for c in picked])
    ranked_docs = sorted(doc_chunks, key=lambda c: -_score(c, words))[:3]
    for i, c in enumerate(ranked_docs):
        if _score(c, words) > 0:
            ids.append(registry.add({"title": f"Provided document, part {i + 1}", "url": "", "text": c, "kind": "document"}))
    return list(dict.fromkeys(ids))[:7]


def extract_findings(run: Run, llm: LLM, angle: str, ids: list[str], registry: Sources) -> list[dict]:
    actor = f"Researcher:{angle}"
    if not ids:
        run.log(actor, "llm", "no sources found for this angle")
        return []
    block = "\n\n".join(f"[{i}] {registry.items[i]['title']}\n{registry.items[i]['text'][:1700]}" for i in ids)
    prompt = (
        f"Angle: {ANGLES[angle]}\n\nSOURCES:\n{block}\n\n"
        "Extract up to 6 concrete, decision-relevant facts (numbers, names, dates, decisions) from the sources.\n"
        'For each, copy an EXACT quote (5 to 25 words, a full clause, not a fragment) from the source. Reply as JSON: '
        '{"findings": [{"claim": "one-sentence fact", "source_id": "S1", "quote": "exact words from the source"}]}'
    )
    kept: list[dict] = []
    dropped = 0
    for attempt in range(2):
        raw = llm.json(prompt, SYS).get("findings", [])
        for f in raw if isinstance(raw, list) else []:
            sid = str(f.get("source_id", "")).strip()
            q = str(f.get("quote", ""))
            if sid in registry.items and f.get("claim") and len(q.split()) >= MIN_QUOTE_WORDS and quote_supported(q, registry.items[sid]["text"]):
                kept.append({"claim": f["claim"].strip(), "source_id": sid, "quote": f["quote"].strip(), "angle": angle,
                             "strength": "strong" if registry.items[sid]["kind"] in STRONG_KINDS else "headline"})
            else:
                dropped += 1
        if kept:
            break
        run.log(actor, "retry", "no finding had a verifiable quote; asking again with stricter instructions")
        prompt += "\n\nIMPORTANT: your previous quotes could not be found in the sources. Copy quotes character-for-character."
    run.log(actor, "llm", f"{len(kept)} findings verified against source text, {dropped} dropped as ungrounded", {"kept": len(kept), "dropped": dropped})
    return kept


def synthesize(run: Run, llm: LLM, company: str, service_line: str, findings: list[dict], feedback: str = "") -> dict:
    focus = (service_line or "general business improvement").strip()
    fl = "\n".join(f"{f['id']} [{f['angle']}|{f['strength']}]: {f['claim']}" for f in findings)
    prompt = (
        f"Company: {company}. We are a consulting firm selling: {focus}.\n\nVERIFIED FINDINGS (id [angle|evidence strength]):\n{fl}\n\n"
        "Write a diagnostic briefing for a consulting partner. Use ONLY the findings; every item must cite finding ids.\n"
        f"RELEVANCE RULES: key_facts must be the facts most useful for a conversation about {focus}. A pain_point must be a likely problem that a {focus} engagement "
        f"could actually address (an operational or process problem). Do NOT list things {focus} cannot fix, such as share-price moves, analyst sentiment or general market commentary. "
        "Prefer findings tagged [service]. Prefer findings with 'strong' evidence; use 'headline' findings only when nothing stronger supports the point. "
        "If the findings support fewer than 3 relevant pain points, return fewer. Never pad with irrelevant ones.\n"
        'Reply as JSON: {'
        '"key_facts": [{"text": "...", "finding_ids": ["F1"]}], '
        '"pain_points": [{"hypothesis": "likely problem the client has", "why": "reasoning from the evidence", "service_fit": "how our consulting could help", "finding_ids": ["F2","F3"]}]}\n'
        "Give 4-6 key_facts and up to 4 pain_points."
    )
    if feedback:
        prompt += f"\n\nA reviewer rejected earlier items:\n{feedback}\nFix them or drop them. Do not add unsupported facts."
    return llm.json(prompt, SYS)


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _nums(text: str) -> set[str]:
    out = set()
    for m in _NUM.findall(text or ""):
        v = m.replace(",", "").rstrip(".")
        if len(v.replace(".", "")) >= 2 or "." in v:
            out.add(v)
    return out


def critique(run: Run, llm: LLM, brief: dict, by_id: dict, service_line: str = "") -> list[dict]:
    """Two layers: (1) code - every figure in a claim must appear in the cited evidence; (2) LLM judge for everything else."""
    items = [("key_fact", i, x) for i, x in enumerate(brief.get("key_facts", []))] + [("pain_point", i, x) for i, x in enumerate(brief.get("pain_points", []))]
    verdicts: list[dict] = []
    to_judge = []
    for kind, i, x in items:
        text = x.get("text") or x.get("hypothesis", "")
        why = x.get("why", "")
        ids = [fid for fid in x.get("finding_ids", []) if fid in by_id]
        if not ids:
            verdicts.append({"kind": kind, "idx": i, "ok": False, "issue": "cites no valid finding id"})
            continue
        evidence = " ".join(by_id[f]["claim"] + " " + by_id[f]["quote"] for f in ids)
        missing = sorted(_nums(text + " " + why) - _nums(evidence))
        if missing:
            verdicts.append({"kind": kind, "idx": i, "ok": False, "issue": f"figure(s) {missing} do not appear in the cited evidence"})
            continue
        to_judge.append((kind, i, text, why, ids))
    if to_judge:
        lines = []
        for n, (kind, i, text, why, ids) in enumerate(to_judge):
            ev = " | ".join(by_id[f]["claim"] + f' (quote: "{by_id[f]["quote"]}")' for f in ids)
            lines.append(f"#{n} ({kind}) CLAIM: {text} {why}\n   EVIDENCE: {ev}")
        prompt = (
            "You are a strict fact-checker. For each numbered item decide whether EVERYTHING it asserts is supported by its EVIDENCE.\n"
            "Mark it unsupported if it names any specific cause, practice, programme, product, person, place or number that the evidence does not contain "
            "(example: blaming 'lack of procurement hedging' when the evidence never mentions hedging). Hedged wording such as 'may' or 'likely' does NOT excuse an unsupported specific.\n"
            "A pain_point is allowed to infer a likely problem, but only from facts present in the evidence, and its 'why' must restate those facts accurately.\n"
            f"Also set relevant=false for a pain_point that a '{service_line or 'general'}' consulting engagement could NOT address (for example share-price moves or analyst sentiment). Key facts are always relevant=true.\n\n" + "\n".join(lines) +
            '\n\nReply as JSON: {"verdicts": [{"n": 0, "supported": true, "relevant": true, "issue": ""}]}'
        )
        try:
            res = {int(v["n"]): v for v in llm.json(prompt, SYS).get("verdicts", []) if "n" in v}
        except Exception as e:  # noqa: BLE001
            run.log("Critic", "retry", f"critic JSON failed ({e}); every unjudged claim is rejected (fail closed)")
            res = {}
        for n, (kind, i, *_rest) in enumerate(to_judge):
            v = res.get(n)
            ok = bool(v.get("supported")) and (kind != "pain_point" or v.get("relevant", True) is not False) if v else False
            why_not = (v or {}).get("issue") or ("outside the service line" if v and v.get("relevant") is False else "not judged")
            verdicts.append({"kind": kind, "idx": i, "ok": ok, "issue": why_not})
    return verdicts


def write_summary(run: Run, llm: LLM, company: str, brief: dict) -> str:
    facts = [x.get("text", "") for x in brief.get("key_facts", [])] + [x.get("hypothesis", "") for x in brief.get("pain_points", [])]
    if not facts:
        return "No claims passed verification, so no summary is given."
    bullets = chr(10).join(f"- {f}" for f in facts)
    prompt = (
        f"Company: {company}" + chr(10) + "VERIFIED POINTS:" + chr(10) + bullets + chr(10) * 2 +
        "Write a 2-sentence executive summary using ONLY these points. Add no new facts or numbers." + chr(10) +
        'Reply as JSON: {"summary": "..."}'
    )
    try:
        out = llm.json(prompt, SYS).get("summary", "")
        run.log("Synthesizer", "llm", "Wrote executive summary from verified points only")
        return out
    except Exception:  # noqa: BLE001
        return " ".join(facts[:2])


def draft_proposal(run: Run, llm: LLM, company: str, service_line: str, brief: dict) -> dict:
    focus = service_line or "general consulting"
    pp = "\n".join(f"- {p.get('hypothesis')}" for p in brief.get("pain_points", [])) or "\n".join(f"- (fact) {x.get('text')}" for x in brief.get("key_facts", []))
    prompt = (
        f"Client: {company}. Our firm sells: {focus}.\nSummary: {brief.get('summary', '')}\nVerified points:\n{pp}\n\n"
        f"Draft the first-contact package from our Indian consulting firm. Mention only the points above; do not invent facts or numbers. Offer ONLY {focus} work: "
        "never claim we can influence share prices, markets, demand or anything outside that service line, and do not mention share-price moves at all.\n"
        "State only public observations as facts. Any possible problem is a HYPOTHESIS: phrase it as a question or as 'we would be keen to understand whether...', never as a certainty "
        "(do not write 'it is clear', 'clearly', 'you are struggling' and the like).\n"
        'Reply as JSON: {"pitch_outline": ["5 short bullets"], '
        '"proposed_scope": [{"workstream": "", "deliverable": "", "weeks": 4}], '
        '"discovery_questions": ["6 sharp questions for the first meeting"], '
        '"email": {"subject": "", "body": "120-word personalised outreach email, no invented facts, signed as [Your Name]"}}'
    )
    out = llm.json(prompt, SYS)
    for attempt in range(2):
        text = " ".join([out.get("email", {}).get("body", "")] + [str(b) for b in out.get("pitch_outline", [])])
        m = OVERCLAIM.search(text)
        if not m:
            break
        run.log("Drafter", "retry", f"draft stated a hypothesis as fact (\"{m.group(0)}\"); asking for a rewrite that phrases problems as questions")
        out = llm.json(prompt + f"\n\nYour previous draft contained the overconfident phrase \"{m.group(0)}\". Rewrite it so every possible problem is a question or an explicit hypothesis, not a fact.", SYS)
    else:
        if OVERCLAIM.search(" ".join([out.get("email", {}).get("body", "")])):
            out["email_warning"] = "The draft still contains overconfident wording; edit it before sending."
            run.log("Drafter", "error", out["email_warning"])
    return out


# ------------------------------------------------------------------------ main
def run_agent(run: Run, llm: LLM, company: str, service_line: str = "", docs: str = "", use_web: bool = True, auto_approve: bool = False, max_rounds: int = 2) -> dict:
    registry = Sources()
    chunks = tools.chunk_text(docs) if docs.strip() else []
    run.log("Orchestrator", "agent", f"Agent A started for \"{company}\"", {"web": use_web, "doc_chunks": len(chunks), "llm": f"{llm.backend}:{llm.model}"})

    queries = plan(run, llm, company, service_line, chunks[0] if chunks else "")
    pool = collect(run, company, use_web)

    # --- parallel research -------------------------------------------------
    def research(angle):
        def _job():
            ids = gather(run, angle, queries[angle], chunks, registry, use_web, pool)
            return extract_findings(run, llm, angle, ids, registry)
        return _job

    results = parallel(run, {f"Researcher:{a}": research(a) for a in ANGLES}, workers=6)
    findings: list[dict] = []
    for r in results.values():
        if isinstance(r, list):
            findings += r
    seen, unique = set(), []
    for f in findings:
        k = _norm(f["claim"])
        if k not in seen:
            seen.add(k)
            unique.append(f)
    if len(unique) < len(findings):
        run.log("Orchestrator", "agent", f"De-duplicated findings: {len(findings)} -> {len(unique)}")
    findings = unique
    for n, f in enumerate(findings, 1):
        f["id"] = f"F{n}"
    by_id = {f["id"]: f for f in findings}
    run.log("Orchestrator", "agent", f"Research complete: {len(findings)} verified findings from {len(registry.items)} sources")
    if not findings:
        raise RuntimeError("No verifiable findings. Provide a document or enable web research.")

    # --- synthesis + critic loop ------------------------------------------
    feedback, brief, verdicts = "", {}, []
    for rnd in range(1, max_rounds + 2):
        brief = synthesize(run, llm, company, service_line, findings, feedback)
        run.log("Synthesizer", "llm", f"Draft {rnd}: {len(brief.get('key_facts', []))} facts, {len(brief.get('pain_points', []))} hypotheses")
        verdicts = critique(run, llm, brief, by_id, service_line)
        bad = [v for v in verdicts if not v["ok"]]
        run.log("Critic", "critic", f"Round {rnd}: {len(verdicts) - len(bad)}/{len(verdicts)} claims supported", bad)
        if not bad or rnd > max_rounds:
            break
        def _txt(v):
            x = (brief["key_facts"] if v["kind"] == "key_fact" else brief["pain_points"])[v["idx"]]
            return x.get("text") or x.get("hypothesis", "")

        feedback = "\n".join(f"- {_txt(v)}: {v['issue']}" for v in bad)
        run.log("Critic", "critic", "Sending rejected claims back to the synthesizer")
    # drop anything still unsupported
    bad_set = {(v["kind"], v["idx"]) for v in verdicts if not v["ok"]}
    brief["key_facts"] = [x for i, x in enumerate(brief.get("key_facts", [])) if ("key_fact", i) not in bad_set]
    brief["pain_points"] = [x for i, x in enumerate(brief.get("pain_points", [])) if ("pain_point", i) not in bad_set]
    if bad_set:
        run.log("Critic", "critic", f"Dropped {len(bad_set)} claims that never passed verification")

    # --- summary written only from verified items --------------------------
    brief["summary"] = write_summary(run, llm, company, brief)

    # --- proposal + human gate --------------------------------------------
    proposal = draft_proposal(run, llm, company, service_line, brief)
    run.log("Drafter", "llm", "Drafted pitch outline, scope, discovery questions and outreach email")
    decision = run.request_approval("Review the outreach email before it is finalized", {"email": proposal.get("email", {})}, auto=auto_approve)
    if decision.get("email"):
        proposal["email"] = decision["email"]
    proposal["email_status"] = "approved" if decision.get("approved") else "rejected (not finalized)"

    # --- assemble result ---------------------------------------------------
    cited = {s for x in brief["key_facts"] + brief["pain_points"] for f in x.get("finding_ids", []) if f in by_id for s in [by_id[f]["source_id"]]}
    for x in brief["key_facts"] + brief["pain_points"]:
        x["sources"] = sorted({by_id[f]["source_id"] for f in x.get("finding_ids", []) if f in by_id})
        x["evidence"] = [{"id": f, "claim": by_id[f]["claim"], "quote": by_id[f]["quote"], "source": by_id[f]["source_id"], "strength": by_id[f]["strength"]} for f in x.get("finding_ids", []) if f in by_id]
        x["evidence_level"] = "strong" if any(e["strength"] == "strong" for e in x["evidence"]) else "headline-only"
    total = len(verdicts)
    result = {
        "company": company,
        "service_line": service_line,
        "summary": brief.get("summary", ""),
        "key_facts": brief["key_facts"],
        "pain_points": brief["pain_points"],
        "proposal": proposal,
        "sources": [{k: v[k] for k in ("id", "title", "url", "kind")} | {"date": v.get("date", ""), "cited": k in cited} for k, v in registry.items.items()],
        "metrics": {
            "sources": len(registry.items),
            "findings_verified": len(findings),
            "claims_checked": total,
            "claims_supported": total - len(bad_set),
            "grounding_rate": round((total - len(bad_set)) / total, 2) if total else 0,
            "llm_calls": llm.stats["calls"],
            "cache_hits": llm.stats["cache_hits"],
            "llm_seconds": round(llm.stats["seconds"], 1),
        },
    }
    result["markdown"] = to_markdown(result)
    return result


def to_markdown(r: dict) -> str:
    import html as _h
    L = [f"# Client briefing: {r['company']}", "", r["summary"], "", "## Key facts"]
    L += [f"- {x.get('text')} [{', '.join(x['sources'])}] _({x.get('evidence_level', '')})_" for x in r["key_facts"]]
    L += ["", "## Pain-point hypotheses"]
    L += [f"- **{x.get('hypothesis')}**: {x.get('why')} _Fit: {x.get('service_fit')}_ [{', '.join(x['sources'])}] _({x.get('evidence_level', '')})_" for x in r["pain_points"]]
    p = r["proposal"]
    L += ["", "## Pitch outline"] + [f"- {b}" for b in p.get("pitch_outline", [])]
    L += ["", "## Proposed scope"] + [f"- {w.get('workstream')}: {w.get('deliverable')} ({w.get('weeks')} wks)" for w in p.get("proposed_scope", []) if isinstance(w, dict)]
    L += ["", "## Discovery questions"] + [f"- {q}" for q in p.get("discovery_questions", [])]
    e = p.get("email", {})
    L += ["", f"## Outreach email ({p.get('email_status')})", f"**Subject:** {e.get('subject', '')}", "", e.get("body", ""), "", "## Sources"]
    L += [f"- [{s['id']}] {s['title']} {s.get('date') or ''} {s['url']}" for s in r["sources"] if s["cited"]]
    return "\n".join(L)
