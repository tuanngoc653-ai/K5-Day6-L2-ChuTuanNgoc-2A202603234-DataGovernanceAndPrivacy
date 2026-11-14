"""T08 · Retention & Delete Request Simulator — PoC pipeline (owner: Chu Tuan Ngoc).

Lab contract = 5 automated steps, run by `python -m src.main ...`:
    01 load      requests.json + data_graph.json + policy YAML, provenance check (manifest.csv sha256)
    02 process   lineage BFS (raw -> frame -> crop -> embedding / annotation / backup / export) + policy rules
    03 rank      review queue = every exception (hold / escalate) + deletes derived from held artefacts
    04 review    human approves / edits exceptions (out/<run>/review_queue_reviewed.csv) -> overrides + audit
    05 decide    GO / HOLD / REWORK + mock delete (only when GO and dry_run=false) with idempotent receipts

Never deletes real data: the "store" is a JSON file under the output folder.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.io import read_json, sha256_text, write_json
from .common.manifest import verify_manifest
from .common.review import load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track
from . import t08_engine as eng

DEFAULT_POLICY = Path("configs/policies/t08_deletion_policy.yaml")


def _require_t08(ctx: RunContext) -> None:
    if ctx.track != "t08":
        spec = get_track(ctx.track)
        raise NotImplementedError(f"this group's pipeline implements t08 only, not {spec.track} ({spec.title})")


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    _require_t08(ctx)
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    policy_path = Path(ctx.param("policy", DEFAULT_POLICY))
    data = {
        "manifest": pd.read_csv(manifest) if manifest.exists() else None,
        "graph": eng.load_graph(ctx.input_dir / "data_graph.json"),
        "requests": eng.load_requests(ctx.input_dir / "requests.json"),
        "policy": eng.load_policy(policy_path),
        "policy_sha256": sha256_text(policy_path.read_text(encoding="utf-8")),
    }
    ctx.stats.update(n_artefacts=len(data["graph"]), n_requests=len(data["requests"]),
                     policy_version=data["policy"]["version"], policy_sha256=data["policy_sha256"][:12])
    ctx.audit.log(actor="pipeline", action="load", item_id=str(ctx.input_dir),
                  after={"n_artefacts": len(data["graph"]), "n_requests": len(data["requests"]),
                         "policy": data["policy"]["version"], "policy_sha256": data["policy_sha256"]})
    return data


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    plan = eng.plan_actions(data["graph"], data["requests"], data["policy"],
                            hold_propagates=bool(ctx.param("hold_propagates_to_descendants", False)))
    plan.to_csv(ctx.out("dry_run_plan.csv"), index=False)        # what WOULD happen, before any human input
    ctx.stats["plan_counts"] = plan.action.value_counts().to_dict()
    return plan


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Queue every row that is not a plain delete, plus deletes downstream of a legal hold."""
    need = scored[(scored.action != "delete") | (scored.score >= eng.HOLD_LINEAGE_RISK)].copy()
    budget = int(ctx.param("budget", 80))
    if len(need) > budget:
        ctx.audit.log(actor="pipeline", action="review_budget_exceeded", item_id="review_queue",
                      after={"needed": len(need), "budget": budget})
    need["reason_txt"] = need.rule_id + " " + need.reason_code + " | " + need.lineage
    need["slice"] = need.request_id + ":" + need.artefact_type
    q = make_review_queue(need, id_col="key", score_col="score", budget=budget, reason_col="reason_txt",
                          action_col="action", slice_col="slice")
    write_review_queue(q, ctx.out("review_queue.csv"))
    try:
        from .common.contact_sheet import render_contact_sheet
        render_contact_sheet(q, ctx.out("contact_sheet.html"), image_col=None,
                             title="T08 delete-request exceptions — approve / edit (override_value = delete | retain_legal_hold | escalate)")
    except Exception as e:   # HTML is a convenience; CSV is the contract
        ctx.audit.log(actor="pipeline", action="contact_sheet_failed", item_id="contact_sheet.html", reason=str(e))
    ctx.stats["n_queue"] = len(q)
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    """Apply human decisions. approve = keep proposed action; edit/reject + override_value = set action;
    reject without value / escalate = escalate; skip = no change (stays pending)."""
    final = scored.copy()
    final["decided_by"] = "policy"
    final["reviewer"] = ""
    final["review_note"] = ""
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    reviewed_keys: set[str] = set()
    if reviewed_path.exists():
        rv = load_review_decisions(reviewed_path)
        ctx.stats["n_reviewed"] = len(rv)
        for r in rv.itertuples(index=False):
            m = final.key == r.item_id
            if not m.any():
                raise ValueError(f"review row {r.item_id} not in current plan (stale review file?)")
            before = final.loc[m, "action"].iloc[0]
            d, ov = r.reviewer_decision, str(r.override_value).strip().lower()
            if d == "skip":
                continue
            if d == "approve":
                after = before
            elif d in ("edit", "reject") and ov:
                if ov not in eng.ACTIONS:
                    raise ValueError(f"override_value {ov!r} for {r.item_id} must be one of {eng.ACTIONS}")
                after = ov
            else:                                   # reject w/o value, escalate
                after = "escalate"
            final.loc[m, ["action", "decided_by", "reviewer", "review_note"]] = [after, "human", r.reviewer, r.review_note]
            reviewed_keys.add(r.item_id)
            aid = final.loc[m, "artefact_id"].iloc[0]
            ctx.audit.log(actor=r.reviewer, action=f"review:{d}", item_id=aid, before=before, after=after,
                          reason=r.review_note, request_id=r.item_id.split("|")[0])
    pending = set(queue.item_id) - reviewed_keys
    ctx.stats["n_pending_review"] = len(pending)
    final["review_status"] = final.key.map(lambda k: "reviewed" if k in reviewed_keys
                                           else ("pending" if k in pending else "auto"))
    pd.DataFrame({"key": final.key, "before": scored.action, "after": final.action}).query("before != after") \
        .to_csv(ctx.out("dry_run_diff.csv"), index=False)
    # one audit entry per final action (item_id = artefact_id, as required by the evaluator)
    for r in final.itertuples(index=False):
        ctx.audit.log(actor=r.reviewer or "pipeline", action=f"plan:{r.action}", item_id=r.artefact_id,
                      reason=r.reason_code, request_id=r.request_id, rule_id=r.rule_id, decided_by=r.decided_by,
                      review_status=r.review_status)
    return final


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame, graph: dict | None = None, actor: str = "data-platform") -> Decision:
    owner = ctx.config.get("owner", "TODO-owner")
    counts = final.action.value_counts().to_dict()
    metrics = {"counts": counts, "n_rows": len(final), "n_pending_review": int(ctx.stats.get("n_pending_review", 0))}
    evidence = [str(ctx.out(p)) for p in ("actions.csv", "dry_run_plan.csv", "review_queue.csv", "audit_log.jsonl")]
    bad = eng.safety_violations(final, graph, actor) if graph else []
    if bad:
        return Decision("REWORK", "SAFETY_INVARIANT_VIOLATED", f"{len(bad)} delete rows break hard invariants: {bad[:3]}",
                        owner, ctx.track, evidence, metrics | {"violations": bad})
    if metrics["n_pending_review"]:
        return Decision("HOLD", "PENDING_HUMAN_APPROVAL",
                        f"{metrics['n_pending_review']} exception rows await reviewer sign-off; no deletion executed",
                        owner, ctx.track, evidence, metrics)
    return Decision("GO", "EXCEPTIONS_APPROVED",
                    f"all exceptions reviewed; {counts.get('delete', 0)} deletes cleared for (mock) execution",
                    owner, ctx.track, evidence, metrics)


