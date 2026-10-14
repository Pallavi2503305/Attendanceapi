# REVIEW.md

**Important scope limitation:** the uploaded candidate kit did not include the starter `app/main.py`, so I could not honestly enumerate line-specific defects in the colleague's original implementation. The implementation in `app/main.py` is a fresh implementation against the supplied OpenAPI contract and data model, not a verified patch of the missing starter.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you'd fix it |
|---|---|---|---|---|
| 1 | Starter `app/main.py` not supplied | Original starter defects cannot be verified without the source. | Compare this implementation against the original starter and run the contract tests. | Supply the starter file to complete a line-by-line defect review. |

## Looked at and not treated as defects
- MongoDB `_id` is internal and is not exposed by API responses; the public identifiers are `emp_code` and (`emp_code`, `date`).
- `date`, `joined_on`, and shift times remain strings in MongoDB because they represent calendar values, not instants.
- Missing legacy `history` and `half_day` fields are treated as `[]` and `false`, respectively.
- Analytics consume stored `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` values rather than recalculating them.
