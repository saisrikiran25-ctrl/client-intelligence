"""Free research tools (no API keys): web search, page fetch, Wikipedia, Google News RSS.

Everything is best-effort and cached on disk, so a demo run can be replayed offline.
Tools return plain dicts: {title, url, text, kind}.
"""
from __future__ import annotations

import datetime
import email.utils
import hashlib
import html
import json
import pathlib
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_CACHE = ROOT / "runs" / "cache" / "web"
WEB_CACHE.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; ConsultingAgents/1.0; +demo)"


def _get(url: str, timeout: int = 12, ttl: int = 1800) -> str:
    """Fetch a URL. Responses are kept for `ttl` seconds only, so every demo run reads the live web."""
    key = hashlib.sha256(url.encode()).hexdigest()
    p = WEB_CACHE / f"{key}.txt"
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        return p.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(1_500_000)
        charset = r.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    p.write_text(text, encoding="utf-8")
    return text


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(raw: str, limit: int = 6000) -> str:
    p = _Text()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001
        pass
    return re.sub(r"\s+", " ", " ".join(p.parts))[:limit]


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ------------------------------------------------------------------------ tools
def web_search(query: str, n: int = 5) -> list[dict]:
    """DuckDuckGo HTML endpoint. Returns result titles + snippets (no page fetch)."""
    raw = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', raw, re.S):
        href, title, snip = m.groups()
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
        out.append({"title": _strip_tags(title), "url": href, "text": _strip_tags(snip), "kind": "web"})
        if len(out) >= n:
            break
    return out


def fetch_url(url: str) -> dict | None:
    try:
        raw = _get(url)
    except Exception:  # noqa: BLE001
        return None
    text = html_to_text(raw)
    return {"title": url, "url": url, "text": text, "kind": "page"} if len(text) > 200 else None


def wikipedia(query: str) -> list[dict]:
    try:
        s = json.loads(_get("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=1&srsearch=" + urllib.parse.quote(query)))
        hits = s["query"]["search"]
        if not hits:
            return []
        title = hits[0]["title"]
        d = json.loads(_get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))))
        return [{"title": f"Wikipedia: {title}", "url": d.get("content_urls", {}).get("desktop", {}).get("page", ""), "text": d.get("extract", ""), "kind": "wiki"}]
    except Exception:  # noqa: BLE001
        return []


def news_rss(query: str, n: int = 6, max_age_days: int = 548) -> list[dict]:
    """Google News RSS, India edition."""
    url = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q=" + urllib.parse.quote(query)
    try:
        root = ET.fromstring(_get(url))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in root.iter("item"):
        title = _strip_tags(item.findtext("title") or "")
        desc = _strip_tags(item.findtext("description") or "")
        pub = item.findtext("pubDate") or ""
        try:
            when = email.utils.parsedate_to_datetime(pub).date()
        except Exception:  # noqa: BLE001
            when = None
        if when and (datetime.date.today() - when).days > max_age_days:
            continue  # stale news is not evidence about the company today
        out.append({"title": title, "url": item.findtext("link") or "", "text": f"{title}. {desc} (published {when or pub})", "kind": "news", "date": str(when or "")})
        if len(out) >= n:
            break
    return out


def chunk_text(text: str, size: int = 1400) -> list[str]:
    """Split a document into paragraph-aligned chunks."""
    paras, chunks, cur = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()], [], ""
    for p in paras:
        if cur and len(cur) + len(p) > size:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n\n" + p).strip()
    if cur:
        chunks.append(cur)
    return chunks


