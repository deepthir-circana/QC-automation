# QC Gate — Manual Fallback Runbook

**Use this when:** QC Gate (Streamlit app), SQL Server, or the Intelligent Suite output is
unreachable and a wave still needs to release on schedule. Run these checks directly in SSMS /
Excel and record results on the sign-off sheet at the end.

**Source of truth:** severity, thresholds and enabled flags come from `checks_input.json`. The
severity in each heading below mirrors it. If the file changes, update this runbook.

**Column names** are the defaults in `checks_input.json → column_hints` (for example `store_number`,
`upc_number`, `delivery_id`). If a table uses different names, substitute them: the logic doesn't change.
The imputed store sample columns (`store`, `delivery_id`, `imputed_status` below) must be the ones mapped on the
app's Imputed store sample tab.

**Release rule (same as the app):** a wave does **not** release if any **CRITICAL or HIGH** check
fails, is Manual (cannot be evaluated) or is Blocked (could not run). **WARNING and INFO** failures are
caveats for the release notes, not blockers.

Fill in before you start:
- Report / client: ______________________
- Delivery ID (expected): ______________________
- Task number (expected): ______________________
- IRI week(s) (expected, comma separated): ______________________
- SA table: ______________________   MDM table: ______________________
- PROJ table: ______________________   POS table: ______________________
- Imputed store sample: `Imputations_sa.dbo.Audit_Delivery_Store_Sample` (WIN0679), imputed value `Y`
- Shelf var master: `Imputations_SA.dbo.shelf_var_master` (WIN0679, read-only)
- Prior-wave SA table (for SA009): ______________________

Store keys are compared after removing leading zeros (0101721 = 101721). In the SQL below, `<key(x)>` means
`COALESCE(CAST(TRY_CAST(x AS bigint) AS varchar(50)), LTRIM(RTRIM(CAST(x AS varchar(100)))))`.

---

## Stage: Shelf Audit (SA)

### SA001 — Delivery IDs match the expected delivery ID (INFO)
```sql
SELECT delivery_id, COUNT(*) AS row_count
FROM <SA_table>
WHERE delivery_id IS NULL OR <key(delivery_id)> <> '<expected_delivery_id>'
GROUP BY delivery_id;
```
**Pass:** 0 rows returned.

### SA002 — Task numbers match the expected task number (INFO)
```sql
SELECT task_number, COUNT(*) AS row_count
FROM <SA_table>
WHERE task_number IS NULL OR CAST(task_number AS varchar(100)) <> '<expected_task_number>'
GROUP BY task_number;
```
**Pass:** 0 rows returned.

### SA003 — Delete_tstamp / Status_Cd / dropped_reason null where category is not null (CRITICAL)
```sql
SELECT
  SUM(CASE WHEN Delete_tstamp IS NOT NULL THEN 1 ELSE 0 END) AS delete_tstamp_nn,
  SUM(CASE WHEN Status_Cd IS NOT NULL THEN 1 ELSE 0 END) AS status_cd_nn,
  SUM(CASE WHEN dropped_reason IS NOT NULL THEN 1 ELSE 0 END) AS dropped_reason_nn
FROM <SA_table>
WHERE category IS NOT NULL;
```
**Pass:** all three counts are 0.

### SA004 — IRI weeks match the expected week(s) (INFO)
```sql
SELECT iri_week, COUNT(*) AS row_count
FROM <SA_table>
WHERE iri_week IS NULL OR CAST(iri_week AS varchar(100)) NOT IN ('<week1>', '<week2>')
GROUP BY iri_week;
```
**Pass:** 0 rows returned, and every expected week appears in `SELECT DISTINCT iri_week FROM <SA_table>`.

### SA005 — Every SA location resolves to one description in shelf_var_master (INFO, read-only)
Confirm read access to `WIN0679.Imputations_SA.dbo.shelf_var_master` first. Assumes numeric IDs; if the IDs are
not numeric, join on the trimmed text instead.
```sql
SELECT a.location, COUNT(*) AS sa_rows,
       COUNT(DISTINCT ISNULL(CAST(b.description AS nvarchar(4000)), N'(null)')) AS descriptions
FROM <SA_table> a
LEFT JOIN WIN0679.Imputations_SA.dbo.shelf_var_master b
  ON TRY_CAST(a.location AS bigint) = TRY_CAST(b.shelf_var_id AS bigint)
GROUP BY a.location
HAVING COUNT(DISTINCT ISNULL(CAST(b.description AS nvarchar(4000)), N'(null)')) <> 1
    OR MAX(CASE WHEN b.description IS NOT NULL THEN 1 ELSE 0 END) = 0;
```
**Pass:** 0 rows returned. Rows returned are locations that are not found, have no description, or match more
than one description. **Do not run any UPDATE of LOCATION_DESC here**: SA005 is read-only.

