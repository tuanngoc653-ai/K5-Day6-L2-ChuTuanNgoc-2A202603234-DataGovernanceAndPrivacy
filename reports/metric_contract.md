# Metric contract (khai báo bắt buộc — chốt ở phút 30, trước khi chạy eval)

> Các metric chuẩn đã được cài trong `src/metrics/tracks.py`. File này là nơi nhóm **cam kết** cách dùng chúng.
> Không đổi testset, threshold hay trọng số sau khi đã thấy kết quả eval.

| Mục | Khai báo của nhóm |
|---|---|
| Track | T08 · Retention & Delete Request Simulator |
| Primary metric (tên đúng như evaluator in ra) | `Deletion Completeness` |
| Tăng tốt hay giảm tốt? | higher (kèm guardrail `False-delete count` → lower, mục tiêu **0**) |
| Đơn vị đánh giá | cặp (request_id, artefact_id) |
| Công thức + numerator / denominator | Completeness = #(GT `delete` mà pred `delete`) / #(GT `delete`). False-delete = #(GT ≠ delete mà pred `delete`) + #(pred `delete` ngoài phạm vi GT). Exception accuracy = #(GT hold/escalate đúng action) / #(GT hold/escalate). Audit completeness = #action có entry `item_id` trong `audit_log.jsonl` / #action |
| Cách ghép prediction ↔ GT | exact join trên `(request_id, artefact_id)`; thiếu dòng = không xóa |
| Candidate pool cố định | 212 artefact trong `data_graph.json` × 10 request trong `requests.json` (sha256 trong `manifest.csv`) |
| Review budget B / K | B = 80 dòng; queue = mọi dòng hold/escalate + delete có tổ tiên bị legal hold (hiện 62); vượt budget → audit `review_budget_exceeded` |
| Seed | 42 (pipeline deterministic, không dùng random) |
| Threshold policy (chọn trên dev như thế nào) | Không có threshold số. Policy YAML `configs/policies/t08_deletion_policy.yaml` v`t08-policy-1.0`, precedence legal hold > identity > ACL > origin > multi-subject > lineage > delete; chốt 2026-11-14 từ đề bài + schema, **không** nhìn GT |
| Eval split (không dùng để tune) | Toàn bộ 10 request (GT ẩn của giảng viên). Self-check nội bộ `reports/selfcheck/gt_actions_selfcheck.csv` (`tools/make_selfcheck_t08.py`) — chỉ kiểm wiring, không phải số chính thức |
| Chỉ số lỗi xấu | **False delete** (xóa dữ liệu legal hold / không có quyền / của người khác) — không hoàn tác được. Thứ hai: miss (còn bản sao / embedding định danh) |
| Slices sẽ báo cáo (≥2) | theo `expected_action` (delete / retain_legal_hold / escalate — evaluator in sẵn); theo loại exception (ACL / origin / multi-subject / lineage) |
| Cách báo độ bất định khi N nhỏ | báo k/n tuyệt đối; Wilson CI 95% cho Completeness |
| Ngày giờ chốt + người chốt | 2026-11-14 · Chu Tuan Ngoc (solo) |

Nếu baseline = 0 thì ghi rõ "baseline = 0" thay vì phần trăm cải thiện.
