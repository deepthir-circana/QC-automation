# QC Gate user guide

QC Gate checks the inputs to a wave (shelf audit, product master, projections, point of sale) live on SQL Server, combines them with the Intelligent Suite (IS) QC report, and gives one release decision.

## 1. Release rule
A wave does **not** release when any check:
- **fails at CRITICAL or HIGH**, or
- is **BLOCKED** (the check could not run), or
- is a **live CRITICAL check that could not be evaluated** because a column is not mapped (shown as Manual).

WARNING and INFO failures are caveats: put them in the release notes, they do not block.

Gate verdicts:
| Situation | Verdict |
|---|---|
| IS report loaded, something blocks, or the Delivery stage has no checks | Do not release |
| IS report loaded, nothing blocks, but a check is not Pass | Release with caveats |
| IS report loaded, everything passes | Release |
| No IS report (live inputs only) | Fix inputs before processing / Inputs ready to process |

## 2. Quick start
1. Open the sidebar **🔌 SQL Server**, keep **Authentication = Windows** (the account you are signed in with, no password), enter the default server and database, and click **Connect**. The success message shows the account SQL Server sees. Choose **SQL login** only when you are off the domain: the password stays in memory for the session and is never saved.
2. Open **🧪 Live input setup**. Enter the report name and the expected delivery ID, task number and IRI week(s).
3. On each source tab enter the **Table** (`schema.table` or `db.schema.table`). Leave Server and Database blank to use the sidebar defaults.
4. Click **Load columns · all sources**, then confirm every mapping. Only exact name matches are filled in.
5. Check **Check readiness** at the top. Anything marked Manual still needs a mapping.
6. Click **💾 Save setup** (so you can reload it next time), then **▶ Run live checks**. The result opens on the Gate.
7. To add the IS result, upload its `qc_report_*.json` under **📥 Load results** in the sidebar.

## 3. The views
- **🚦 Gate**: verdict, counts, one card per stage, plus expanders for owners/SLOs, quality dimensions, schema changes, discussions and report integrity.
- **🔎 Checks**: every check with filters (source, stage, status, severity, text, only blockers, changed since last run). Select a row for actual vs expected, the message, history, up to 10 sample failing rows, and feedback. Download the exceptions CSV or the combined gate JSON.
- **🧪 Live input setup**: mappings, readiness, save and run.
- **📈 History**: pass rate and blocking count across runs of a report.
- **🧮 SQL**: read-only queries and ILD templates.
- **🧲 MSL**: mostly-stocked-together test on Shelf audit and Product master (section 6a).
- **📖 Guide**: this page and the check reference.
- **💬 Help** (sidebar, every view): answers about the app and your current setup and run (section 10).

## 4. Status, severity and badges
| Status | Meaning |
|---|---|
| Pass | Check evaluated and met its rule |
| Fail | Check evaluated and did not meet its rule |
| Manual | Not run because a table, column or expected value is not set |
| Blocked | Could not run (SQL or driver error) or was expected but produced no result |

Severity is CRITICAL, HIGH, WARNING or INFO and is set per rule in `checks_input.json`.

**Suspect pass**: a passing check whose message shows a negative percentage. Confirm it tests absolute deviation.

**Stage badges** are informational and do not change the release decision:
- **Not certified**: something in the stage blocks.
- **Bronze**: nothing blocks.
- **Silver**: Bronze, every check evaluated, and an owner and SLO entered.
- **Gold**: Silver, every check passes, no suspect passes.

Enter owner and SLO under **📦 Data products** on the Gate. They are kept in the saved setup.

## 5. Live input setup
Expected values at the top:
- **Expected delivery ID**: SA001, and filters the imputed store sample for SA007 to SA009.
- **Expected task number**: SA002.
- **Expected IRI week(s)**: SA004, comma separated.

Sources (one tab each):
| Source | Used for |
|---|---|
| Shelf audit (SA) | Most SA checks |
| Imputed store sample | SA007, SA008, SA009. Imputed status null = collected, the imputed value (default Y) = imputed |
| Prior wave SA | SA009 wave-over-wave increase for imputed stores. Optional: leave the table blank when there is no prior wave and SA009 shows as not applicable (Manual, not blocking, put it in the release notes) |
| Shelf var master | SA005 location lookup (read-only, nothing is updated) |
| Product master (MDM) | MDM001 to MDM005. Product column(s) accepts several columns: MDM004 lists the distinct values and row counts of each, and the SQL tab ILD template uses the first one |
| Projections | PRJ and projection weight checks |
| Point of sale (POS) | POS checks |

