# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Mọi con số dưới đây lấy từ file do script sinh ra (`out/*/run_info.json`, `reports/selfcheck/*.json|md`).
> **Số chính thức** (`reports/metrics_*.json`) chỉ có khi giảng viên chạy `src.evaluate` với GT ẩn — repo học viên không có `gt/`.

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID | SOLO-2A202603234 |
| Challenge | T08 · Retention & Delete Request Simulator |
| Thành viên + vai trò | 1. Chu Tuan Ngoc — làm đơn, đảm nhận mọi vai trò (integrator, core algorithm, QA/metric, review, report) |
| Git URL | https://github.com/tuanngoc653-ai/K5-Day6-L2-ChuTuanNgoc-2A202603234-DataGovernanceAndPrivacy |
| Tag `demo-180m` (commit) | `demo-180m` → commit chứa report này (sau cf5a905, 844e01d) |
| Tag `final-24h` (commit) | ____ |
| Deadline (giờ VN) | ____ |

## 01 · Problem statement

- **Ai bị ảnh hưởng:** chủ thể dữ liệu (người lái/người đi đường trong video) — nếu sót bản sao/embedding thì quyền xóa bị vi phạm và embedding khuôn mặt vẫn định danh được họ; công ty — nếu xóa nhầm dữ liệu đang **legal hold** thì là hủy chứng cứ (không hoàn tác được); chủ thể khác — nếu xóa frame/export dùng chung thì mất dữ liệu của người khác.
- **Điểm nghẽn:** dữ liệu của 1 người nằm rải rác trong lineage raw → backup → frame → face_crop → embedding → annotation → training_export; xóa theo filename chỉ chạm raw.
- **Đầu vào → đầu ra:** `requests.json` (10 request) + `data_graph.json` (212 artefact) → `actions.csv` (1 dòng / cặp request×artefact, action ∈ delete / retain_legal_hold / escalate, kèm rule_id, reason_code, lineage) + review queue + audit log + decision + receipts.
- **GO / HOLD / REWORK (chốt trước eval):**
  - **REWORK** `SAFETY_INVARIANT_VIOLATED` — có dòng `delete` chạm legal hold hoặc thiếu ACL (kể cả do người review sửa).
  - **HOLD** `PENDING_HUMAN_APPROVAL` — còn dòng exception trong queue chưa có reviewer ký.
  - **GO** `EXCEPTIONS_APPROVED` — mọi exception đã được duyệt; chỉ khi đó (và `dry_run: false`) mock delete mới chạy.
- **Giả định cần DPO/legal xác nhận:** (1) legal hold chỉ áp cho artefact mang cờ hold, **không** tự lan xuống backup/frame/crop dẫn xuất (xem ablation §06); (2) artefact nhiều chủ thể không xóa mà escalate (redact/rebuild); (3) ACL không chứa `data-platform` → job không có quyền, phải chuyển cho owner/processor.

## 02 · Data & provenance

| Split | N | Nguồn | License | Synthetic? |
|---|---|---|---|---|
| eval (toàn bộ) | 10 request × 212 artefact → 164 cặp trong scope | `fixtures/t08_deletion` (tools/make_fixtures.py seed 42 của giảng viên) | CC0-1.0 | có |
| self-check nội bộ | 164 cặp | `tools/make_selfcheck_t08.py` → `reports/selfcheck/gt_actions_selfcheck.csv` | CC0 | có (tự gán) |

- Schema artefact: `type, subject_ids, parent_ids, uri, owner, acl, legal_hold, origin_known, created_at[, filename]`. Phân bố: 15 raw_video, 15 backup, 45 frame / face_crop / embedding / annotation, 2 training_export.
- Bẫy tìm được trong fixture: 2 legal hold (`a0043` LH-2026-03 subj_03, `a0099` LH-2026-07 subj_07); 3 embedding `origin_known=false` (`a0075/a0079/a0083`, subj_05); 1 backup ACL `vendor-x` (`a0156`, subj_11); 4 frame + 4 annotation 2 chủ thể; 2 training_export 9–10 chủ thể; face_crop của người khác cắt từ frame dùng chung (`a0012`, `a0068`, `a0124`).
- Seed 42 (pipeline deterministic, không có random). Không chia split — toàn bộ 10 request là eval; không tune trên GT.
- `manifest.csv` verified sha256: **có** — `python -m tools.verify_manifests` → `14 manifests, 0 failing`.
  - Bug tìm thấy: trên Windows `core.autocrlf=true` đổi LF→CRLF ⇒ sha256 lệch ⇒ pipeline dừng exit 2 (đúng thiết kế). Fix: thêm `.gitattributes` `fixtures/** -text`.
