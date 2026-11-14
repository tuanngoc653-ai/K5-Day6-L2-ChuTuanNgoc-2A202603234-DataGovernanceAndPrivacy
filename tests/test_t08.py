"""T08 group tests: success path, idempotency, policy edge cases, failure modes, HITL loop."""
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src import main as runner
from src import t08_engine as eng

FIX = Path("fixtures/t08_deletion")
POLICY = Path("configs/policies/t08_deletion_policy.yaml")


def _run(out, *extra, config="configs/lab.yaml"):
    return runner.main(["--config", str(config), "--track", "t08", "--output", str(out), "--offline", "--seed", "42", *extra])


def _cfg(tmp_path, **params):
    cfg = yaml.safe_load(Path("configs/lab.yaml").read_text(encoding="utf-8"))
    cfg["params"].update(params)
    cfg["track_config"] = str(Path("configs/tracks/t08.yaml").resolve())
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def plan():
    return eng.plan_actions(eng.load_graph(FIX / "data_graph.json"), eng.load_requests(FIX / "requests.json"),
                            eng.load_policy(POLICY))


# ------------------------------------------------------------------ success path
def test_end_to_end_success(tmp_path):
    out = tmp_path / "poc"
    assert _run(out) == 0
    for f in ["actions.csv", "review_queue.csv", "audit_log.jsonl", "decision.json", "dry_run_plan.csv",
              "request_summary.csv", "deletion_receipts.csv", "run_info.json"]:
        assert (out / f).exists(), f
    acts = pd.read_csv(out / "actions.csv")
    assert set(acts.action) <= set(eng.ACTIONS)
    assert not acts.duplicated(["request_id", "artefact_id"]).any()
    assert set(acts.request_id) == {f"DR-{i:03d}" for i in range(1, 11)}
    dec = json.loads((out / "decision.json").read_text())
    assert dec["canonical"] == "HOLD" and dec["reason_code"] == "PENDING_HUMAN_APPROVAL"   # nobody reviewed yet
    receipts = pd.read_csv(out / "deletion_receipts.csv")
    assert set(receipts.status) == {"dry_run"}                                            # nothing executed
    logged = {json.loads(l)["item_id"] for l in (out / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()}
    assert set(acts.artefact_id) <= logged                                                # audit completeness = 1


def test_idempotent_two_runs_same_actions(tmp_path):
    assert _run(tmp_path / "a") == 0 and _run(tmp_path / "b") == 0
    assert (tmp_path / "a/actions.csv").read_text() == (tmp_path / "b/actions.csv").read_text()


# ------------------------------------------------------------------ policy edge cases (fixture traps)
def _row(plan, req, aid):
    r = plan[(plan.request_id == req) & (plan.artefact_id == aid)]
    assert len(r) == 1, (req, aid)
    return r.iloc[0]


def test_legal_hold_never_deleted(plan):
    held = plan[plan.legal_hold != ""]
    assert len(held) >= 2 and (held.action == "retain_legal_hold").all()
    assert _row(plan, "DR-002", "a0043").rule_id == "R-HOLD-01"


def test_backup_and_embeddings_in_scope(plan):
    r1 = plan[plan.request_id == "DR-001"]
    assert {"raw_video", "backup", "frame", "face_crop", "embedding", "annotation"} <= set(r1.artefact_type)


def test_exceptions_escalated(plan):
    assert _row(plan, "DR-008", "a0156").reason_code == "NO_DELETE_PERMISSION"          # vendor-x ACL backup
    assert _row(plan, "DR-004", "a0075").reason_code == "ORIGIN_UNKNOWN"
    assert _row(plan, "DR-001", "a0011").reason_code == "MULTI_SUBJECT_SHARED"          # frame with 2 people
    assert _row(plan, "DR-003", "a0211").action == "escalate"                           # training export, many subjects


def test_unverified_identity_escalates_everything():
    g = eng.load_graph(FIX / "data_graph.json")
    req = [{"request_id": "DR-X", "subject_id": "subj_01", "deadline": "2026-12-01", "identity_verified": False}]
    p = eng.plan_actions(g, req, eng.load_policy(POLICY))
    assert len(p) and (p.action != "delete").all()


# ------------------------------------------------------------------ failure modes
def test_tampered_input_fails_provenance(tmp_path):
    fx = tmp_path / "fx"
    shutil.copytree(FIX, fx)
    reqs = json.loads((fx / "requests.json").read_text())
    reqs[0]["subject_id"] = "subj_99"
    (fx / "requests.json").write_text(json.dumps(reqs))
    assert runner.main(["--track", "t08", "--input", str(fx), "--output", str(tmp_path / "o"), "--offline"]) == 2


def test_review_row_without_reviewer_fails(tmp_path):
    out = tmp_path / "poc"
    assert _run(out) == 0
    q = pd.read_csv(out / "review_queue.csv", dtype=str).fillna("")
    q.loc[0, "reviewer_decision"] = "approve"                      # decision but no reviewer name
    q.to_csv(out / "review_queue_reviewed.csv", index=False)
    assert _run(out) == 2


def test_human_cannot_delete_legal_hold(tmp_path):
    out = tmp_path / "poc"
    assert _run(out) == 0
    q = pd.read_csv(out / "review_queue.csv", dtype=str).fillna("")
    q["reviewer"], q["reviewer_decision"] = "tester", "approve"
    hold = q.item_id == "DR-002|a0043"
    q.loc[hold, ["reviewer_decision", "override_value"]] = ["edit", "delete"]
    q.to_csv(out / "review_queue_reviewed.csv", index=False)
    assert _run(out) == 0
    dec = json.loads((out / "decision.json").read_text())
    assert dec["canonical"] == "REWORK" and dec["reason_code"] == "SAFETY_INVARIANT_VIOLATED"


# ------------------------------------------------------------------ HITL -> GO -> idempotent mock delete
def test_reviewed_go_and_mock_delete_idempotent(tmp_path):
    cfg = _cfg(tmp_path, dry_run=False)
    out = tmp_path / "poc"
    assert _run(out, config=cfg) == 0
    q = pd.read_csv(out / "review_queue.csv", dtype=str).fillna("")
    q["reviewer"], q["reviewer_decision"], q["review_note"] = "tester", "approve", "confirm policy"
    q.to_csv(out / "review_queue_reviewed.csv", index=False)
    assert _run(out, config=cfg) == 0
    assert json.loads((out / "decision.json").read_text())["canonical"] == "GO"
    r1 = pd.read_csv(out / "deletion_receipts.csv")
    assert (r1.status == "deleted_mock").sum() > 0
    assert _run(out, config=cfg) == 0                                # resume / re-run
    r2 = pd.read_csv(out / "deletion_receipts.csv")
    deleted_first = set(r1.loc[r1.status == "deleted_mock", "artefact_id"])
    assert set(r2.loc[r2.status == "deleted_mock", "artefact_id"]) == set()
    assert deleted_first <= set(r2.loc[r2.status == "already_deleted", "artefact_id"])