Each tab says what is still needed. The Product master tab also links the Streamlit MDM validation tool (https://mdm-validation-app.streamlit.app/) for additional MDM checks (MDM005). A check with an unmapped column runs as **Manual**, and a Manual CRITICAL check blocks release. Server and Database can differ per source. The same connection identity (your Windows account or SQL login) is used for all of them.

**Check readiness** lists two kinds of check:
- **Spec checks** (SA, MDM, PRJ, POS) need specific columns. If one is not mapped the check runs as Manual.
- **Input checks** (IN-001 table has rows, IN-002 key columns not null, IN-005 no negative measures, PRJ-001 projection weights) run automatically on every source that has something mapped for them. The **Applies to** column shows the sources and columns each one will test. A source with nothing mapped is skipped, never Manual. IN-000 (server version, Info) always runs.

Untick **Run** to switch any check off for this run. It then shows as Manual, does not block release, and is listed in the report integrity findings: put it in the release notes.

## 5a. Your own checks
Add checks of your own under **🧪 Live input setup → ✅ Check readiness → ➕ Your own checks**. Each row is one check on a mapped source. They run with the live checks, count in the gate like any other check, and are saved with the setup.

| Column | What to enter |
|---|---|
| Run | Tick to run. Untick to skip: the check then shows as Manual and does not block |
| ID | Leave blank for CUS-001, CUS-002 … by row position. An ID cannot match a built-in check or repeat |
| Source | SA, MDM, PROJ or POS. That source's table must be set on its tab |
| Test | Not null, Unique, Allowed values, Range or Custom SQL |
| Column | A column name in the source table. Not needed for Custom SQL |
| Value / SQL | Depends on the test (below) |
| Severity | CRITICAL, HIGH, WARNING or INFO (default WARNING) |
| Description | Free text, shown in the check detail |

| Test | Column | Value / SQL | Example | Fails when |
|---|---|---|---|---|
| Not null | one column | blank | `facings` | the column is null |
| Unique | one or more, comma separated | blank | `store_number,cdm_geo` | a combination repeats (null keys ignored) |
| Allowed values | one column | comma separated list | `side` · `L,R` | a non-null value is not in the list |
| Range | one column | `min,max` (either may be blank) | `facings` · `0,50` | a non-null value is non-numeric or outside the range |
| Custom SQL | blank | one SELECT or WITH that returns the failing rows | `SELECT store_number FROM <schema.table> WHERE facings > 0 AND shelf_feet = 0` | the query returns any row (0 rows = Pass) |

- The examples are illustrations. Use your own column names, values and table.
- A warning under the table lists what is still missing ("These will run as Manual: …").
- A CRITICAL or HIGH row that fails, is Manual or is Blocked blocks release. WARNING and INFO go in the release notes.
- Custom SQL follows the same read-only rule as the SQL tab. Use the full `db.schema.table` name.

## 6. SQL tab
- Runs **one SELECT or WITH statement**. Anything that writes or executes is rejected. This is a guard, not a security boundary: use a read-only account or AD group.
- **Source** chooses the server and database. Max rows and Timeout limit the result.
- **Templates** (built from the mapped sources, then editable): ILD projected facings (cdm_geo × product), a combined SA × MDM × PROJ row sample, and a join row count and fan-out check. Click **Insert**, then **Run query**.
- Template inputs: **Product** (a Product column mapped on the Product master: the ILD template groups by it), optional **Product value(s)** (comma separated filter on that column), optional **Location** (filters the Shelf audit location column, matched to the Shelf var master description when that source is mapped on the same server, otherwise to the raw value; it is also the Location output column). Blank filters mean all. The projections table is not filtered by version: all its rows are joined.
- SA is filtered to the expected delivery ID when the SA delivery column is mapped and the ID is entered.
- SA and Projections must be on the same server. MDM may be on another one: its UPC and product rows for the SA UPCs are pulled when you run the query. The run stops with an error above 20,000 matching MDM rows.
- Read the join counts: SA rows with no MDM match and stores with no projection are dropped by the inner joins.

## 6a. MSL tab
Tests the location grain at which an item is "mostly stocked together" (MSL), measured by facings. It uses only the mapped **Shelf audit** and **Product master**.
- **Needs** (set in Live input setup): SA table, Store, UPC, Facings and the location columns of the chosen rule (**Aisle, Side, Section, Shelf** for rules 1 to 4, new on the Shelf audit tab and filled by Load columns when named aisle, side, section, shelf; **Location** for rule 5, the column already mapped for SA005); MDM table, UPC and **Product column(s)**. SA is filtered to the expected delivery ID when the SA delivery column is mapped. SA and MDM may be on different servers.
- **Product**: one of the Product columns mapped on the Product master (category, segment, sub brand, brand …).
- **Threshold**: 50%, 55%, 60%, 65% or 70%.
- **Rule**: 1 Store, aisle · 2 Store, aisle, side · 3 Store, aisle, side, section · 4 Store, aisle, side, section, shelf · 5 Store, location (uses the SA **Location column** already mapped for SA005).
- **How it is computed**: facings are summed per product × store × location at the rule grain. The dominant location is the one with the most facings (ties go to the lowest value). A store passes when its dominant facings are at or above the threshold share of the product's facings in that store. Rows with null facings, no product value or a null location column are left out and counted.
- **Insight line** (bold, underlined): for each selected product (blank = the 5 with the most stores), how many stores meet the threshold out of the stores that carry the product (stores with facings and a location at the rule grain), at the chosen rule.
- **Read**: the by-product table shows the share of stores passing, the facings-weighted dominant share and the most common dominant location. The row counts show SA rows, rows dropped by the MDM join and UPCs repeated in MDM: a repeated UPC multiplies its facings, so fix MDM first.
- **Run all 5 rules** runs Rules 1 to 5 one after another for the chosen Product and Threshold and shows the % of stores at or above the threshold per rule side by side. A rule whose location columns are not mapped is skipped and listed. It runs five queries, so it takes about five times as long as one rule.
- MSL results are not part of the gate decision and are not saved: download the CSV.

## 7. Feedback and discussions
- On a check, answer **Is this correctly classified?** and pick a reason: **Expected**, **Need to Review** or **Other**. Feedback is saved to `feedback.json`.
- Use **💬 Discussions** on the Gate for threads on a report. They are saved to `discussions.json`.

## 8. Files
| File or folder | Purpose |
|---|---|
| `checks_input.json` | Severity, thresholds, enabled flags, column hints and quality dimensions. Edit it without code changes. It applies on the next run |
| `circana_logo.jpg` | Sidebar logo: any `.png`, `.jpg`, `.jpeg` or `.svg` with "circana" in its name, next to `app.py` or in `assets/`. Nothing is shown if absent |
| `configs/` | Saved setups (no passwords) |
| `results/` | One JSON per live run. Runs are not reloaded automatically after a restart: upload them under Load results |
| `QC_Gate_Manual_Fallback_Runbook.md` | Manual SQL for every check when the app or SQL Server is unreachable |

## 9. Troubleshooting
| You see | Do this |
|---|---|
| Live SQL is off | Install pyodbc (`python -m pip install pyodbc`) and restart |
| Connect works but a source fails | Connect only tests the default server. Your account must also have access to every other server you mapped |
| Windows connect fails (login failed, anonymous logon) | Use a domain-joined Windows PC signed in with your work account, have ODBC Driver 17 installed, and ask for read access to the server. Otherwise switch to SQL login |
| "set … table" or "set … column" | Map the named table or column in Live input setup |
| Check shows Manual | An input, column or expected value is not set. See the Needs list on the source tab |
| Check shows Blocked | The query failed. The message has the SQL or driver error |
| Result looks wrong after a restart | Load the saved setup, and re-upload earlier runs under Load results |
| A fail you accept | Add feedback with reason Expected and a note, and put it in the release notes |

Before calling a failure a code issue, check upstream first: MDM mapping, projections and universe, SA, then POS.

## 10. Help
Open **💬 Help** in the sidebar. It works on every view and uses only the user guide, the manual fallback runbook, `checks_input.json` and the setup and run currently in the app. Nothing is sent outside the app and no AI model is used.
- **Why is my gate not Release?**: the verdict, each blocking check with the reason (failed, Manual or Blocked at Critical or High), and the non-blocking checks for the release notes.
- **Why is a check Manual or Blocked?**: the Manual and Blocked checks in the run on the gate, the checks the current setup would still run as Manual and what each needs, and the checks switched off for this run.
- **What do I map next?**: the next steps in order, starting with what unblocks Critical and High checks (⛔).
- **Why is a stage not Gold?**: what keeps each stage at its badge.
- **Ask about the app**: type a question. A rule ID (for example SA007 or IN-002) gives that check's description, severity, status on the gate and what it still needs. Other questions show the closest guide or runbook sections.

Help reads the files when they change, so keep this guide current with the app. If Help finds nothing, use **📖 Guide → 9. Troubleshooting**.

