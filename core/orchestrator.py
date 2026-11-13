"""Tiny agent runtime: run object (trace + state + approval gate) and parallel/retry helpers."""
from __future__ import annotations

import json
import pathlib
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
RUNS.mkdir(exist_ok=True)


class Run:
    """One execution of an agent. Holds the trace, status, result and the human-approval gate."""

    def __init__(self, agent: str, title: str, params: dict):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.agent, self.title, self.params = agent, title, params
        self.events: list[dict] = []
        self.status = "running"  # running | awaiting_approval | done | error
        self.result = None
        self.error = None
        self.pending_gate = None
        self._t0 = time.time()
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._decision = None

    # ----------------------------------------------------------------- tracing
    def log(self, actor: str, kind: str, msg: str, data=None):
        with self._lock:
            self.events.append(
                {"i": len(self.events), "t": round(time.time() - self._t0, 2), "actor": actor, "kind": kind, "msg": msg, "data": data}
            )

    # --------------------------------------------------------------- the gate
    def request_approval(self, label: str, payload: dict, auto: bool = False, timeout: int = 3600) -> dict:
        self.log("Gate", "gate", f"Waiting for human approval: {label}", payload)
        if auto:
            self.log("Gate", "gate", "Auto-approved (auto_approve is on)")
            return {"approved": True}
        self.pending_gate = {"label": label, "payload": payload}
        self.status = "awaiting_approval"
        self._gate.clear()
        got = self._gate.wait(timeout)
        decision = (self._decision if got else None) or {"approved": False, "reason": "timed out"}
        self.pending_gate = None
        self.status = "running"
        self.log("Gate", "gate", "Approved by human" if decision.get("approved") else "Rejected by human", decision)
        return decision

    def resolve(self, decision: dict):
        self._decision = decision
        self._gate.set()

    # ---------------------------------------------------------------- lifecycle
    def finish(self, result):
        self.result = result
        self.status = "done"
        self.log("Orchestrator", "done", "Run complete")
        self.save()

    def fail(self, exc: BaseException):
        self.error = f"{type(exc).__name__}: {exc}"
        self.status = "error"
        self.log("Orchestrator", "error", self.error, traceback.format_exc())
        self.save()

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "agent": self.agent,
                "title": self.title,
                "status": self.status,
                "events": self.events[since:],
                "next": len(self.events),
                "gate": self.pending_gate,
                "result": self.result if self.status in ("done",) else None,
                "error": self.error,
            }

    def save(self):
        data = {
            "id": self.id, "agent": self.agent, "title": self.title, "params": {k: v for k, v in self.params.items() if k != "docs"},
            "status": self.status, "events": self.events, "result": self.result, "error": self.error,
        }
        (RUNS / f"{self.id}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parallel(run: Run, jobs: dict, workers: int = 5) -> dict:
    """Run named callables concurrently. Failures are logged and returned as exceptions, not raised."""
    out: dict = {}

    def wrap(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            run.log(name, "error", f"{type(e).__name__}: {e}")
            return e

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {name: pool.submit(wrap, name, fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            out[name] = fut.result()
    return out


def retry(fn, tries: int = 2, run: Run | None = None, actor: str = "Orchestrator"):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if run:
                run.log(actor, "retry", f"attempt {i + 1}/{tries} failed: {e}")
    raise last  # type: ignore[misc]
