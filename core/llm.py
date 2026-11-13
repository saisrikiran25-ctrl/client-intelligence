"""Pluggable LLM layer.

Backends
  claude  - headless Claude Code CLI (uses your Claude login, no API key)
  gemini  - Google Gemini via a free AI Studio key (set GEMINI_API_KEY; never commit it)
  ollama  - local model served by Ollama (free, offline, used for dev/tests)
  replay  - answers only from the on-disk cache (instant, deterministic demos)

Every response is cached on disk keyed by (backend, model, system, prompt), so a
demo that has been run once can be replayed instantly and costs no usage.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
def _load_dotenv() -> None:
    """Load KEY=value lines from <project>/.env into the environment (never overrides real env vars)."""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
CACHE = ROOT / "runs" / "cache" / "llm"
CACHE.mkdir(parents=True, exist_ok=True)

DEFAULT_MODELS = {"claude": "sonnet", "gemini": "gemini-3.5-flash", "ollama": "qwen2.5:3b-instruct", "replay": "replay"}


class LLMError(RuntimeError):
    pass

# Local models are RAM-bound: allow one Ollama request at a time.
_OLLAMA_LOCK = threading.Lock()
# Free Gemini tier is rate limited: keep concurrency low.
_GEMINI_SEM = threading.Semaphore(2)
_GEMINI_EXHAUSTED: set[tuple] = set()  # (key index, model) pairs used up this session
GEMINI_FALLBACKS = ["gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-flash-lite-latest"]


def extract_json(text: str):
    """Pull the first valid JSON object/array out of an LLM reply."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == open_c:
                    depth += 1
                elif ch == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except Exception:
                            break
            start = text.find(open_c, start + 1)
    raise LLMError("no valid JSON in model reply")