### SA006 — No duplicates at grain or across the full row (CRITICAL)
Grain (default): store × aisle × side × section × shelf × seq × UPC × location. Use the grain declared in the app.
```sql
-- Grain
SELECT store_number, aisle, side, section, shelf, seq, upc_number, location, COUNT(*) AS dup_count
FROM <SA_table>
GROUP BY store_number, aisle, side, section, shelf, seq, upc_number, location
HAVING COUNT(*) > 1;

-- Full row
SELECT <all columns>, COUNT(*) AS dup_count
FROM <SA_table>
GROUP BY <all columns>
HAVING COUNT(*) > 1;
```
**Pass:** 0 rows from both. Text, ntext and xml columns cannot be grouped: leave them out and note it.

### SA007 — SA non-imputed stores equal collected stores (CRITICAL, exact match)
Collected = imputed status null in the sample; imputed = `Y`. Run on WIN0679.
```sql
WITH c AS (SELECT DISTINCT <key(store)> AS s FROM Imputations_sa.dbo.Audit_Delivery_Store_Sample
           WHERE <key(delivery_id)> = '<expected_delivery_id>' AND imputed_status IS NULL),
     i AS (SELECT DISTINCT <key(store)> AS s FROM Imputations_sa.dbo.Audit_Delivery_Store_Sample
           WHERE <key(delivery_id)> = '<expected_delivery_id>' AND imputed_status = 'Y'),
     a AS (SELECT DISTINCT <key(store_number)> AS s FROM <SA_table>
           WHERE <key(delivery_id)> = '<expected_delivery_id>')
SELECT 'collected, not in SA' AS issue, s FROM c WHERE s NOT IN (SELECT s FROM a)
UNION ALL
SELECT 'in SA, neither collected nor imputed', s FROM a WHERE s NOT IN (SELECT s FROM i) AND s NOT IN (SELECT s FROM c);
```
**Pass:** 0 rows returned, and `SELECT COUNT(*) FROM c` is greater than 0. No tolerance.

### SA008 — Audit store sample matches SA stores (CRITICAL, both directions)
Same CTEs as SA007.
```sql
SELECT 'in sample, not in SA' AS issue, s FROM (SELECT s FROM c UNION SELECT s FROM i) u WHERE s NOT IN (SELECT s FROM a)
UNION ALL
SELECT 'in SA, not in sample', s FROM a WHERE s NOT IN (SELECT s FROM c) AND s NOT IN (SELECT s FROM i)
UNION ALL
SELECT 'imputed and collected', s FROM c WHERE s IN (SELECT s FROM i);
```
**Pass:** 0 rows returned and the sample has rows for the delivery. Stores with any other status are excluded.

### SA009 — Imputed stores: increase vs prior wave (HIGH, blocks release, more than 50% flags)
Run once per measure (shelf_feet, facings, capacity). Store-level sums; change is measured on the prior-wave base.
Imputed stores that are not in the prior wave are not assessed: count them and put them in the release notes.
```sql
SELECT cur.s AS store, pri.m AS prior_wave, cur.m AS current_wave,
       (cur.m - pri.m) / NULLIF(pri.m, 0) * 100.0 AS pct_change
FROM (
  SELECT <key(store_number)> AS s, SUM(CAST(shelf_feet AS float)) AS m
  FROM <SA_table>
  WHERE <key(delivery_id)> = '<expected_delivery_id>'
    AND <key(store_number)> IN (SELECT <key(store)> FROM Imputations_sa.dbo.Audit_Delivery_Store_Sample
                                WHERE <key(delivery_id)> = '<expected_delivery_id>' AND imputed_status = 'Y')
  GROUP BY <key(store_number)>
) cur
JOIN (
  SELECT <key(store_number)> AS s, SUM(CAST(shelf_feet AS float)) AS m
  FROM <prior_wave_SA_table>
  GROUP BY <key(store_number)>
) pri ON pri.s = cur.s
WHERE (pri.m <> 0 AND (cur.m - pri.m) / pri.m * 100.0 > 50)
   OR (pri.m = 0 AND cur.m > 0);
```
**Pass:** 0 rows returned per measure. A flagged store blocks release. Add `AND delivery_id = '<prior_delivery_id>'` to the prior-wave subquery only if that table holds more than one delivery.

### SA010 — Count of imputed stores (INFO, list only)
```sql
SELECT COUNT(DISTINCT <key(store)>) AS imputed_stores
FROM Imputations_sa.dbo.Audit_Delivery_Store_Sample
WHERE <key(delivery_id)> = '<expected_delivery_id>' AND imputed_status = 'Y';
```
Record the count in release notes.

