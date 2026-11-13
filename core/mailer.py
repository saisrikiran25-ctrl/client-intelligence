"""Email sending for BOTH agents (Gmail or any SMTP server). Preview by default; a real send needs an explicit confirm.

One-time setup: create a Gmail App password (Google account > Security > App passwords), paste it into the "Connect Gmail" pop-up.
It is verified against Gmail, stored ENCRYPTED for the current Windows user (DPAPI) in runs/mail_settings.json, and never shown again.
(Google's OAuth login tokens for personal apps expire after 7 days, so an App password is what keeps "log in once" true.)

Safety rules (each one is tested):
  * Agent B: only drafts of documents a human actually reviewed in the exception queue are eligible
  * Agent A: only an outreach email the human APPROVED is eligible
  * a vendor email goes to the address in YOUR vendor master, never to an address read from a document
  * fraud alerts (bank-detail change) never go to the vendor
  * DEMO REDIRECT: while a redirect address is set, EVERY message goes only to that address, subject marked [DEMO],
    with the intended recipient written inside the message
  * every message is logged and a draft is never sent twice
  * header values are stripped of line breaks (no header injection); the app password is never logged or returned
Payments are deliberately NOT implemented: moving money needs bank/ERP credentials and controls that belong to the company.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import json
import os
import pathlib
import re
import smtplib
import ssl
import sys
import uuid
from email.message import EmailMessage
from email.utils import formatdate

from core.exports import out_dir

ROOT = pathlib.Path(__file__).resolve().parent.parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GMAIL_HOST, GMAIL_PORT = "smtp.gmail.com", 587


class MailerError(RuntimeError):
    pass


# ------------------------------------------------------------------ encrypted settings
def settings_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MAIL_SETTINGS_FILE") or ROOT / "runs" / "mail_settings.json")


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI: only the same Windows user on this PC can decrypt."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    inb, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    if not fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("Windows could not encrypt/decrypt the saved password")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text: str) -> dict:
    if sys.platform == "win32":
        return {"enc": "dpapi", "data": base64.b64encode(_dpapi(text.encode(), True)).decode()}
    return {"enc": "plain", "data": base64.b64encode(text.encode()).decode()}  # other OS: no OS keystore available here


def unprotect(blob: dict) -> str:
    raw = base64.b64decode(blob["data"])
    return (_dpapi(raw, False) if blob.get("enc") == "dpapi" else raw).decode()


def validate_settings(email: str, app_password: str, redirect_to: str) -> tuple[str, str, str]:
    email, redirect_to = (email or "").strip(), (redirect_to or "").strip()
    pw = re.sub(r"\s", "", app_password or "")
    if not EMAIL_RE.match(email):
        raise MailerError("Enter the full Gmail address, e.g. you@gmail.com")
    if not re.fullmatch(r"[A-Za-z]{16}", pw):
        raise MailerError("That is not an App password. Google App passwords are exactly 16 letters (shown as 4 groups of 4). Your normal Gmail password will not work.")
    if redirect_to and not EMAIL_RE.match(redirect_to):
        raise MailerError("The demo recipient is not a valid email address")
    return email, pw, redirect_to


def verify_login(email: str, password: str) -> tuple[bool, str]:
    """Log in to Gmail without sending anything. Returns (ok, human-readable error). The password never appears in the error."""
    try:
        s = smtplib.SMTP(GMAIL_HOST, GMAIL_PORT, timeout=25)
        try:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
            s.login(email, password)
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, "Gmail rejected that login. Check the address and the 16-letter App password (2-Step Verification must be on, and the code must be an App password, not your normal password)."
    except Exception as e:  # noqa: BLE001
        return False, f"Could not reach Gmail ({type(e).__name__}). Check your internet connection."


def save_settings(email: str, password: str, redirect_to: str) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"email": email, "password": protect(password), "redirect_to": redirect_to, "saved": dt.datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")


def load_settings() -> dict | None:
    p = settings_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"email": d["email"], "password": unprotect(d["password"]), "redirect_to": d.get("redirect_to", "")}
    except Exception:  # noqa: BLE001  (corrupt file, or saved by another Windows user)
        return None


def clear_settings() -> None:
    settings_path().unlink(missing_ok=True)


# ----------------------------------------------------------------------------- config
def config() -> dict | None:
    saved = load_settings()
    if saved:
        return {"source": "gmail", "host": GMAIL_HOST, "port": GMAIL_PORT, "user": saved["email"], "password": saved["password"], "from": saved["email"], "security": "starttls",
                "finance": os.environ.get("FINANCE_EMAIL", "").strip(), "legal": os.environ.get("LEGAL_EMAIL", "").strip() or os.environ.get("FINANCE_EMAIL", "").strip(),
                "redirect": saved["redirect_to"]}
    host, sender = os.environ.get("SMTP_HOST", "").strip(), os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        return None
    return {"source": "env", "host": host, "port": int(os.environ.get("SMTP_PORT", "587")), "user": os.environ.get("SMTP_USER", ""), "password": os.environ.get("SMTP_PASSWORD", ""),
            "from": sender, "security": os.environ.get("SMTP_SECURITY", "starttls").lower(), "finance": os.environ.get("FINANCE_EMAIL", "").strip(),
            "legal": os.environ.get("LEGAL_EMAIL", "").strip() or os.environ.get("FINANCE_EMAIL", "").strip(), "redirect": os.environ.get("REDIRECT_EMAIL", "").strip()}


def status() -> dict:
    c = config()
    return {"configured": bool(c), "source": c["source"] if c else None, "account": c["user"] if c else "", "redirect_to": c["redirect"] if c else "", "finance_email": bool(c and c["finance"])}


def _clean(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", str(s or "")).strip()


# ------------------------------------------------------------------------- planning
def _plan_b(result: dict, finance: str, legal: str) -> list[dict]:
    out = []
    for r in result["documents"]:
        d = r.get("draft")
        if not d:
            continue
        item = {"document_id": r["id"], "kind": d["kind"], "subject": _clean(d["subject"]), "body": d["body"], "to": "", "status": "skipped", "reason": "", "addr_reason": ""}
        if not (r.get("human") or {}).get("reviewed"):
            item["reason"] = "not reviewed by a human in the exception queue"
        elif d["kind"] == "internal_alert":
            item["to"], item["addr_reason"] = finance, ("" if finance else "FINANCE_EMAIL is not set")
        elif d["kind"] == "legal_note":
            item["to"], item["addr_reason"] = legal, ("" if legal else "LEGAL_EMAIL / FINANCE_EMAIL is not set")
        else:  # vendor_query: only the address on file in the vendor master
            to = (r.get("vendor_email") or "").strip()
            item["to"] = to
            if not to:
                item["addr_reason"] = "no email address for this vendor in the vendor master"
            elif not EMAIL_RE.match(to):
                item["to"], item["addr_reason"] = "", f"vendor master email '{to}' is not a valid address"
        out.append(item)
    return out


def _plan_a(result: dict) -> list[dict]:
    p = result.get("proposal") or {}
    e = p.get("email") or {}
    item = {"document_id": "outreach", "kind": "outreach", "subject": _clean(e.get("subject", "")), "body": e.get("body", ""), "to": "", "status": "skipped", "reason": "",
            "addr_reason": "the agent has no verified contact address for this company: set a demo recipient, or send the .eml file yourself"}
    if p.get("email_status") != "approved":
        item["reason"] = "the outreach email was not approved by a human"
    elif not e.get("body"):
        item["reason"] = "there is no email body"
    return [item]


def plan_sends(result: dict, finance: str = "", legal: str = "", redirect: str = "") -> list[dict]:
    """Decide, for every draft, whether and where it may go. Pure function: nothing is sent."""
    items = _plan_b(result, finance, legal) if "documents" in result else _plan_a(result)
    for it in items:
        if not it["reason"]:
            if redirect:  # DEMO REDIRECT: nothing leaves except to the one demo address, clearly marked
                it["intended"] = it["to"] or "(no address on file)"
                it["to"] = redirect
                it["subject"] = "[DEMO] " + it["subject"]
                it["body"] = f"[Demo mode: this message was redirected to you. Intended recipient: {it['intended']}]\n\n" + it["body"]
            elif it["addr_reason"]:
                it["reason"] = it["addr_reason"]
            if it["to"] and not EMAIL_RE.match(it["to"]):
                it["to"], it["reason"] = "", f"recipient '{it['to']}' is not a valid address"
        if not it["reason"] and it["to"]:
            it["status"] = "ready"
    return items


def _already_sent(run_id: str) -> set[str]:
    f = out_dir(run_id) / "sent_log.csv"
    if not f.exists():
        return set()
    with open(f, encoding="utf-8-sig") as fh:
        return {row["document_id"] + "|" + row["kind"] for row in csv.DictReader(fh) if row["status"] == "sent"}


def _connect(cfg: dict):
    ctx = ssl.create_default_context()
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
    if cfg["user"]:
        server.login(cfg["user"], cfg["password"])
    return server


def _message(cfg: dict, to: str, subject: str, body: str) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = cfg["from"], to, _clean(subject)
    m["Date"], m["Message-ID"] = formatdate(localtime=True), f"<{uuid.uuid4().hex}@agents.local>"
    m.set_content(body)
    return m


def send(run_id: str, result: dict, dry_run: bool = True) -> dict:
    cfg = config()
    plan = plan_sends(result, cfg["finance"] if cfg else "", cfg["legal"] if cfg else "", cfg["redirect"] if cfg else "")
    done = _already_sent(run_id)
    for p in plan:
        if p["status"] == "ready" and p["document_id"] + "|" + p["kind"] in done:
            p.update(status="skipped", reason="already sent earlier (see sent_log.csv)")
    ready = [p for p in plan if p["status"] == "ready"]
    if dry_run:
        for p in ready:
            p["status"] = "would send"
        return {"dry_run": True, "configured": bool(cfg), "redirect_to": cfg["redirect"] if cfg else "", "results": plan}
    if not cfg:
        raise MailerError("Email is not connected. Click 'Connect Gmail' and add an App password (or set SMTP_HOST / SMTP_FROM in .env).")
    log = out_dir(run_id) / "sent_log.csv"
    new = not log.exists()
    server = None
    try:
        if ready:
            server = _connect(cfg)
    except Exception as e:  # noqa: BLE001
        for p in ready:
            p.update(status="failed", reason=f"could not connect or log in: {type(e).__name__}")
        ready = []
    with open(log, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp", "document_id", "kind", "to", "subject", "status", "detail"])
        for p in plan:
            if p["status"] == "ready":
                try:
                    server.send_message(_message(cfg, p["to"], p["subject"], p["body"]))
                    p["status"] = "sent"
                except Exception as e:  # noqa: BLE001
                    p.update(status="failed", reason=f"{type(e).__name__}: {e}")
            if p["status"] in ("sent", "failed"):
                w.writerow([dt.datetime.now().isoformat(timespec="seconds"), p["document_id"], p["kind"], p["to"], p["subject"], p["status"], p["reason"]])
    if server:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    return {"dry_run": False, "configured": True, "redirect_to": cfg["redirect"], "results": plan}


def send_test() -> dict:
    """Send one short test message to the demo recipient (or, if none is set, to the connected account itself)."""
    cfg = config()
    if not cfg:
        raise MailerError("Email is not connected yet.")
    to = cfg["redirect"] or cfg["user"] or cfg["from"]
    try:
        server = _connect(cfg)
        server.send_message(_message(cfg, to, "[DEMO] Test from AI Agent Demos", "This is a test message from the AI Agent Demos app. If you can read this, email sending works."))
        server.quit()
    except Exception as e:  # noqa: BLE001
        raise MailerError(f"The test email could not be sent ({type(e).__name__}).") from e
    return {"to": to, "status": "sent"}