class LLM:
    def __init__(self, backend: str | None = None, model: str | None = None, cache: bool = True):
        self.backend = (backend or os.environ.get("AGENT_LLM") or "claude").lower()
        if self.backend not in DEFAULT_MODELS:
            raise LLMError(f"unknown backend {self.backend!r}")
        self.model = model or os.environ.get("AGENT_MODEL") or DEFAULT_MODELS[self.backend]
        self.cache = cache
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "cache_hits": 0, "seconds": 0.0}

    # ------------------------------------------------------------------ public
    def complete(self, prompt: str, system: str = "", json_mode: bool = False, attachments: list | None = None) -> str:
        att = "".join(hashlib.sha256(b).hexdigest() for _, b in (attachments or []))
        key = hashlib.sha256(f"{self.backend}|{self.model}|{system}|{prompt}|{att}".encode()).hexdigest()
        path = CACHE / f"{key}.txt"
        if self.cache and path.exists():
            with self._lock:
                self.stats["cache_hits"] += 1
            return path.read_text(encoding="utf-8")
        if self.backend == "replay":
            raise LLMError("replay mode: no cached answer for this prompt")
        t0 = time.time()
        if attachments and self.backend != "gemini":
            raise LLMError("reading images / scanned PDFs needs the gemini backend")
        if self.backend == "claude":
            out = self._claude(prompt, system)
        elif self.backend == "gemini":
            out = self._gemini(prompt, system, json_mode, attachments)
        else:
            out = self._ollama(prompt, system, json_mode)
        with self._lock:
            self.stats["calls"] += 1
            self.stats["seconds"] += time.time() - t0
        if self.cache and out.strip():
            path.write_text(out, encoding="utf-8")
        return out

    def json(self, prompt: str, system: str = "", tries: int = 3):
        """Ask for JSON; on a parse failure, retry with a repair instruction."""
        last = ""
        for attempt in range(tries):
            p = prompt if attempt == 0 else (
                prompt + "\n\nYour previous reply was not valid JSON. Reply with ONLY one valid JSON object, no prose."
            )
            last = self.complete(p, system, json_mode=True)
            try:
                return extract_json(last)
            except LLMError:
                continue
        raise LLMError(f"model never returned valid JSON. Last reply: {last[:200]!r}")

    # ---------------------------------------------------------------- backends
    def _claude(self, prompt: str, system: str) -> str:
        exe = shutil.which("claude") or "claude"
        cmd = [exe, "-p", "--output-format", "text", "--tools", "", "--no-session-persistence", "--model", self.model]
        if system:
            cmd += ["--system-prompt", system]
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=300)
        except subprocess.TimeoutExpired as e:
            raise LLMError("claude CLI timed out (300s)") from e
        except FileNotFoundError as e:
            raise LLMError("claude CLI not found on PATH") from e
        if r.returncode != 0 or not r.stdout.strip():
            raise LLMError(f"claude CLI failed: {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout.strip()

    @staticmethod
    def _gemini_keys() -> list[str]:
        """All configured keys: GEMINI_API_KEY, GEMINI_API_KEY_2.., or a comma-separated GEMINI_API_KEYS."""
        vals = [os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GOOGLE_API_KEY", "")]
        vals += [os.environ.get(f"GEMINI_API_KEY_{i}", "") for i in range(2, 10)]
        vals += os.environ.get("GEMINI_API_KEYS", "").split(",")
        return list(dict.fromkeys(v.strip() for v in vals if v.strip()))

    def _gemini(self, prompt: str, system: str, json_mode: bool, attachments: list | None = None) -> str:
        keys = self._gemini_keys()
        if not keys:
            raise LLMError("GEMINI_API_KEY is not set (create a free key in Google AI Studio and put it in the .env file)")
        parts = [{"text": prompt}] + [{"inlineData": {"mimeType": m, "data": base64.b64encode(d).decode()}} for m, d in (attachments or [])]
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.2}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        # Free-tier limits are per KEY and per MODEL. Best model first, on every key, before a weaker model. A per-minute limit,
        # a timeout or a 5xx moves to the next key/model immediately (sleeping on the same key only wastes time); we sleep
        # only between full sweeps, in case every combination is momentarily limited.
        chain = [self.model] + [m for m in GEMINI_FALLBACKS if m != self.model]
        timeout = 150 if attachments else 60
        last = None
        for sweep in range(3):
            for model in chain:
                for ki, key in enumerate(keys):
                    if (ki, model) in _GEMINI_EXHAUSTED or (ki, "*") in _GEMINI_EXHAUSTED:
                        continue
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key})
                    try:
                        with _GEMINI_SEM, urllib.request.urlopen(req, timeout=timeout) as resp:
                            data = json.loads(resp.read())
                        out_parts = data["candidates"][0]["content"]["parts"]
                        with self._lock:
                            if model != self.model:
                                self.stats.setdefault("fallback_models", set()).add(model)
                            if ki > 0:
                                self.stats.setdefault("keys_used", set()).add(ki + 1)
                        return "".join(p.get("text", "") for p in out_parts).strip()
                    except urllib.error.HTTPError as e:
                        detail = e.read()[:1500].decode("utf-8", "replace")
                        last = f"key{ki + 1}/{model}: HTTP {e.code}"
                        if e.code == 429 and ("PerDay" in detail or "per day" in detail):
                            _GEMINI_EXHAUSTED.add((ki, model))  # this key's daily quota for this model is gone
                        elif e.code in (400, 401, 403) and ("API key" in detail or "API_KEY" in detail or "PERMISSION" in detail):
                            _GEMINI_EXHAUSTED.add((ki, "*"))  # invalid / blocked key: stop using it entirely
                        elif e.code in (404, 400) and model != self.model:
                            _GEMINI_EXHAUSTED.add((ki, model))  # retired / unsupported fallback model
                        elif e.code in (429, 500, 503):
                            continue  # per-minute limit or transient: next key / model right away
                        else:
                            raise LLMError(f"gemini call failed: HTTP {e.code} {detail[:200]!r}") from e
                    except (KeyError, IndexError) as e:
                        raise LLMError("gemini returned no text (blocked or empty response)") from e
                    except Exception as e:  # noqa: BLE001  (timeouts, connection resets)
                        last = f"key{ki + 1}/{model}: {type(e).__name__}"
            if sweep < 2:
                time.sleep(10 * (sweep + 1))
        raise LLMError(f"all Gemini keys/models are rate-limited or unavailable ({last}). Free-tier quota is per model per day: wait for reset, add another key to .env, or use the replay/claude/ollama backend.")

    def _ollama(self, prompt: str, system: str, json_mode: bool) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 4096},
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}],
        }
        if json_mode:
            body["format"] = "json"
        req = urllib.request.Request(
            os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with _OLLAMA_LOCK, urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read())["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"ollama call failed: {e}") from e