### SA011 — Count of collected stores (INFO, list only)
```sql
SELECT COUNT(DISTINCT <key(store)>) AS collected_stores
FROM Imputations_sa.dbo.Audit_Delivery_Store_Sample
WHERE <key(delivery_id)> = '<expected_delivery_id>' AND imputed_status IS NULL;
```
Record the count in release notes.

### SA012 / SA013 — Shelf feet / Facings not null where category is not null (HIGH)
```sql
SELECT COUNT(*) AS bad_rows
FROM <SA_table>
WHERE category IS NOT NULL AND shelf_feet IS NULL;   -- repeat with facings for SA013
```
**Pass:** 0 for each.

### SA014 / SA015 — Distinct store_number / upc_number (INFO, list only)
```sql
SELECT COUNT(DISTINCT store_number) AS stores, COUNT(DISTINCT upc_number) AS upcs FROM <SA_table>;
```

### SA016 — Shelf feet = facings × upc_width / 12 (CRITICAL, tolerance 0, both sides rounded to 2 dp)
```sql
SELECT COUNT(*) AS bad_rows
FROM <SA_table>
WHERE shelf_feet IS NOT NULL AND facings IS NOT NULL AND upc_width IS NOT NULL
  AND ROUND(CAST(shelf_feet AS decimal(38,10)), 2) <> ROUND(CAST(facings * upc_width / 12.0 AS decimal(38,10)), 2);
```
**Pass:** 0.

### SA017 — Capacity = facings × units_deep × units_high (CRITICAL, tolerance 0)
```sql
SELECT COUNT(*) AS bad_rows
FROM <SA_table>
WHERE capacity IS NOT NULL AND facings IS NOT NULL AND units_deep IS NOT NULL AND units_high IS NOT NULL
  AND capacity <> facings * units_deep * units_high;
```
**Pass:** 0.

### SA018 — Grain columns not null where category is not null (INFO)
```sql
SELECT COUNT(*) AS bad_rows
FROM <SA_table>
WHERE category IS NOT NULL AND (store_number IS NULL OR aisle IS NULL OR side IS NULL OR section IS NULL
   OR shelf IS NULL OR seq IS NULL OR upc_number IS NULL OR location IS NULL);
```
Review the count per column if it is not 0.

---

## Stage: Product Master (MDM)

### MDM001 — UPC unique and complete (CRITICAL)
```sql
SELECT upc_number, COUNT(*) AS dup_count
FROM <MDM_table>
WHERE upc_number IS NOT NULL
GROUP BY upc_number
HAVING COUNT(*) > 1;

SELECT COUNT(*) AS null_upc_rows, (SELECT COUNT(DISTINCT upc_number) FROM <MDM_table>) AS distinct_upcs
FROM <MDM_table> WHERE upc_number IS NULL;
```
**Pass:** 0 duplicate rows and 0 null UPC rows. Record the distinct UPC count.

### MDM002 — sys(2) vendor(5) item(5) builds upc_number as bigint (CRITICAL)
```sql
SELECT *
FROM <MDM_table>
WHERE TRY_CAST(sys AS bigint) IS NULL OR TRY_CAST(vendor AS bigint) IS NULL OR TRY_CAST(item AS bigint) IS NULL
   OR TRY_CAST(upc_number AS bigint) IS NULL
   OR sys NOT BETWEEN 0 AND 99 OR vendor NOT BETWEEN 0 AND 99999 OR item NOT BETWEEN 0 AND 99999
   OR (CAST(sys AS bigint) * 10000000000 + CAST(vendor AS bigint) * 100000 + CAST(item AS bigint)) <> upc_number;
```
**Pass:** 0 rows returned.

### MDM003 — No duplicate rows across all columns (CRITICAL)
```sql
SELECT <all columns>, COUNT(*) AS dup_count
FROM <MDM_table>
GROUP BY <all columns>
HAVING COUNT(*) > 1;
```
**Pass:** 0 rows returned. Text, ntext and xml columns cannot be grouped: leave them out and note it.

### MDM004 — Distinct values per Product column (INFO, list only)
Repeat for each Product column.
```sql
SELECT <product_col> AS value, COUNT(*) AS row_count
FROM <MDM_table>
WHERE <product_col> IS NOT NULL
GROUP BY <product_col>
ORDER BY row_count DESC;

SELECT COUNT(*) AS null_rows FROM <MDM_table> WHERE <product_col> IS NULL;
```
**Pass:** listing only. Review the values and record anything unexpected in release notes.

### MDM005 — Additional MDM checks (INFO, reminder)
Run the Streamlit MDM validation tool: https://mdm-validation-app.streamlit.app/

---

## Stage: Projections (PROJ)

