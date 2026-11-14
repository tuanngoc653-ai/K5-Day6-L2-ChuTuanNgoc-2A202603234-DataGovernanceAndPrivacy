"""T08 baseline (as specified in docs/tracks/T08.md): "delete one raw copy by filename, no derived/backup search".

For each request: delete every raw_video whose filename contains the subject id. No lineage, no backups,
no legal-hold / ACL / multi-subject checks. Deliberately naive — this is the bar the PoC must beat.

    python -m src.main --track t08 --pipeline src.baselines.t08_filename_baseline --output out/baseline --run-name baseline --offline
"""
from __future__ import annotations

import json

import pandas as pd

from ..common.context import RunContext
from ..common.decision import Decision, write_decision
from ..common.review import make_review_queue, write_review_queue
from ..metrics.tracks import get_track


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    if spec.track != "t08":
        raise NotImplementedError("t08 baseline only")
    graph = json.loads((ctx.input_dir / "data_graph.json").read_text(encoding="utf-8"))["artefacts"]   # 01 load
    requests = json.loads((ctx.input_dir / "requests.json").read_text(encoding="utf-8"))
    rows = []
    for req in sorted(requests, key=lambda r: r["request_id"]):                                           # 02 process
        for a in graph:
            if a["type"] == "raw_video" and req["subject_id"] in (a.get("filename") or ""):
                rows.append({"request_id": req["request_id"], "artefact_id": a["artefact_id"], "action": "delete",
                             "score": 0.0, "reason": "filename_match"})
    out = pd.DataFrame(rows, columns=["request_id", "artefact_id", "action", "score", "reason"])
    ctx.stats["n_items"] = len(out)
    out["key"] = out.request_id + "|" + out.artefact_id
    write_review_queue(make_review_queue(out, "key", "score", 0, reason_col="reason"), ctx.out("review_queue.csv"))  # 03/04: no review
    for r in out.itertuples():
        ctx.audit.log(actor="baseline", action="plan:delete", item_id=r.artefact_id, request_id=r.request_id)
    out[spec.pred_columns + ["reason"]].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(Decision("HOLD", "BASELINE_ONLY", "Filename baseline: no lineage/exception handling, never execute",   # 05
                            ctx.config.get("owner", "baseline"), ctx.track, [str(ctx.out(spec.pred_file))]),
                   ctx.out("decision.json"), ctx.out("decision_log.csv"))