# ------------------------------------------------ richer free sources (Wikipedia full text, Wikidata, official website)
def wikipedia_full(query: str) -> dict | None:
    """Full plain-text article of the best Wikipedia match."""
    try:
        d = json.loads(_get("https://en.wikipedia.org/w/api.php?action=query&format=json&prop=extracts&explaintext=1&exlimit=1&generator=search&gsrlimit=1&gsrsearch=" + urllib.parse.quote(query)))
        pages = d.get("query", {}).get("pages", {})
        if not pages:
            return None
        pg = sorted(pages.values(), key=lambda p: p.get("index", 0))[0]
        return {"title": "Wikipedia: " + pg["title"], "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(pg["title"].replace(" ", "_")), "text": pg.get("extract", ""), "kind": "wiki"}
    except Exception:  # noqa: BLE001
        return None


_WD_PROPS = {"P169": "Chief executive officer", "P159": "Headquarters", "P452": "Industry", "P571": "Founded", "P1128": "Employees", "P2139": "Revenue", "P2295": "Net income", "P856": "Official website"}


def wikidata_facts(query: str) -> dict | None:
    """Structured facts (CEO, HQ, industry, founding, employees, revenue, website) from Wikidata."""
    try:
        hit = json.loads(_get("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json&language=en&limit=1&search=" + urllib.parse.quote(query))).get("search", [])
        if not hit:
            return None
        qid = hit[0]["id"]
        ent = json.loads(_get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"))["entities"][qid]
        claims = ent.get("claims", {})

        def latest(lst):
            def when(c):
                q = c.get("qualifiers", {}).get("P585", [{}])[0].get("datavalue", {}).get("value", {}).get("time", "")
                return q
            return sorted(lst, key=when, reverse=True)

        raw: dict[str, list] = {}
        ids: set[str] = set()
        for prop in _WD_PROPS:
            for c in latest(claims.get(prop, []))[:2 if prop in ("P2139", "P1128", "P2295", "P452") else 1]:
                v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                if v is None:
                    continue
                when = c.get("qualifiers", {}).get("P585", [{}])[0].get("datavalue", {}).get("value", {}).get("time", "")[1:5]
                raw.setdefault(prop, []).append((v, when))
                if isinstance(v, dict) and "id" in v:
                    ids.add(v["id"])
                if isinstance(v, dict) and "unit" in v:
                    ids.add(v["unit"].rsplit("/", 1)[-1])
        labels = {}
        if ids:
            lab = json.loads(_get("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json&props=labels&languages=en&ids=" + "|".join(sorted(ids))))
            labels = {k: v.get("labels", {}).get("en", {}).get("value", k) for k, v in lab.get("entities", {}).items()}
        lines, website, industry = [], None, []
        for prop, vals in raw.items():
            for v, when in vals:
                if isinstance(v, str):
                    text = v
                    website = v if prop == "P856" else website
                elif "id" in v:
                    text = labels.get(v["id"], v["id"])
                    if prop == "P452":
                        industry.append(text)
                elif "time" in v:
                    text = v["time"][1:5]
                elif "amount" in v:
                    unit = labels.get(v.get("unit", "").rsplit("/", 1)[-1], "")
                    text = f"{float(v['amount']):,.0f} {unit}".strip()
                else:
                    continue
                lines.append(f"{_WD_PROPS[prop]}: {text}" + (f" (as of {when})" if when else ""))
        if not lines:
            return None
        name = ent.get("labels", {}).get("en", {}).get("value", query)
        return {"title": f"Wikidata: {name}", "url": f"https://www.wikidata.org/wiki/{qid}", "text": f"{name}. " + "\n".join(lines), "kind": "wikidata", "website": website, "industry": industry}
    except Exception:  # noqa: BLE001
        return None


def official_site(url: str | None, max_pages: int = 3) -> list[dict]:
    """Homepage plus up to two 'about / investors / newsroom' pages of the company's own website."""
    if not url:
        return []
    out = []
    try:
        home_raw = _get(url)
    except Exception:  # noqa: BLE001
        return []
    home = html_to_text(home_raw)
    if len(home) > 200:
        out.append({"title": f"Official website: {url}", "url": url, "text": home, "kind": "company-site"})
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    seen = {url}
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', home_raw):
        full = urllib.parse.urljoin(url, href)
        if full in seen or host not in urllib.parse.urlparse(full).netloc or not re.search(r"about|investor|annual|press|news|media|sustainab", full, re.I):
            continue
        seen.add(full)
        try:
            t = html_to_text(_get(full))
        except Exception:  # noqa: BLE001
            continue
        if len(t) > 300:
            out.append({"title": f"Official website: {full}", "url": full, "text": t, "kind": "company-site"})
        if len(out) >= max_pages:
            break
    return out
