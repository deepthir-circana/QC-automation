# QC Gate user guide

QC Gate checks the inputs to a wave (shelf audit, product master, projections, point of sale) live on SQL Server, combines them with the Intelligent Suite (IS) QC report, and gives one release decision.

## 1. Release rule
A wave does **not** release when any check:
- **fails at CRITICAL or HIGH**, or
- is **BLOCKED** (the check could not run), or
- is a **live CRITICAL or HIGH check that could not be evaluated** because a column is not mapped, or because it was switched off for the run (shown as Manual).

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
5. Check **Check readiness** at the top. Anything marked Manual still needs a mapping. Untick **Run** on any check you do not need in this run (see section 5).
6. Click **💾 Save setup** (so you can reload it next time), then **▶ Run live checks**. The result opens on the Gate.
7. To add the IS result, upload its `qc_report_*.json` under **📥 Load results** in the sidebar.

## 3. The views
- **🚦 Gate**: verdict, counts, one card per stage, plus expanders for owners/SLOs, quality dimensions, schema changes, discussions and report integrity.
- **🔎 Checks**: every check with filters (source, stage, status, severity, text, only blockers, changed since last run). Select a row for actual vs expected, the message, history, up to 10 sample failing rows, and feedback. Download the exceptions CSV or the combined gate JSON.
- **🧪 Live input setup**: mappings, readiness, save and run.
- **📈 History**: pass rate and blocking count across runs of a report.
- **🧮 SQL**: read-only queries and ILD templates.
- **📖 Guide**: this page and the check reference.

## 4. Status, severity and badges
| Status | Meaning |
|---|---|
| Pass | Check evaluated and met its rule |
| Fail | Check evaluated and did not meet its rule |
| Manual | Not run because a table, column or expected value is not set, or because the check was switched off for this run |
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
| Prior wave SA | SA009 wave-over-wave increase for imputed stores |
| Shelf var master | SA005 location lookup (read-only, nothing is updated) |
| Product master (MDM) | MDM001, MDM002 |
| Projections | PRJ and projection weight checks |
| Point of sale (POS) | POS checks |

**Choosing which checks run.** Under **Check readiness**, untick **Run** to switch a check off for this run, or click **Reset: run all checks**.
- A switched-off check is not dropped: it shows as **Manual** with "switched off for this run", and is listed under Report integrity and in the saved results.
- A switched-off **Critical or High** check blocks release, the same as an unmapped one. Warning and Info checks become caveats for the release notes.
- The choice is kept in **💾 Save setup**. Checks disabled in `checks_input.json` are not listed. To change a check's severity, or to turn one of those on, edit that file. Your own checks (below) cover most other needs.

**Adding your own checks.** In **Check readiness**, scroll to **➕ Your own checks** and add a row:
| Test | Column | Value / SQL | Fails when |
|---|---|---|---|
| Not null | one column | | the column is null |
| Unique | one or more columns, comma separated | | the same key appears more than once (null keys ignored) |
| Allowed values | one column | `A,B,C` | the value is not in the list (nulls ignored) |
| Range | one column | `min,max` (either may be blank) | the value is non-numeric or outside the range (nulls ignored) |
| Custom SQL | | a `SELECT` that returns the failing rows | the query returns any row |

- Pick the **Source** (SA, MDM, PROJ or POS) and a **Severity**. A blank ID becomes CUS-001, CUS-002 …
- Your checks run with the live checks, appear in Checks and the stage cards, count in the gate by severity like any other check, and are saved with **💾 Save setup**. Untick **Run** to switch one off: it shows as Manual, like the built-in ones.
- A row that cannot run yet (table not set, column missing, ID already used) shows a warning and runs as Manual.
- Custom SQL is read-only (one SELECT or WITH) and runs on the chosen source's server and database. Up to 10,000 failing rows are counted and 10 are shown.

Each tab says what is still needed. A check with an unmapped column runs as **Manual**, and a Manual CRITICAL or HIGH check blocks release. Server and Database can differ per source. The same connection identity (your Windows account or SQL login) is used for all of them.

## 6. SQL tab
- Runs **one SELECT or WITH statement**. Anything that writes or executes is rejected. This is a guard, not a security boundary: use a read-only account or AD group.
- **Source** chooses the server and database. Max rows and Timeout limit the result.
- **Templates** (built from the mapped sources, then editable): ILD projected facings (cdm_geo × product), a combined SA × MDM × PROJ row sample, and a join row count and fan-out check. Click **Insert**, then **Run query**.
- Optional template inputs: a location label for the output column, and projection version(s).
- SA is filtered to the expected delivery ID when the SA delivery column is mapped and the ID is entered.
- SA and Projections must be on the same server. MDM may be on another one: its UPC and product rows for the SA UPCs are pulled when you run the query. The run stops with an error above 20,000 matching MDM rows.
- Read the join counts: SA rows with no MDM match and stores with no projection are dropped by the inner joins.

## 7. Feedback and discussions
- On a check, answer **Is this correctly classified?** and pick a reason: **Expected**, **Need to Review** or **Other**. Feedback is saved to `feedback.json`.
- Use **💬 Discussions** on the Gate for threads on a report. They are saved to `discussions.json`.

## 8. Files
| File or folder | Purpose |
|---|---|
| `checks_input.json` | Severity, thresholds, enabled flags, column hints and quality dimensions. Edit it without code changes. It applies on the next run. The scrolling banner at the top shows the messages in its `marquee` list; empty the list to hide it |
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