- Không dùng dữ liệu thật (đề cấm xóa dữ liệu thực).

## 03 · Research & design

- **Kỹ thuật:** BFS trên đồ thị lineage (cha→con) từ mọi artefact gắn `subject_id` + **policy engine YAML có version** (`configs/policies/t08_deletion_policy.yaml`, `t08-policy-1.0`, sha256 ghi vào audit). Mỗi rule có `id`, `when`, `action`, `reason_code`, `source` (điều luật), `needs_legal_review`. First-match-wins, precedence: **legal hold > identity > ACL > origin > multi-subject > lineage-mismatch > delete**.
- Lý do: GDPR Art.17(3)(e) và Luật BVDLCN 91/2025 có ngoại lệ quyền xóa khi có nghĩa vụ pháp lý ⇒ hold phải thắng mọi thứ; quyền xóa chỉ áp cho dữ liệu **của người yêu cầu** ⇒ dữ liệu chung phải escalate; nếu không chứng minh được nguồn gốc / không có quyền thì không tự động xóa.
- **Data flow:** `load` (sha256 manifest + policy) → `process` (BFS + rules → `dry_run_plan.csv`) → `rank` (queue mọi exception + delete có tổ tiên bị hold → `review_queue.csv`, `contact_sheet.html`) → `review` (đọc `review_queue_reviewed.csv`, override, audit) → `decide` (invariant check → GO/HOLD/REWORK) → mock delete idempotent (`mock_store_state.json`, `deletion_receipts.csv`) + `request_summary.csv` (SLA deadline).
- Không có model/weights. Thuần Python + pandas + PyYAML, offline.
- **Trade-offs:** escalate nhiều (an toàn, không false delete) ⇒ tốn công người review (62 dòng / 164); chọn hold không lan ⇒ completeness cao hơn nhưng có rủi ro nếu GT coi bản sao của dữ liệu bị hold cũng bị hold — đã đưa 26 dòng này vào queue để người quyết.

## 04 · Baseline vs PoC

- **Baseline** (`src/baselines/t08_filename_baseline.py`): đúng mô tả đề — mỗi request xóa raw_video có filename chứa subject_id; không lineage, không backup, không kiểm hold/ACL.
- **PoC** (`src/pipeline.py` + `src/t08_engine.py`): như §03.
- Cùng pool (10 request, 212 artefact), cùng seed 42, cùng evaluator `eval_t08`, `--offline`.

**Bảng 1 · Baseline vs PoC (SELF-CHECK gold, không phải GT giảng viên)** — output `python -m src.compare reports/selfcheck/metrics_baseline_selfcheck.json reports/selfcheck/metrics_poc_selfcheck.json`:

| Metric (unit) | Slice | Baseline | PoC | Δ abs | Δ rel | Better | N |
|---|---|---|---|---|---|---|---|
| **Deletion Completeness** (must-delete artefacts deleted / must-delete artefacts) | all | 0.0625 | 1.0000 | 0.9375 | +1500.0% | higher | 164 |
| False-delete count (exceptions deleted + deletes outside request scope) | all | 2.0000 | 0.0000 | -2.0000 | -100.0% | lower | |
| Exception handling accuracy (legal-hold/escalate rows handled as expected / such rows) | all | 0.0000 | 1.0000 | 1.0000 | baseline=0 | higher | |
| Audit trace completeness (actions with an audit entry / actions) | all | 1.0000 | 1.0000 | 0.0000 | +0.0% | higher | |
| Runtime / 100 items (s) | all | 0.3730 | 0.1165 | | | lower | |

Đọc trung thực:
- Baseline = 8/128 must-delete (chỉ raw), **2 false delete** = đúng 2 raw đang legal hold (`a0043`, `a0099`), exception accuracy = 0/36.
- PoC = 1.0 trên self-check **không chứng minh policy đúng**: gold self-check do chính người làm viết theo cùng cách đọc đề (dù code tách riêng). Nó chỉ chứng minh (a) engine cài đúng policy đã khai báo, (b) wiring/evaluator chạy, (c) khoảng cách với baseline. Số thật phụ thuộc GT ẩn — đặc biệt là giả định hold-lineage (§06).
- Runtime/100 items của baseline lớn hơn vì mẫu số chỉ 10 dòng (overhead cố định).

## 05 · Evaluation protocol

- Xem `reports/metric_contract.md`. Primary = Deletion Completeness, đơn vị = cặp (request_id, artefact_id), join exact; guardrail False-delete = 0.
- Không có threshold số; policy YAML + budget B=80 chốt 2026-11-14 trước mọi eval.
- N nhỏ: báo k/n tuyệt đối. Wilson 95% CI cho Completeness PoC 128/128 trên self-check: [0.971, 1.0]; baseline 8/128: [0.032, 0.119].
- Lệnh chính thức (giảng viên): `python -m src.evaluate --track t08 --pred out/poc/actions.csv --run-name poc --out reports/metrics_poc.json`.

