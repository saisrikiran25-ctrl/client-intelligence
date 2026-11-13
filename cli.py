"""Command-line runner.  python cli.py a --company "Bharat Agro Foods" --doc samples/bharat_agro_foods.txt --llm ollama --no-web"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from agent_a.pipeline import run_agent as run_a  # noqa: E402
from core.llm import LLM  # noqa: E402
from core.orchestrator import Run  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("agent", choices=["a"])
    ap.add_argument("--company", required=True)
    ap.add_argument("--service", default="")
    ap.add_argument("--doc", help="path to a text file of company documents")
    ap.add_argument("--llm", default=None, help="claude | ollama | replay")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="bypass the model-answer cache: every call goes to the live model")
    a = ap.parse_args()

    docs = pathlib.Path(a.doc).read_text(encoding="utf-8") if a.doc else ""
    llm = LLM(a.llm, a.model, cache=not a.fresh)
    run = Run("a", a.company, {"company": a.company})
    last = 0

    def pump():
        nonlocal last
        for e in run.events[last:]:
            print(f"[{e['t']:6.1f}s] {e['actor']:<22} {e['kind']:<7} {e['msg']}")
        last = len(run.events)

    import threading

    def work():
        try:
            run.finish(run_a(run, llm, a.company, a.service, docs, not a.no_web, auto_approve=True))
        except Exception as exc:  # noqa: BLE001
            run.fail(exc)

    t = threading.Thread(target=work)
    t.start()
    while t.is_alive():
        t.join(0.5)
        pump()
    pump()
    if run.status == "done":
        print("\n" + run.result["markdown"] + "\n\nMETRICS:", run.result["metrics"])
    else:
        print("FAILED:", run.error)
    print(f"\nSaved: runs/{run.id}.json")


if __name__ == "__main__":
    main()
