1. **Indexes.** I create a unique index on `employees.emp_code` so concurrent duplicate employee creation cannot succeed twice, and a unique compound index on `attendance_logs (emp_code, date)` so concurrent punch-ins for the same attendance day produce one success. I add date/employee and employee/punch-time indexes for attendance listing and punch-out lookup, plus date/status/employee to support filtered attendance queries. I also index department/join date for headcount and trend filtering. I considered a standalone `status` index but rejected it because status is low-cardinality and the compound indexes better match the query shapes.

2. **Punch-in race.** Both requests can look up the employee and calculate the same attendance date. They then attempt inserts. MongoDB's unique `(emp_code, date)` index accepts one insert; the other raises `DuplicateKeyError`, which becomes HTTP 409. I do not rely on a check-then-insert.

3. **Ties.** I use MongoDB standard competition ranking based on total late minutes. Employees tied at the cutoff share the same rank, and every employee whose rank is at most `limit` is returned, so the result may exceed `limit`. Rows are ordered by late minutes descending, then employee code ascending.

4. **Headcount.** Department summary starts from employees and looks up that month's logs with `preserveNullAndEmptyArrays`, so employees with no attendance documents remain in the group. Employees who joined after month-end are filtered out before the lookup.

5. **100x data.** I would validate query plans and workload with production-sized data, add targeted compound indexes based on measured access patterns, and consider pre-aggregated daily analytics/materialized summaries to reduce repeated expensive cross-collection aggregations.
