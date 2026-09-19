"""Eval harness: does the pipeline extract the right commitments, and only those?

A golden case (data/eval/<name>.yaml, gitignored because it holds customer
call content) names a transcript and the actions a careful human found in it:

  title: Anita <> the seller, 2 Sep
  transcript_file: anita-2026-09-02.txt         # relative to the case file
  call_date: "2026-09-02T08:30:00+00:00"
  deal: {name: Acme pilot, account: Acme Industries, domains: [acme.example]}
  participants: [{name: Anita Rao, email: anita.rao@acme.example}]
  expected_actions:
    - {owner: me, type: my_action, source: explicit_commitment, match: [deck], due: "2026-09-03"}
    - {owner: prospect, type: prospect_action, match: [cfo, meeting]}
  forbidden: [signed, purchase order]           # claims that must NOT appear as commitments

Each run uses its own throwaway sales.db, so evals never touch real memory,
and stops before the email step. Metrics:
  recall            expected actions matched by a loop with the same owner and every match word
  precision         matched loops / all commitment-type loops (my, prospect, mutual actions)
  source_accuracy   matched loops whose source equals the expected source (when one is given)
  evidence_failures commitments whose quote was not found in the cited turns
  garbled_leaks     loops citing a garbled turn at more than medium confidence (must be 0)
  forbidden_hits    loops whose description contains a forbidden phrase
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import config

COMMITMENT_TYPES = ("my_action", "prospect_action", "mutual")


def eval_dir() -> Path:
    path = config.DATA_DIR / "eval"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_case(name_or_path) -> tuple[dict, Path]:
    path = Path(name_or_path)
    if not path.exists():
        path = eval_dir() / f"{name_or_path}.yaml"
    return yaml.safe_load(path.read_text()), path


def _matches(loop, exp) -> bool:
    """Same owner, every `match` word, and at least one `match_any` word (descriptions vary)."""
    text = (loop["description"] or "").lower()
    if loop["owner"] != exp["owner"] or not all(w.lower() in text for w in exp.get("match", [])):
        return False
    any_of = exp.get("match_any") or []
    return not any_of or any(w.lower() in text for w in any_of)


def score(conn, call_id, case) -> dict:
    loops = [dict(r) for r in conn.execute("SELECT * FROM loops WHERE call_id=?", (call_id,))]
    commitments = [l for l in loops if l["type"] in COMMITMENT_TYPES]
    # A recommended item is the system's own idea, not something said on the call;
    # it only counts when the case expects a recommendation.
    agreed = [l for l in commitments if l["source"] != "recommended"]
    expected = case.get("expected_actions") or []
    matched_expected, matched_loops, source_ok = 0, set(), 0
    details = []
    for exp in expected:
        pool = commitments if exp.get("source") == "recommended" else agreed
        hit = next((l for l in pool if _matches(l, exp) and l["node_id"] not in matched_loops), None)
        if hit:
            matched_expected += 1
            matched_loops.add(hit["node_id"])
            if exp.get("source") and hit["source"] == exp["source"]:
                source_ok += 1
        details.append({"expected": exp, "matched": hit["description"] if hit else None,
                        "source": hit["source"] if hit else None, "due": hit["due_date"] if hit else None})
    turns = {r["idx"]: r["quality"] for r in conn.execute(
        "SELECT idx, quality FROM turns WHERE call_id=? AND tier='final'", (call_id,))}
    garbled_leaks = [l["description"] for l in loops
                     if any(turns.get(i) == "garbled" for i in json.loads(l["evidence_turns"] or "[]"))
                     and l["confidence"] in ("high", "explicit")]
    evidence_failures = [l["description"] for l in commitments
                         if l["source"] != "recommended" and l["confidence"] == "low"]
    forbidden = [l["description"] for l in commitments
                 if any(f.lower() in (l["description"] or "").lower() for f in case.get("forbidden") or [])]
    with_source = [e for e in expected if e.get("source")]
    return {
        "recall": round(matched_expected / len(expected), 3) if expected else None,
        "precision": round(len(matched_loops) / len(agreed), 3) if agreed else None,
        "source_accuracy": round(source_ok / len(with_source), 3) if with_source else None,
        "evidence_failures": evidence_failures,
        "garbled_leaks": garbled_leaks,
        "forbidden_hits": forbidden,
        "loops": [{k: l[k] for k in ("type", "owner", "source", "confidence", "due_date", "description")}
                  for l in loops],
        "details": details,
    }


def run_case(name_or_path) -> dict:
    case, path = load_case(name_or_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs = eval_dir() / "runs"
    runs.mkdir(exist_ok=True)
    db_file = runs / f"{path.stem}-{stamp}.db"
    previous = os.environ.get("SALES_DB")
    os.environ["SALES_DB"] = str(db_file)
    from .store import stores
    real_world = stores.WORLD_DB
    # The eval must not see the seller's real Jarvis commitments: on 2026-09-11 the
    # extractor found Jarvis's copies of this very call's promises and skipped
    # them, which scored as misses.
    stores.WORLD_DB = db_file.with_suffix(".no-world")
    try:
        from . import repo
        from .orchestrator import workflow
        from .sources import paste
        conn = stores.sales()
        deal_cfg = case.get("deal") or {}
        deal_id = None
        if deal_cfg:
            acct = repo.create_account(conn, deal_cfg.get("account") or deal_cfg["name"], deal_cfg.get("domains", []))
            deal_id = repo.create_deal(conn, deal_cfg["name"], account_id=acct)
        people = [repo.ensure_me(conn)]
        for p in case.get("participants") or []:
            pid = repo.create_person(conn, p["name"], email=p.get("email"))
            people.append(pid)
            if deal_id:
                repo.link_deal_person(conn, deal_id, pid)
        conn.commit()
        text = (path.parent / case["transcript_file"]).read_text()
        started = time.monotonic()
        call_id = paste.import_text(conn, text, case.get("title", path.stem), deal_id=deal_id,
                                    started_at=case.get("call_date"), lang_mode=case.get("lang_mode", "auto"),
                                    participants=people)
        workflow.run_pipeline(conn, call_id, until="loops_reconciled")
        result = score(conn, call_id, case)
        result.update({"case": path.stem, "call_id": call_id, "db": str(db_file),
                       "seconds": round(time.monotonic() - started, 1),
                       "agent_runs": [dict(r) for r in conn.execute(
                           "SELECT agent, provider, model, isolation, status, duration_ms, cost_usd "
                           "FROM agent_runs ORDER BY id")]})
    finally:
        stores.WORLD_DB = real_world
        if previous is None:
            os.environ.pop("SALES_DB", None)
        else:
            os.environ["SALES_DB"] = previous
    out = eval_dir() / "results" / f"{path.stem}-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    result["results_file"] = str(out)
    return result


def cases() -> list[str]:
    return sorted(p.stem for p in eval_dir().glob("*.yaml"))


def report(result) -> str:
    lines = [f"== {result['case']}  ({result['seconds']}s, {len(result['agent_runs'])} agent runs)",
             f"recall {result['recall']}  precision {result['precision']}  source_accuracy {result['source_accuracy']}",
             f"evidence_failures {len(result['evidence_failures'])}  garbled_leaks {len(result['garbled_leaks'])}  "
             f"forbidden_hits {len(result['forbidden_hits'])}"]
    for d in result["details"]:
        mark = "OK " if d["matched"] else "MISS"
        words = d["expected"].get("match", []) + ["|".join(d["expected"].get("match_any", []))]
        lines.append(f"  {mark} expected {d['expected']['owner']}: {' '.join(w for w in words if w)}"
                     f"  ->  {d['matched'] or '-'}  [{d['source'] or '-'}, due {d['due'] or '-'}]")
    lines.append("  extracted:")
    lines += [f"   - {l['owner']:<8} {l['type']:<15} {l['source']:<20} {l['confidence']:<8} {l['description']}"
              for l in result["loops"]]
    lines.append(f"  results: {result['results_file']}")
    return "\n".join(lines)