## 06 · Result slices

| Slice (self-check) | N | Baseline | PoC | Ghi chú |
|---|---|---|---|---|
| expected=delete | 128 | 0.0625 | 1.0 | baseline chỉ chạm raw |
| expected=escalate | 34 | 0.0 | 1.0 | ACL 1, origin 3, multi-subject 24, lineage-mismatch 6 |
| expected=retain_legal_hold | 2 | 0.0 (cả 2 bị xóa) | 1.0 | |

**Ablation (bonus):** `hold_propagates_to_descendants: true` (`reports/selfcheck/table_ablation_hold.md`): Completeness 1.0 → **0.797** (102/128) trên self-check, False-delete vẫn 0. ⇒ 26 dòng (backup + frame/crop/embedding/annotation của `a0043`, `a0099`) là **điểm bất định lớn nhất**: nếu GT coi bản sao của dữ liệu bị hold cũng bị hold thì PoC mặc định sẽ có 26 false delete. Vì vậy 26 dòng này luôn vào review queue (`HELD_ANCESTOR:<id>` trong reason_code) — người duyệt pháp lý chốt.

- **Failure case 1 — baseline xóa dữ liệu legal hold, giữ bản sao định danh** (`DR-002`): baseline xóa `a0043` (raw_video, `legal_hold=LH-2026-03`) ⇒ hủy chứng cứ; đồng thời bỏ sót `a0044` (backup `.bak` trên glacier) và 3 embedding khuôn mặt (`a0047`, `a0051`, `a0055`). Root cause: khớp filename, không duyệt lineage, không kiểm cờ hold.
- **Failure case 2 — dữ liệu dùng chung / lineage lệch tag** (`DR-001`, `DR-004`, `DR-007`): frame `a0011` chứa subj_00 + subj_01; crop `a0012` + embedding `a0013` cắt từ frame này nhưng tag subj_00. BFS kéo chúng vào scope của subj_01 → PoC **escalate** (`R-LINEAGE-01`) thay vì xóa. Và `training_export a0211/a0212` (9–10 chủ thể) không thể xóa nguyên file ⇒ escalate; nghĩa là dữ liệu của người yêu cầu **vẫn còn** trong export đến khi rebuild — rủi ro còn tồn tại, phải theo dõi SLA (`request_summary.csv`).
- Latency: PoC 0.191 s cho 164 dòng (~1.2 ms/dòng, Python 3.15, AMD64 Windows 11); baseline 0.037 s.

## 07 · Human & decision

- **Queue:** `out/poc/review_queue.csv` (+ `contact_sheet.html` có dropdown + Export CSV), B = 80, hiện 62 dòng, sắp theo risk: hold 1.0 > identity 0.95 > ACL 0.9 > origin 0.8 > multi-subject 0.7 > lineage 0.6 > delete-dưới-hold 0.5. `item_id = request_id|artefact_id`.
- **Override:** reviewer điền `reviewer`, `reviewer_decision` (approve = giữ action đề xuất; edit/reject + `override_value` ∈ {delete, retain_legal_hold, escalate}; reject không giá trị / escalate = escalate; skip = vẫn pending) → lưu `out/poc/review_queue_reviewed.csv` → chạy lại. Dòng có quyết định mà thiếu tên reviewer ⇒ exit 2. Mọi thay đổi ghi `audit_log.jsonl` (actor, before, after, note, request_id); mỗi action cuối có 1 entry `item_id=artefact_id` (audit completeness 164/164).
- **Hard guardrail:** người review sửa `delete` lên artefact legal hold ⇒ decision **REWORK** (test `test_human_cannot_delete_legal_hold`).
- **Decision trước review:** `HOLD / PENDING_HUMAN_APPROVAL` (62 dòng chờ duyệt).
- **Review thực tế:** reviewer Chu Tuan Ngoc duyệt 62/62 dòng, cả 62 đều `approve` (giữ đề xuất của policy, kể cả 26 dòng `HELD_ANCESTOR` ⇒ xóa bản dẫn xuất, giữ bản gốc bị hold). Bằng chứng: `reports/review_queue.csv`, 62 entry `review:approve` trong `audit_log.jsonl`.
- **Decision sau review:** `GO / EXCEPTIONS_APPROVED`, owner "Chu Tuan Ngoc (Data Protection reviewer / policy owner)", 128 delete được duyệt; `dry_run: true` ⇒ receipts = 128 × `dry_run` (chưa xóa mock). Log: `reports/decision_log.csv` (HOLD → GO).
- **Mock delete:** chỉ khi GO và `dry_run: false`. Idempotent: lần chạy 2 → `already_deleted`, không xóa lại (test `test_reviewed_go_and_mock_delete_idempotent`). Resume sau khi bị ngắt dùng cùng `mock_store_state.json`.

