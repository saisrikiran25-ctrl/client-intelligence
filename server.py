"""Zero-dependency web server for the Client Intelligence Agent demo UI.   python server.py  ->  http://localhost:8765"""
import base64
import json
import pathlib
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent_a.pipeline import run_agent as run_a  # noqa: E402
from core import exports, mailer  # noqa: E402
from core.docs import read_document  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.orchestrator import RUNS, Run  # noqa: E402

RUN_REGISTRY: dict[str, Run] = {}


def start_run(p: dict) -> Run:
    # live by default: every demo run calls the model for real. Reusing cached answers is an explicit opt-in.
    llm = LLM(p.get("backend") or None, p.get("model") or None, cache=bool(p.get("cache", False)))
    run = Run("a", p.get("company", "Company"), p)

    def work():
        try:
            res = run_a(run, llm, p["company"], p.get("service_line", ""), p.get("docs", ""), bool(p.get("use_web", True)), bool(p.get("auto_approve", False)))
            res["run_id"], res["outputs"] = run.id, exports.export_a(run.id, res)
            run.finish(res)
        except Exception as e:  # noqa: BLE001
            run.fail(e)

    RUN_REGISTRY[run.id] = run
    threading.Thread(target=work, daemon=True).start()
    return run


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        pages = {"/": "agent_a.html", "/client-intelligence": "agent_a.html"}
        if path in pages:
            return self._send(200, (ROOT / "ui" / pages[path]).read_bytes(), "text/html")
        if path in ("/ui/style.css", "/ui/common.js", "/ui/mail.js"):  # the only static assets
            return self._send(200, (ROOT / path.lstrip("/")).read_bytes(), "text/css" if path.endswith(".css") else "application/javascript")
        if path == "/api/samples":
            return self._send(200, [{"name": f.stem, "text": f.read_text(encoding="utf-8")} for f in sorted((ROOT / "samples").glob("*.txt"))])
        if path == "/api/mail_status":
            return self._send(200, mailer.status())
        if path == "/api/saved":
            out = []
            for f in sorted(RUNS.glob("*.json"), reverse=True):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    if d.get("status") == "done" and d.get("agent") == "a":
                        out.append({"id": d["id"], "agent": d["agent"], "title": d["title"], "events": len(d["events"])})
                except Exception:  # noqa: BLE001
                    pass
            return self._send(200, out)
        m = re.fullmatch(r"/api/saved/([\w-]+)", path)
        if m and (RUNS / f"{m[1]}.json").exists():
            return self._send(200, (RUNS / f"{m[1]}.json").read_bytes())
        m = re.fullmatch(r"/api/file/([\w-]+)/([\w.\-]+)", path)
        if m and (exports.OUT / m[1] / m[2]).is_file():
            return self._send(200, (exports.OUT / m[1] / m[2]).read_bytes(), "application/octet-stream", {"Content-Disposition": f'attachment; filename="{m[2]}"'})
        m = re.fullmatch(r"/api/runs/([\w-]+)", path)
        if m and m[1] in RUN_REGISTRY:
            since = int(dict(x.split("=") for x in qs.split("&") if "=" in x).get("since", 0))
            return self._send(200, RUN_REGISTRY[m[1]].snapshot(since))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/api/run":
                return self._send(200, {"id": start_run(self._body()).id})
            if self.path == "/api/send":
                b = self._body()
                run = RUN_REGISTRY.get(b.get("run_id", ""))
                if not run or run.status != "done" or run.agent != "a":
                    return self._send(400, {"error": "no finished run with that id (a recorded run cannot be emailed)"})
                dry = not (b.get("dry_run") is False and b.get("confirm") is True)  # a real send needs an explicit confirm
                try:
                    return self._send(200, mailer.send(run.id, run.result, dry_run=dry))
                except mailer.MailerError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/api/mail_settings":
                b = self._body()

                def note(outcome):  # attempt log for troubleshooting: outcome only, never the password
                    import datetime as _dt
                    with open(ROOT / "runs" / "mail_attempts.log", "a", encoding="utf-8") as fh:
                        fh.write(f"{_dt.datetime.now().isoformat(timespec='seconds')} {outcome}" + chr(10))
                try:
                    email, pw, redirect = mailer.validate_settings(b.get("email"), b.get("app_password"), b.get("redirect_to"))
                except mailer.MailerError as e:
                    note("rejected before contacting Gmail: " + str(e)[:60])
                    return self._send(400, {"error": str(e)})
                ok, err = mailer.verify_login(email, pw)  # logs in to Gmail, sends nothing
                if not ok:
                    note("Gmail refused the login: " + err[:60])
                    return self._send(400, {"error": err})
                mailer.save_settings(email, pw, redirect)
                note("saved OK")
                return self._send(200, {"ok": True, **mailer.status()})
            if self.path == "/api/mail_settings/clear":
                mailer.clear_settings()
                return self._send(200, {"ok": True, **mailer.status()})
            if self.path == "/api/mail_test":
                try:
                    return self._send(200, mailer.send_test())
                except mailer.MailerError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/api/extract":  # real PDF / text ingestion for uploaded files
                b = self._body()
                text, used_ocr = read_document(b.get("name", ""), base64.b64decode(b.get("b64", "")))
                return self._send(200, {"text": text, "ocr": used_ocr})
            m = re.fullmatch(r"/api/runs/([\w-]+)/approve", self.path)
            if m and m[1] in RUN_REGISTRY:
                RUN_REGISTRY[m[1]].resolve(self._body())
                return self._send(200, {"ok": True})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    port = 8765
    print(f"Client Intelligence Agent: http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