def execute_mock_delete(ctx: RunContext, final: pd.DataFrame, decision: Decision) -> pd.DataFrame:
    """Idempotent mock deletion. Store state = out/mock_store_state.json (set of tombstoned artefact ids)."""
    dry = bool(ctx.param("dry_run", True))
    store_p = ctx.out("mock_store_state.json")
    store = read_json(store_p) if store_p.exists() else {"deleted": {}}
    rows = []
    for r in final[final.action == "delete"].itertuples(index=False):
        if dry or decision.canonical != "GO":
            status = "dry_run" if dry else "blocked_by_decision"
        elif r.artefact_id in store["deleted"]:
            status = "already_deleted"                         # second run = no-op (idempotent)
        else:
            store["deleted"][r.artefact_id] = {"request_id": r.request_id, "uri": r.uri}
            status = "deleted_mock"
            ctx.audit.log(actor="pipeline", action="mock_delete", item_id=r.artefact_id, before=r.uri,
                          after="tombstone", request_id=r.request_id)
        rows.append({"request_id": r.request_id, "artefact_id": r.artefact_id, "uri": r.uri, "status": status,
                     "tombstone": sha256_text(f"{r.request_id}|{r.artefact_id}|{r.uri}")[:16]})
    if not dry and decision.canonical == "GO":
        write_json(store_p, store)
    rec = pd.DataFrame(rows, columns=["request_id", "artefact_id", "uri", "status", "tombstone"])
    rec.to_csv(ctx.out("deletion_receipts.csv"), index=False)
    return rec


def request_summary(ctx: RunContext, final: pd.DataFrame, requests: list[dict]) -> pd.DataFrame:
    as_of = pd.Timestamp(ctx.param("as_of", "2026-11-14T09:00:00+07:00"))
    rows = []
    for req in requests:
        f = final[final.request_id == req["request_id"]]
        dl = pd.Timestamp(req["deadline"])
        rows.append({"request_id": req["request_id"], "subject_id": req["subject_id"], "n_artefacts": len(f),
                     "delete": int((f.action == "delete").sum()), "retain_legal_hold": int((f.action == "retain_legal_hold").sum()),
                     "escalate": int((f.action == "escalate").sum()), "pending_review": int((f.review_status == "pending").sum()),
                     "deadline": req["deadline"], "days_to_deadline": round((dl - as_of).total_seconds() / 86400, 1),
                     "sla_breach": bool(dl < as_of)})
    s = pd.DataFrame(rows)
    s.to_csv(ctx.out("request_summary.csv"), index=False)
    return s


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    cols = spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]
    final[cols].to_csv(ctx.out(spec.pred_file), index=False)
    request_summary(ctx, final, data["requests"])
    decision = decide(ctx, final, data["graph"], data["policy"].get("pipeline_actor", "data-platform"))
    rec = execute_mock_delete(ctx, final, decision)
    ctx.stats["receipts"] = rec.status.value_counts().to_dict()
    write_decision(decision, ctx.out("decision.json"), ctx.out("decision_log.csv"))