## 08 · Reproduce & tests

```bash
pip install -r requirements.txt        # Python 3.15 trên Windows: PYYAML_FORCE_LIBYAML=0 pip install pyyaml
python -m tools.verify_manifests
python -m src.main --input fixtures/t08_deletion --config configs/lab.yaml --output out/poc --run-name poc --offline --seed 42
python -m src.main --track t08 --output out/baseline --run-name baseline --pipeline src.baselines.t08_filename_baseline --offline --seed 42
python -m tools.make_selfcheck_t08     # self-check gold (nội bộ)
python -m src.evaluate --track t08 --gt reports/selfcheck/gt_actions_selfcheck.csv --pred out/poc/actions.csv --run-name poc --out reports/selfcheck/metrics_poc_selfcheck.json
pytest -q
```

- `pytest -q`: **57 passed, 39 skipped** (skipped = cần GT giảng viên), 0 failed. 10 test nhóm trong `tests/test_t08.py`: E2E success, idempotency 2 lần, legal hold, backup+embedding trong scope, exception ACL/origin/multi-subject/export, identity chưa xác minh, input bị sửa ⇒ exit 2 (provenance), review thiếu reviewer ⇒ exit 2, human xóa legal hold ⇒ REWORK, GO + mock delete idempotent.
- Máy: Windows 11 (10.0.26200), AMD64 Family 25, Python 3.15.0 · git sha: cf5a905 (code), 844e01d (reports)

## 09 · Limitations & ethics

- Đây là tài liệu học tập, **không** tuyên bố "đạt GDPR / Luật 91/2025". Rule `R-HOLD-01`, `R-MULTI-01` có `needs_legal_review: true` — số điều phải đối chiếu văn bản gốc với DPO/legal.
- Chưa biết: GT xử lý bản sao/dẫn xuất của dữ liệu bị hold thế nào (ablation cho thấy chênh 26 dòng); escalate khiến dữ liệu người yêu cầu còn trong export/frame dùng chung — cần quy trình redact/rebuild + hẹn xóa khi hold hết hạn.
- Scale 10×: BFS O(V+E) vẫn nhẹ; nút thắt là **người review** (62 dòng/10 request ⇒ ~620 dòng). Cải tiến: auto-approve rule có độ chắc cao, gom review theo nhóm (cả cây con dưới 1 hold là 1 quyết định).
- Không dùng dữ liệu thật; không xóa gì thật (store là file JSON mock).
- Kế hoạch +24h: SLA dashboard + notification stub, cơ chế hẹn xóa khi hold release, so sánh California DROP (1 request → nhiều broker).

## 10 · Team contribution

| Học viên | Git author/email | Module / vai trò | Commit SHA | Trình bày? |
|---|---|---|---|---|
| Chu Tuan Ngoc (solo) | Tuan Ngoc / 352845328+tuanngoc653-ai@users.noreply.github.com | Tất cả: pipeline, engine, policy, baseline, tests, report | cf5a905, 844e01d | có |

## 11 · References

| URL | Ngày xem | Ý nghĩa |
|---|---|---|
| https://eur-lex.europa.eu/eli/reg/2016/679/oj (GDPR Art.17) | 2026-11-14 | Quyền xóa + ngoại lệ 17(3)(e) cho legal claims ⇒ rule hold thắng |
| Luật BVDLCN 91/2025 · NĐ 356/2025 (Research Shelf handout) | 2026-11-14 | Quyền xóa ở VN và ngoại lệ nghĩa vụ pháp lý — cần legal xác nhận số điều |
| Data Cards Playbook (Research Shelf) | 2026-11-14 | Provenance/lineage là điều kiện để xóa có chứng cứ ⇒ rule origin-unknown |

---

**Bảng 3 · Checklist chốt khi nộp**

- [x] Input mẫu không chứa PII thật (synthetic CC0).
- [x] Đúng 1 lệnh run, 1 lệnh evaluate, 1 lệnh tests; không cần download khi chạy.
- [x] Có baseline/PoC metrics (self-check), ≥1 failure case, decision log, reviewer evidence (`reports/review_queue.csv`, 62/62 approve).
- [x] License/source provenance, seed, offline, N.
- [x] Link repo + tag `demo-180m`; commit SHA trong bảng trên. [ ] tag `final-24h`.
