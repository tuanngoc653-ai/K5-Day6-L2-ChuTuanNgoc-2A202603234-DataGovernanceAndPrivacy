"""T08 lineage + deletion policy engine (pure functions, no I/O side effects except reading inputs).

Flow per delete request:
  1. scope:   artefacts tagged with the subject (direct) + BFS over children (lineage-only)
  2. policy:  first matching rule in configs/policies/t08_deletion_policy.yaml wins
  3. output:  one row per (request_id, artefact_id) with action + rule_id + reason_code + lineage path
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pandas as pd
import yaml

ACTIONS = ("delete", "retain_legal_hold", "escalate")

# risk score used to order the human review queue (higher = review first)
RISK = {"R-HOLD-01": 1.0, "R-ID-01": 0.95, "R-ACL-01": 0.9, "R-ORIGIN-01": 0.8, "R-MULTI-01": 0.7,
        "R-LINEAGE-01": 0.6, "R-DEL-01": 0.1}
HOLD_LINEAGE_RISK = 0.5   # delete-candidates derived from a held artefact: worth a human look


def load_graph(path: Path) -> dict[str, dict]:
    g = json.loads(Path(path).read_text(encoding="utf-8"))
    arts = g["artefacts"] if isinstance(g, dict) else g
    ids = [a["artefact_id"] for a in arts]
    if len(ids) != len(set(ids)):
        raise ValueError("data_graph.json has duplicate artefact_id")
    return {a["artefact_id"]: a for a in arts}


def load_requests(path: Path) -> list[dict]:
    reqs = json.loads(Path(path).read_text(encoding="utf-8"))
    for r in reqs:
        for k in ("request_id", "subject_id"):
            if not r.get(k):
                raise ValueError(f"delete request missing {k}: {r}")
    return reqs


def load_policy(path: Path) -> dict:
    pol = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for r in pol["rules"]:
        if r["action"] not in ACTIONS:
            raise ValueError(f"policy rule {r['id']} has invalid action {r['action']}")
        for k in ("id", "when", "reason_code", "source"):
            if not r.get(k):
                raise ValueError(f"policy rule missing {k}: {r}")
    if pol["rules"][-1]["when"] != "always":
        raise ValueError("last policy rule must be the `always` fallback")
    return pol


def children_index(graph: dict[str, dict]) -> dict[str, list[str]]:
    ch: dict[str, list[str]] = {k: [] for k in graph}
    for a in graph.values():
        for p in a.get("parent_ids") or []:
            if p in ch:
                ch[p].append(a["artefact_id"])
    for v in ch.values():
        v.sort()
    return ch


def lineage_path(graph: dict[str, dict], aid: str) -> str:
    """Root -> ... -> aid following the first parent (enough for evidence display)."""
    path, cur, seen = [aid], aid, {aid}
    while graph[cur].get("parent_ids"):
        cur = graph[cur]["parent_ids"][0]
        if cur not in graph or cur in seen:
            break
        path.append(cur)
        seen.add(cur)
    return " > ".join(f"{graph[x]['type']}:{x}" for x in reversed(path))


def held_ancestor(graph: dict[str, dict], aid: str) -> str | None:
    """Closest ancestor (any parent path) that carries a legal hold."""
    q, seen = deque(graph[aid].get("parent_ids") or []), set()
    while q:
        p = q.popleft()
        if p in seen or p not in graph:
            continue
        seen.add(p)
        if graph[p].get("legal_hold"):
            return p
        q.extend(graph[p].get("parent_ids") or [])
    return None


def scope_for_subject(graph: dict[str, dict], children: dict[str, list[str]], subject: str) -> dict[str, str]:
    """{artefact_id: 'direct' | 'lineage'} — direct = tagged with subject; lineage = descendant not tagged."""
    direct = {k for k, a in graph.items() if subject in (a.get("subject_ids") or [])}
    scope = {k: "direct" for k in direct}
    q = deque(sorted(direct))
    while q:
        cur = q.popleft()
        for c in children.get(cur, []):
            if c not in scope:
                scope[c] = "lineage"
                q.append(c)
    return scope


def _matches(cond: str, a: dict, req: dict, scope_kind: str, actor: str) -> bool:
    if cond == "legal_hold_set":
        return bool(a.get("legal_hold"))
    if cond == "identity_not_verified":
        return not bool(req.get("identity_verified", False))
    if cond == "actor_not_in_acl":
        return actor not in (a.get("acl") or [])
    if cond == "origin_unknown":
        return not bool(a.get("origin_known", False))
    if cond == "multi_subject":
        return len(a.get("subject_ids") or []) > 1
    if cond == "lineage_subject_mismatch":
        return scope_kind == "lineage"
    if cond == "always":
        return True
    raise ValueError(f"unknown policy condition {cond!r}")


def classify(a: dict, req: dict, scope_kind: str, policy: dict) -> dict:
    actor = policy.get("pipeline_actor", "data-platform")
    for rule in policy["rules"]:
        if _matches(rule["when"], a, req, scope_kind, actor):
            return rule
    raise AssertionError("unreachable: policy has an `always` fallback")


def plan_actions(graph: dict[str, dict], requests: list[dict], policy: dict,
                 hold_propagates: bool = False) -> pd.DataFrame:
    """Dry-run plan: one row per (request, artefact) in scope. Deterministic (sorted)."""
    children = children_index(graph)
    rows = []
    for req in sorted(requests, key=lambda r: r["request_id"]):
        scope = scope_for_subject(graph, children, req["subject_id"])
        for aid in sorted(scope):
            a = graph[aid]
            rule = classify(a, req, scope[aid], policy)
            action, rule_id, code = rule["action"], rule["id"], rule["reason_code"]
            held_anc = held_ancestor(graph, aid)
            if hold_propagates and held_anc and action == "delete":
                action, rule_id, code = "retain_legal_hold", "R-HOLD-01b", "LEGAL_HOLD_INHERITED"
            score = RISK.get(rule_id, 1.0)
            if held_anc and action == "delete":
                score = max(score, HOLD_LINEAGE_RISK)
                code = f"{code};HELD_ANCESTOR:{held_anc}"
            rows.append({
                "request_id": req["request_id"], "artefact_id": aid, "action": action,
                "subject_id": req["subject_id"], "artefact_type": a["type"], "scope": scope[aid],
                "rule_id": rule_id, "reason_code": code, "legal_hold": a.get("legal_hold") or "",
                "subject_ids": ";".join(a.get("subject_ids") or []), "acl": ";".join(a.get("acl") or []),
                "origin_known": bool(a.get("origin_known", False)), "uri": a.get("uri", ""),
                "lineage": lineage_path(graph, aid), "deadline": req.get("deadline", ""),
                "score": score,
            })
    df = pd.DataFrame(rows)
    df.insert(0, "key", df.request_id + "|" + df.artefact_id)
    return df


def safety_violations(plan: pd.DataFrame, graph: dict[str, dict], actor: str) -> list[str]:
    """Invariants that must hold for any delete row (checked after human overrides too)."""
    bad = []
    for r in plan[plan.action == "delete"].itertuples():
        a = graph[r.artefact_id]
        if a.get("legal_hold"):
            bad.append(f"{r.key}: delete on legal hold {a['legal_hold']}")
        if actor not in (a.get("acl") or []):
            bad.append(f"{r.key}: delete without ACL permission")
        if r.subject_id not in (a.get("subject_ids") or []) and r.scope == "direct":
            bad.append(f"{r.key}: subject not on artefact")
    return bad
