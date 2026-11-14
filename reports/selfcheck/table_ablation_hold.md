Baseline: `poc` (git b0ce8bf, seed 42) · PoC: `ablation_hold` (git b0ce8bf, seed 42)

| Metric (unit) | Slice | Baseline | PoC | Δ abs | Δ rel | Better | N |
|---|---|---|---|---|---|---|---|
| **Deletion Completeness** (must-delete artefacts deleted / must-delete artefacts) | all | 1.0000 | 0.7969 | -0.2031 | -20.3% | higher | 164 |
| False-delete count (count (target 0): exceptions deleted + deletes outside request scope) | all | 0.0000 | 0.0000 | 0.0000 | baseline=0 | lower | None |
| Exception handling accuracy (legal-hold/escalate rows handled as expected / such rows) | all | 1.0000 | 1.0000 | 0.0000 | +0.0% | higher | None |
| Audit trace completeness (actions with an audit entry / actions) | all | 1.0000 | 1.0000 | 0.0000 | +0.0% | higher | None |
| Runtime / 100 items (s) | all | 0.1165 | 0.1250 | | | lower | |