### PRJ001–PRJ005, PRJ007 — Distinct profiling (INFO, list only)
List the distinct values and record them in the release notes. No pass/fail.
```sql
SELECT cdm_geo, COUNT(*) AS row_count FROM <PROJ_table> GROUP BY cdm_geo ORDER BY cdm_geo;   -- PRJ001 (geography if that is the column)
SELECT COUNT(DISTINCT store_number) AS stores FROM <PROJ_table>;                              -- PRJ002
SELECT outlet, COUNT(*) AS row_count FROM <PROJ_table> GROUP BY outlet ORDER BY outlet;       -- PRJ003
SELECT geo_level, COUNT(*) AS row_count FROM <PROJ_table> GROUP BY geo_level ORDER BY geo_level;  -- PRJ004
SELECT iri_week, COUNT(*) AS row_count FROM <PROJ_table> GROUP BY iri_week ORDER BY iri_week;     -- PRJ005
SELECT version, COUNT(*) AS row_count FROM <PROJ_table> GROUP BY version ORDER BY version;        -- PRJ007
```

### PRJ006 — No duplicate rows across all columns (CRITICAL)
```sql
SELECT <all columns>, COUNT(*) AS dup_count
FROM <PROJ_table>
GROUP BY <all columns>
HAVING COUNT(*) > 1;
```
**Pass:** 0 rows returned.

### PRJ-001 — Projection weights not null, numeric and positive (CRITICAL)
```sql
SELECT COUNT(*) AS bad_rows
FROM <PROJ_table>
WHERE weight_col IS NULL OR TRY_CAST(weight_col AS float) IS NULL OR TRY_CAST(weight_col AS float) <= 0;
```
**Pass:** 0.

---

## Stage: Point of Sale (POS)

### POS001–POS003 — Counts and weeks (INFO, list only)
```sql
SELECT COUNT(DISTINCT store_number) AS stores, COUNT(DISTINCT upc_number) AS upcs FROM <POS_table>;          -- POS001
SELECT COUNT(upc_number) AS non_null_upc_rows, COUNT(DISTINCT upc_number) AS distinct_upcs FROM <POS_table>;  -- POS002
SELECT iri_week, COUNT(*) AS row_count FROM <POS_table> GROUP BY iri_week ORDER BY iri_week;                  -- POS003
```
Record the results in release notes.

### POS004 / POS005 — Dollars / Units not null (CRITICAL)
```sql
SELECT
  SUM(CASE WHEN dollars IS NULL THEN 1 ELSE 0 END) AS null_dollars,
  SUM(CASE WHEN units IS NULL THEN 1 ELSE 0 END) AS null_units
FROM <POS_table>;
```
**Pass:** both 0.

### POS006 — No duplicate rows across all columns (CRITICAL)
```sql
SELECT <all columns>, COUNT(*) AS dup_count
FROM <POS_table>
GROUP BY <all columns>
HAVING COUNT(*) > 1;
```
**Pass:** 0 rows returned. Large POS tables can be slow: run it for the wave's weeks only if needed and say so in release notes.

---

## Input checks (every mapped source: SA, MDM, PROJ, POS)

### IN-001 — Source table has rows (CRITICAL)
```sql
SELECT COUNT_BIG(*) AS row_count FROM <table>;
```
**Pass:** greater than 0.

### IN-002 — Store, UPC and grain columns have no null rows (CRITICAL)
```sql
SELECT SUM(CASE WHEN <col> IS NULL THEN 1 ELSE 0 END) AS null_rows FROM <table>;   -- one per mapped column
```
**Pass:** 0 for every column.

### IN-005 — Measures on SA or POS have no negative values (HIGH)
```sql
SELECT SUM(CASE WHEN <measure> < 0 THEN 1 ELSE 0 END) AS negative_rows FROM <table>;   -- one per mapped measure
```
**Pass:** 0 for every measure.

IN-000 (server version, INFO) is informational: `SELECT @@VERSION;`.

---

## Not covered manually (disabled in `checks_input.json`)
Legacy checks IN-003, IN-004, IN-007, SA-005, IMP-001, IMP-002 and IMP-003 are disabled because SA006,
SA007–SA009, SA012/SA013 and POS004/POS005 supersede them. The removed XS-001/002/003 cross-source checks are no
longer part of the gate. If a client needs UPC or store coverage across SA, MDM, PROJ or POS, run it ad hoc and record the result in the release notes.

---

## Sign-off

| Field | Value |
|---|---|
| Checked by | |
| Date | |
| Peer reviewer | |
| Any CRITICAL/HIGH failure, Manual or Blocked check? (Y/N) | |
| Release decision | Release / Release with caveats / Do not release |
| Caveats for release notes | |

*This runbook mirrors the logic in `checks_input.json` and `qc_live.py` as of this version. If a
rule's config (severity, thresholds, enabled flags, column names) changes in the app, update this document
to match.*
