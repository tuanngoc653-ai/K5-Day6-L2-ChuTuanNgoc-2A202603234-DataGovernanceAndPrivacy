"""Build the group's INTERNAL self-check gold set for T08 (NOT the instructor GT).

Labelled independently of src/t08_engine.py (no shared code), from a plain reading of docs/tracks/T08.md:
  * artefact carries legal_hold                                   -> retain_legal_hold
  * our actor not in ACL / origin unknown / >1 subject on artefact -> escalate
  * descendant of requester's data that is not tagged with them    -> escalate
  * everything else tagged with the requester (raw, backup, frame, crop, embedding, annotation) -> delete
Assumption (documented in report): a legal hold covers only the held artefact, not its copies/derivatives.

Covers all 10 requests (a partial gold would count every other request's deletes as "outside scope").

    python -m tools.make_selfcheck_t08   ->  reports/selfcheck/gt_actions_selfcheck.csv
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

FIX = Path("fixtures/t08_deletion")
OUT = Path("reports/selfcheck/gt_actions_selfcheck.csv")
REQUESTS = None  # None = all requests (matches the scope of the official GT)


def main() -> int:
    arts = json.loads((FIX / "data_graph.json").read_text(encoding="utf-8"))["artefacts"]
    reqs = {r["request_id"]: r for r in json.loads((FIX / "requests.json").read_text(encoding="utf-8"))}
    by_id = {a["artefact_id"]: a for a in arts}
    rows = []
    for rid in REQUESTS or sorted(reqs):
        subj = reqs[rid]["subject_id"]
        mine = {a["artefact_id"] for a in arts if subj in a["subject_ids"]}
        # descendants (repeat until fixed point — deliberately different from the engine's BFS)
        scope = set(mine)
        changed = True
        while changed:
            changed = False
            for a in arts:
                if a["artefact_id"] not in scope and set(a["parent_ids"]) & scope:
                    scope.add(a["artefact_id"])
                    changed = True
        for aid in sorted(scope):
            a = by_id[aid]
            if a["legal_hold"]:
                act, why = "retain_legal_hold", f"legal hold {a['legal_hold']}"
            elif "data-platform" not in a["acl"]:
                act, why = "escalate", f"acl={a['acl']}"
            elif not a["origin_known"]:
                act, why = "escalate", "origin unknown"
            elif len(a["subject_ids"]) > 1:
                act, why = "escalate", f"shared by {len(a['subject_ids'])} subjects"
            elif aid not in mine:
                act, why = "escalate", f"derived but tagged {a['subject_ids']}"
            else:
                act, why = "delete", f"{a['type']} of {subj}"
            rows.append({"request_id": rid, "artefact_id": aid, "expected_action": act, "artefact_type": a["type"], "why": why})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
