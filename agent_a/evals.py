"""Eval harness for Agent A.   python -m agent_a.evals --llm claude

Each case runs the full pipeline on a provided document (no web, so results are reproducible) and is scored on:
  grounding   - share of brief claims that survived the verification loop
  recall      - required facts (from the document) that appear in the brief
  hallucinate - FORBIDDEN facts (not in the document) that wrongly appear (trap check)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent_a.pipeline import run_agent  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.orchestrator import Run  # noqa: E402

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "samples"

CASES = [
    {
        "name": "bharat_agro_operations",
        "company": "Bharat Agro Foods",
        "service": "operations and supply chain",
        "doc": "bharat_agro_foods.txt",
        # any one of the alternatives counts as a hit
        "required": [["1,840", "1840"], ["11.2", "margin"], ["74", "working capital"], ["Indore", "61 percent", "utilisation"]],
        # facts the document never states: a faithful brief must not assert them
        "forbidden": ["IPO", "export revenue of", "acquired", "Rs 5,000"],
    },
    {
        "name": "bharat_agro_digital",
        "company": "Bharat Agro Foods",
        "service": "digital transformation",
        "doc": "bharat_agro_foods.txt",
        "required": [["ERP", "legacy"], ["distributor", "1,900"], ["Chief Digital Officer", "CDO"], ["spreadsheet", "forecast"]],
        "forbidden": ["SAP S/4", "cloud migration completed", "IPO"],
    },
]


def score(case: dict, result: dict) -> dict:
    text = (result["markdown"]).lower()
    hits = sum(any(alt.lower() in text for alt in group) for group in case["required"])
    bad = [f for f in case["forbidden"] if f.lower() in text]
    return {
        "case": case["name"],
        "grounding_rate": result["metrics"]["grounding_rate"],
        "recall": round(hits / len(case["required"]), 2),
        "hallucinations": bad,
        "pass": result["metrics"]["grounding_rate"] >= 0.8 and hits / len(case["required"]) >= 0.5 and not bad,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default=None)
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    rows = []
    for c in CASES:
        llm = LLM(a.llm, a.model)
        run = Run("a", c["name"], {})
        try:
            res = run_agent(run, llm, c["company"], c["service"], (SAMPLES / c["doc"]).read_text(encoding="utf-8"), use_web=False, auto_approve=True)
            rows.append(score(c, res))
        except Exception as e:  # noqa: BLE001
            rows.append({"case": c["name"], "error": str(e), "pass": False})
        print(rows[-1], flush=True)
    passed = sum(r["pass"] for r in rows)
    print(f"\n{passed}/{len(rows)} cases passed")
    out = pathlib.Path(__file__).resolve().parent.parent / "runs" / "eval_a.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
