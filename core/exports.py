"""Real output files for each run: register CSVs, payment file, ready-to-send .eml drafts, briefing markdown.
Nothing is sent or paid: the files are what a human would take from here (upload the payment file, send the emails)."""
from __future__ import annotations

import csv
import datetime as dt
import pathlib
import re
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "runs" / "outputs"


def out_dir(run_id: str) -> pathlib.Path:
    d = OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _eml(to: str, subject: str, body: str, sender: str = "accounts.payable@yourcompany.example") -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = f'"{to}" <address-to-confirm@example.invalid>'
    m["Subject"] = subject
    m["X-Status"] = "DRAFT - prepared by the agent, NOT sent"
    m.set_content(body)
    return bytes(m)


def final_status(r: dict) -> str:
    h = r.get("human") or {}
    if r["decision"] == "approve":
        return "approved for payment (all checks passed)"
    if r["decision"] == "ignore":
        return "ignored (not an invoice or contract)"
    return {"approve": "approved by human reviewer", "reject": "rejected by human reviewer", "hold": "on hold"}.get(h.get("action"), "awaiting review")


def export_b(run_id: str, res: dict, db) -> list[str]:
    d = out_dir(run_id)
    names = []
    with open(d / "decisions.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["document_id", "file", "type", "vendor", "number", "date", "total_inr", "agent_decision", "final_status", "issues"])
        for r in res["documents"]:
            ex = r.get("extracted") or {}
            w.writerow([r["id"], r["name"], r["type"], r.get("vendor") or "", ex.get("doc_number") or "", ex.get("date") or ex.get("effective_date") or "", ex.get("total") or "",
                        r["decision"], final_status(r), "; ".join(f"{i['rule']}({i['severity']}): {i['msg']}" for i in r["issues"] if i["severity"] != "waived")])
    names.append("decisions.csv")

    rows = []
    for r in res["documents"]:
        ex = r.get("extracted") or {}
        cleared = r["decision"] == "approve" or (r.get("human") or {}).get("action") == "approve"
        if r["type"] == "invoice" and cleared and r.get("vendor_id") and ex.get("total"):
            v = db.q("SELECT * FROM vendors WHERE id=?", (r["vendor_id"],))[0]
            try:
                due = (dt.date.fromisoformat(ex["date"]) + dt.timedelta(days=int(ex.get("payment_terms_days") or 30))).isoformat()
            except Exception:  # noqa: BLE001
                due = ""
            # pay to the bank account ON FILE in the vendor master, never the one printed on the invoice
            rows.append([v["name"], v["gstin"], v["bank_account"], v["ifsc"], ex["doc_number"], ex["date"], due, f"{ex['total']:.2f}", r["id"]])
    with open(d / "payment_file.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["beneficiary", "gstin", "account_no_on_file", "ifsc", "invoice_no", "invoice_date", "due_date", "amount_inr", "document_id"])
        w.writerows(rows)
    names.append("payment_file.csv")

    for r in res["documents"]:
        if r.get("draft"):
            dr = r["draft"]
            n = f"outbox_{r['id']}_{dr['kind']}.eml"
            (d / n).write_bytes(_eml(dr["to"], dr["subject"], dr["body"]))
            names.append(n)
    s = res["stats"]
    lines = [f"# Accounts-payable run summary ({run_id})", "", f"- Documents: {s['documents']} ({s['invoices']} invoices, {s['contracts']} contracts, {s['ignored']} ignored, {s['skipped']} skipped)",
             f"- Auto-approved: {s['auto_approved']}, sent to review: {s['sent_to_review']}, rejected: {s['rejected']}", f"- Cleared for payment: Rs {s['amount_cleared']:,.2f}",
             f"- Held: Rs {s['amount_held']:,.2f}", f"- Fraud signals: {s['fraud_signals']}", "", "## Issues by rule"] + [f"- {k}: {v}" for k, v in sorted(s["issues_by_rule"].items(), key=lambda x: -x[1])]
    (d / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    names.append("summary.md")
    return names


def export_a(run_id: str, res: dict) -> list[str]:
    d = out_dir(run_id)
    (d / "client_briefing.md").write_text(res["markdown"], encoding="utf-8")
    names = ["client_briefing.md"]
    e = res["proposal"].get("email", {})
    if e.get("body") and res["proposal"].get("email_status") == "approved":
        (d / "outreach_email.eml").write_bytes(_eml(res["company"], e.get("subject", ""), e["body"], sender="you@yourfirm.example"))
        names.append("outreach_email.eml")
    return names


def safe_name(s: str) -> bool:
    return bool(re.fullmatch(r"[\w.\-]+", s))
