"""Combine a live SQL run and an IS JSON run into one gate: cross-checks, integrity, diff."""
import re

IN_ST = ["SA", "MDM", "PROJ", "POS"]
IS_ST = ["PREP", "STAGING", "PREDELIVERY", "DELIVERY"]
STAGES = IN_ST + IS_ST
STAGE_ORDER = {s: i for i, s in enumerate(STAGES)}  # unknown stages (e.g. an IS JSON stage we don't list) sort last, never crash
LBL = {"SA": "Shelf audit", "MDM": "Product master", "PROJ": "Projections", "POS": "Point of sale",
       "PREP": "Prep", "STAGING": "Staging", "PREDELIVERY": "Pre-delivery", "DELIVERY": "Delivery"}
SRC_LBL = {"LIVE": "Live SQL", "IS": "IS JSON", "CROSS": "Cross-check"}
ST_O = {"FAIL": 0, "BLOCKED": 1, "MANUAL_REQUIRED": 2, "PASS": 3}
SEV_O = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "INFO": 3}
XR_DESC = {"XR-001": "Live SA counts match the SA the IS run processed",
           "XR-002": "Store count change between stages within 5%"}
XR_TOL = 5.0
DIMS = ["accuracy", "completeness", "consistency", "timeliness", "uniqueness", "validity"]  # rule -> dimension map lives in checks_input.json


def fmsg(head, *bullets):
    """Check message: one-line verdict, then one '- ' bullet per fact. Empty bullets are dropped. The Checks table shows line 1 only."""
    return "\n".join([head] + [f"- {b}" for b in bullets if b])

# ---- live input sources, column roles and what each spec check needs ----
INPUTS = {
    "SA": ("Shelf audit (SA)", ["store_col", "upc_col", "grain", "measures", "delivery_col", "task_col", "week_col", "location_col",
                                "category_col", "delete_tstamp_col", "status_cd_col", "dropped_reason_col", "shelf_feet_col",
                                "facings_col", "capacity_col", "upc_width_col", "units_deep_col", "units_high_col",
                                "aisle_col", "side_col", "section_col", "shelf_col"]),
    "IMP": ("Imputed store sample", ["store_col", "delivery_col", "status_col", "imputed_value"]),
    "PRIOR": ("Prior wave SA", ["store_col", "shelf_feet_col", "facings_col", "capacity_col"]),
    "SHELFVAR": ("Shelf var master", ["id_col", "desc_col"]),
    "MDM": ("Product master (MDM)", ["upc_col", "grain", "sys_col", "vendor_col", "item_col", "product_col"]),
    "PROJ": ("Projections", ["store_col", "weight_col", "grain", "geo_col", "outlet_col", "geo_level_col", "week_col", "version_col"]),
    "POS": ("Point of sale (POS)", ["store_col", "upc_col", "grain", "measures", "week_col", "dollars_col", "units_col"]),
}
ROLE = {"store_col": "Store column", "upc_col": "UPC column", "grain": "Grain (unique together)", "measures": "Measures (negative check)",
        "delivery_col": "Delivery ID column", "task_col": "Task number column", "week_col": "IRI week column",
        "location_col": "Location column", "category_col": "Category column", "delete_tstamp_col": "Delete_tstamp column",
        "status_cd_col": "Status_Cd column", "dropped_reason_col": "Dropped_reason column", "shelf_feet_col": "Shelf feet column",
        "facings_col": "Facings column", "capacity_col": "Capacity column", "upc_width_col": "UPC width column",
        "units_deep_col": "Units deep column", "units_high_col": "Units high column", "status_col": "Imputed status column",
        "imputed_value": "Imputed status value", "id_col": "Shelf var ID column", "desc_col": "Description column",
        "sys_col": "Sys column", "gen_col": "Gen column", "vendor_col": "Vendor column", "item_col": "Item column",
        "weight_col": "Projection weight column", "geo_col": "CDM geo / geography column", "outlet_col": "Outlet column",
        "geo_level_col": "Geo level column", "dollars_col": "Dollars column", "units_col": "Units column", "product_col": "Product column(s)", "version_col": "Version column",
        "aisle_col": "Aisle column", "side_col": "Side column", "section_col": "Section column", "shelf_col": "Shelf column"}
_IMP = [("IMP", "store_col"), ("IMP", "delivery_col"), ("IMP", "status_col")]
_M3 = ["shelf_feet_col", "facings_col", "capacity_col"]
REQ = {
    "SA001": [("SA", "delivery_col")], "SA002": [("SA", "task_col")],
    "SA003": [("SA", "category_col"), ("SA", "delete_tstamp_col"), ("SA", "status_cd_col"), ("SA", "dropped_reason_col")],
    "SA004": [("SA", "week_col")], "SA005": [("SA", "location_col"), ("SHELFVAR", "id_col"), ("SHELFVAR", "desc_col")],
    "SA006": [("SA", "grain")], "SA007": [("SA", "store_col")] + _IMP, "SA008": [("SA", "store_col")] + _IMP,
    "SA010": [("SA", "store_col")] + _IMP, "SA011": [("SA", "store_col")] + _IMP,
    "SA009": [("SA", "store_col")] + [("SA", r) for r in _M3] + [("PRIOR", "store_col")] + [("PRIOR", r) for r in _M3] + _IMP,
    "SA012": [("SA", "category_col"), ("SA", "shelf_feet_col")], "SA013": [("SA", "category_col"), ("SA", "facings_col")],
    "SA014": [("SA", "store_col")], "SA018": [("SA", "category_col"), ("SA", "grain")], "SA015": [("SA", "upc_col")],
    "SA016": [("SA", "shelf_feet_col"), ("SA", "facings_col"), ("SA", "upc_width_col")],
    "SA017": [("SA", "capacity_col"), ("SA", "facings_col"), ("SA", "units_deep_col"), ("SA", "units_high_col")],
    "MDM001": [("MDM", "upc_col")], "MDM003": [("MDM", "table")], "MDM005": [("MDM", "table")], "MDM004": [("MDM", "product_col")], "MDM002": [("MDM", r) for r in ("upc_col", "sys_col", "vendor_col", "item_col")],
    "PRJ001": [("PROJ", "geo_col")], "PRJ002": [("PROJ", "store_col")], "PRJ003": [("PROJ", "outlet_col")],
    "PRJ004": [("PROJ", "geo_level_col")], "PRJ005": [("PROJ", "week_col")], "PRJ006": [("PROJ", "table")], "PRJ007": [("PROJ", "version_col")],
    "POS001": [("POS", "store_col"), ("POS", "upc_col")], "POS006": [("POS", "table")], "POS002": [("POS", "upc_col")], "POS003": [("POS", "week_col")],
    "POS004": [("POS", "dollars_col")], "POS005": [("POS", "units_col")],
}
REQ_INPUT = {"SA001": ("expected_delivery_id", "Expected delivery ID"), "SA002": ("expected_task", "Expected task number"),
             "SA004": ("expected_weeks", "Expected IRI week(s)")}
CUSTOM_TESTS = ["Not null", "Unique", "Allowed values", "Range", "Custom SQL"]
CUSTOM_SRC = ["SA", "MDM", "PROJ", "POS"]
CUSTOM_SEV = ["CRITICAL", "HIGH", "WARNING", "INFO"]
CUSTOM_COLS = ["Run", "ID", "Source", "Test", "Column", "Value / SQL", "Severity", "Description"]


def custom_clean(rows):
    """Editor rows -> check dicts. Blank rows are dropped and a blank ID becomes CUS-001, CUS-002 ... by row position."""
    out = []
    for i, r in enumerate(rows or [], 1):
        def g(k):
            v = r.get(k)
            return "" if v is None or (isinstance(v, float) and v != v) else str(v).strip()
        if not any(g(k) for k in ("ID", "Source", "Test", "Column", "Value / SQL", "Description")):
            continue
        run = r.get("Run")
        out.append({"run": True if run is None or (isinstance(run, float) and run != run) else bool(run), "id": g("ID") or f"CUS-{i:03d}",
                    "source": g("Source"), "test": g("Test"), "column": g("Column"), "value": g("Value / SQL"),
                    "severity": g("Severity") if g("Severity") in CUSTOM_SEV else "WARNING", "description": g("Description")})
    return out


def custom_range(v):
    parts = [p.strip() for p in str(v or "").split(",")]
    if len(parts) != 2 or not any(parts):
        raise ValueError("enter the range as min,max (either may be blank)")
    try:
        lo, hi = (float(p) if p else None for p in parts)
    except ValueError:
        raise ValueError("min and max must be numbers")
    return lo, hi


def custom_problem(c, cfg, builtin_ids, taken):
    """Why a user-added check cannot run yet ('' = ready). Used by the setup page and by the runner, so both agree."""
    if c["id"] in builtin_ids:
        return f"ID {c['id']} is already a built-in check, use another ID"
    if c["id"] in taken:
        return f"ID {c['id']} is used twice"
    if c["source"] not in CUSTOM_SRC:
        return "choose a source"
    if c["test"] not in CUSTOM_TESTS:
        return "choose a test"
    if not ((cfg.get("sources") or {}).get(c["source"]) or {}).get("table"):
        return f"set {INPUTS[c['source']][0]} table"
    if c["test"] != "Custom SQL" and not c["column"]:
        return "enter the column"
    if c["test"] == "Allowed values" and not c["value"]:
        return "enter the allowed values, comma separated"
    if c["test"] == "Custom SQL" and not c["value"]:
        return "enter the SQL (a SELECT that returns the failing rows)"
    if c["test"] == "Range":
        try:
            custom_range(c["value"])
        except ValueError as e:
            return str(e)
    return ""


# ---- MSL tab: location grain (store is always part of the grain; the roles below are the SA location columns added by each rule) ----
MSL_RULES = {"Rule 1: Store, aisle": ["aisle_col"],
             "Rule 2: Store, aisle, side": ["aisle_col", "side_col"],
             "Rule 3: Store, aisle, side, section": ["aisle_col", "side_col", "section_col"],
             "Rule 4: Store, aisle, side, section, shelf": ["aisle_col", "side_col", "section_col", "shelf_col"],
             "Rule 5: Store, location": ["location_col"]}
MSL_THRESHOLDS = [50, 55, 60, 65, 70]

# Worked example shown in the MSL tab (illustrative numbers, not client data): facings of one product, one row per physical position.
MSL_EX_COLS = ["Store", "Aisle", "Side", "Section", "Shelf", "Location", "Facings"]
MSL_EX_BASE = [("101", "4", "L", "2", "3", "L1", 24), ("101", "4", "L", "2", "5", "L2", 10), ("101", "4", "R", "1", "2", "L3", 6),
               ("102", "4", "L", "2", "3", "L1", 35), ("102", "4", "L", "2", "5", "L2", 5)]
MSL_EX_IDX = {"aisle_col": 1, "side_col": 2, "section_col": 3, "shelf_col": 4, "location_col": 5}
MSL_EX_LBL = {"aisle_col": "Aisle", "side_col": "Side", "section_col": "Section", "shelf_col": "Shelf", "location_col": "Location"}


def msl_example(rule, threshold):
    """Same arithmetic as msl_sql on the worked-example rows: facings summed per store x the rule's location grain; dominant = most
    facings (ties: lowest location value); share = 100 x dominant facings / store total facings."""
    idx, out = [MSL_EX_IDX[r] for r in MSL_RULES[rule]], []
    for s in dict.fromkeys(r[0] for r in MSL_EX_BASE):
        g = {}
        for r in (x for x in MSL_EX_BASE if x[0] == s):
            k = tuple(r[i] for i in idx)
            g[k] = g.get(k, 0) + r[6]
        rows = sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))
        tot = sum(g.values())
        share = 100.0 * rows[0][1] / tot
        out.append({"store": s, "groups": [(" / ".join(k), f) for k, f in rows], "total": tot, "dominant": " / ".join(rows[0][0]),
                    "dom_fac": rows[0][1], "share": share, "passes": share >= threshold})
    return out


def msl_missing(cfg, rule, level=""):
    """Mappings the MSL tab still needs from SA and MDM. Empty list = ready to run."""
    srcs, out = cfg.get("sources") or {}, []
    for s, roles in (("SA", ["store_col", "upc_col", "facings_col", *MSL_RULES[rule]]), ("MDM", ["upc_col", "product_col"])):
        o = srcs.get(s) or {}
        if not o.get("table"):
            out.append(f"{INPUTS[s][0]} table")
            continue
        out += [f"{INPUTS[s][0]} → {ROLE[r]}" for r in roles if not o.get(r)]
    if level and level not in ((srcs.get("MDM") or {}).get("product_col") or []):
        out.append(f"Product '{level}' is not one of the mapped Product master → {ROLE['product_col']}")
    return list(dict.fromkeys(out))


NEEDS_DELIVERY = {"SA007", "SA008", "SA009", "SA010", "SA011"}
OPTIONAL = {"PRIOR": "no prior wave table entered"}  # a source that may legitimately not exist: its checks are not applicable, not Manual


def not_applicable(cfg):
    """{rule: reason} for spec checks that depend on an optional source whose table is blank."""
    srcs = cfg.get("sources") or {}
    return {r: OPTIONAL[s] for r, req in REQ.items() for s, _ in req if s in OPTIONAL and not (srcs.get(s) or {}).get("table")}

# Input checks that run automatically on every mapped source (not in REQ: an unmapped source is skipped, never Manual)
GEN = {"IN-001": {"roles": None, "cols": ["row count"]},
       "IN-002": {"roles": ["store_col", "upc_col", "grain"]},
       "IN-005": {"roles": ["measures"]},
       "PRJ-001": {"roles": ["weight_col"], "only": ["PROJ"]}}


def generic_runs(rule, cfg):
    """{source: [columns]} an input check will test with the current mapping. Empty = it will not run."""
    g, out = GEN[rule], {}
    for s in ("SA", "MDM", "PROJ", "POS"):
        o = (cfg.get("sources") or {}).get(s) or {}
        if not o.get("table") or (g.get("only") and s not in g["only"]):
            continue
        if g["roles"] is None:
            out[s] = list(g["cols"])
            continue
        cols = []
        for r in g["roles"]:
            v = o.get(r)
            cols += [x for x in (v if isinstance(v, list) else [v]) if x]
        if cols:
            out[s] = list(dict.fromkeys(cols))
    return out


def missing_for(rule, cfg):
    """Mappings a spec check still needs. Empty list = ready to run."""
    srcs, out = cfg.get("sources") or {}, []
    for s, r in REQ.get(rule, []):
        o = srcs.get(s) or {}
        if not o.get("table"):
            out.append(f"{INPUTS[s][0]} table")
        elif not o.get(r):
            out.append(f"{INPUTS[s][0]} → {ROLE[r]}")
    if rule in REQ_INPUT and not str(cfg.get(REQ_INPUT[rule][0]) or "").strip():
        out.append(REQ_INPUT[rule][1])
    if rule in NEEDS_DELIVERY and not str(cfg.get("expected_delivery_id") or "").strip() and not (srcs.get("SA") or {}).get("delivery_col"):
        out.append("Expected delivery ID (or SA delivery ID column)")
    return list(dict.fromkeys(out))


def skipped(c):
    """A check the user switched off in Check readiness. Shown as Manual, never a blocker (older saved runs: matched by message)."""
    return bool(c.get("skipped")) or "switched off for this run" in str(c.get("message") or "")


def blocks(c):
    if c["status"] == "PASS" or skipped(c):
        return False
    if c["status"] == "BLOCKED":
        return True
    return c.get("severity") in ("CRITICAL", "HIGH")  # incl. Manual: an unmapped or switched-off CRITICAL/HIGH check has no evidence


def suspect(c):
    return c["status"] == "PASS" and bool(re.search(r"-\d+(\.\d+)?%", str(c.get("message") or "")))


def certify(cs, p):
    """Stage badge. None = not certified (something blocks). Bronze: nothing blocks. Silver: Bronze + every check evaluated
    + owner and SLO documented. Gold: Silver + every check passes. Informational only; the release gate still uses blocks()."""
    if not cs or any(blocks(c) for c in cs):
        return None
    if any(c["status"] in ("MANUAL_REQUIRED", "BLOCKED") for c in cs) or not (p.get("owner") and p.get("slo")):
        return "Bronze"
    return "Gold" if all(c["status"] == "PASS" for c in cs) and not any(suspect(c) for c in cs) else "Silver"


def dim_matrix(checks, dims):
    """(stage, dimension) -> worst status among the checks mapped to that dimension. Unmapped rules are ignored."""
    r2d = {r: d for d, rs in (dims or {}).items() for r in rs}
    out = {}
    for c in checks:
        d = r2d.get(c.get("rule"))
        if d:
            k, cur = (c["stage"], d), out.get((c["stage"], d), "PASS")
            out[k] = c["status"] if ST_O.get(c["status"], 9) < ST_O.get(cur, 9) else cur
    return out


def dim_detail(checks, dims):
    """Every non-passing check behind the dimension grid: one row per check, worst first. Same rule -> dimension map as dim_matrix."""
    r2d = {r: d for d, rs in (dims or {}).items() for r in rs}
    rows = [{"stage": c["stage"], "dimension": r2d[c["rule"]], "rule": c.get("rule"), "check_id": c["check_id"], "status": c["status"],
             "severity": c.get("severity"), "message": str(c.get("message") or "").splitlines()[0] if c.get("message") else ""}
            for c in checks if c.get("rule") in r2d and c["status"] != "PASS"]
    rows.sort(key=lambda r: (STAGE_ORDER.get(r["stage"], len(STAGES)), DIMS.index(r["dimension"]) if r["dimension"] in DIMS else 99,
                             ST_O.get(r["status"], 9), SEV_O.get(r["severity"], 9), r["rule"] or ""))
    return rows


def trend(check_id, report_name, runs):
    """Numeric actual_value for one check_id across every run of the same report, oldest first."""
    pts = []
    for r in runs:
        if r.get("report_name") != report_name:
            continue
        c = next((c for c in r.get("checks") or [] if c.get("check_id") == check_id), None)
        if not c or not isinstance(c.get("actual_value"), (int, float)):
            continue
        t = c.get("logged_at") or r.get("timestamp") or str(r.get("run_id"))
        pts.append((t, c["actual_value"]))
    pts.sort(key=lambda p: p[0])
    return pts


def pc(a, b):
    return abs(a - b) / b * 100 if b else 0.0


def fmt(n):
    if n is None:
        return ""
    if isinstance(n, (int, float)):
        return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"
    return str(n)


def schema_diff(prev_schema, cur_schema):
    """Column Added/Removed/Type Changed per source, vs the previous live run of the same report. Sources with a capture
    error are skipped (nothing to diff). Caller supplies a friendly label per source key (e.g. INPUTS[s][0])."""
    out = []
    for s, cur in (cur_schema or {}).items():
        if cur.get("error"):
            continue
        pcols, ccols = (((prev_schema or {}).get(s) or {}).get("columns") or {}), (cur.get("columns") or {})
        for col, t in ccols.items():
            if col not in pcols:
                out.append({"source": s, "column": col, "event": "Added", "details": f"New column, type {t}"})
            elif pcols[col] != t:
                out.append({"source": s, "column": col, "event": "Type Changed", "details": f"Previous Type: {pcols[col]}; New Type: {t}"})
        for col, t in pcols.items():
            if col not in ccols:
                out.append({"source": s, "column": col, "event": "Removed", "details": f"Previous Type: {t}"})
    return out


def run_key(r):
    return f"{r.get('report_name')}|{r.get('run_id')}"


def prev(runs, r):
    same = [x for x in runs if x.get("report_name") == r.get("report_name") and str(x.get("run_id")) < str(r.get("run_id"))]
    return max(same, key=lambda x: str(x.get("run_id"))) if same else None


def _m(c, pat):
    if not c:
        return None
    m = re.search(pat, str(c.get("message") or ""))
    return float(m.group(1).replace(",", "")) if m else None


def is_counts(J):
    f = lambda r: next((c for c in J["checks"] if c.get("rule") == r), None)
    sa5 = f("SA-005")
    sa_s = float(sa5["actual_value"]) if sa5 and sa5.get("actual_value") is not None else _m(f("SA-014"), r"'store_number':\s*(\d+)")
    return {"saS": sa_s, "saU": _m(f("SA-014"), r"'upc_number':\s*(\d+)"),
            "prep": _m(f("PREP-002"), r"(?i)->\s*([\d,]+)\s*prep"),
            "stg": _m(f("STG-008"), r"'ALL STORES':\s*(\d+)")}


def cross(L, J):
    out = []
    if not J:
        return out
    c = is_counts(J)
    lp = ((L or {}).get("live_profile") or {}).get("SA") or {}

    def mk(cid, rule, stage, sev, ok, msg, p, a, e):
        out.append({"check_id": cid, "rule": rule, "stage": stage, "source": "CROSS", "status": "PASS" if ok else "FAIL",
                    "passed": ok, "severity": sev, "message": msg, "affected_pct": round(p, 4),
                    "actual_value": a, "expected_value": e, "sample_failing_rows": []})

    if lp.get("stores") is not None and c["saS"] is not None:
        ok = lp["stores"] == c["saS"]
        mk("XR_SA_stores", "XR-001", "SA", "CRITICAL", ok,
           fmsg("IS run and live SA have the same store count" if ok else "IS run and live SA store counts differ",
                f"IS run processed: {fmt(c['saS'])}", f"Live SA table: {fmt(lp['stores'])}"), pc(c["saS"], lp["stores"]), c["saS"], lp["stores"])
    if lp.get("upcs") is not None and c["saU"] is not None:
        ok = lp["upcs"] == c["saU"]
        mk("XR_SA_upcs", "XR-001", "SA", "CRITICAL", ok,
           fmsg("IS run and live SA have the same UPC count" if ok else "IS run and live SA UPC counts differ",
                f"IS run processed: {fmt(c['saU'])}", f"Live SA table: {fmt(lp['upcs'])}"), pc(c["saU"], lp["upcs"]), c["saU"], lp["upcs"])
    base = (lp["stores"], "live SA") if lp.get("stores") is not None else (c["saS"], "IS SA") if c["saS"] is not None else None
    if base and c["prep"] is not None:
        d = pc(c["prep"], base[0])
        mk("XR_PREP_stores", "XR-002", "PREP", "WARNING", d <= XR_TOL,
           fmsg(f"Prep store count is within {XR_TOL:g}% of {base[1]}" if d <= XR_TOL else f"Prep store count moved more than {XR_TOL:g}% from {base[1]}",
                f"Prep: {fmt(c['prep'])}", f"{base[1]}: {fmt(base[0])}", f"Absolute change: {d:.2f}%"), d, c["prep"], base[0])
    if c["prep"] is not None and c["stg"] is not None:
        d = pc(c["stg"], c["prep"])
        mk("XR_STG_stores", "XR-002", "STAGING", "WARNING", d <= XR_TOL,
           fmsg(f"Staging store count is within {XR_TOL:g}% of Prep" if d <= XR_TOL else f"Staging store count moved more than {XR_TOL:g}% from Prep",
                f"Staging (all stores): {fmt(c['stg'])}", f"Prep: {fmt(c['prep'])}", f"Absolute change: {d:.2f}%"), d, c["stg"], c["prep"])
    return out


def recount(ch):
    k = lambda s: sum(c["status"] == s for c in ch)
    return {"total_checks": len(ch), "passed": k("PASS"), "failed": len(ch) - k("PASS"),
            "critical_failures": sum(c.get("severity") == "CRITICAL" and c["status"] == "FAIL" for c in ch),
            "high_failures": sum(c.get("severity") == "HIGH" and c["status"] == "FAIL" for c in ch),
            "manual_required": k("MANUAL_REQUIRED"), "blocked": k("BLOCKED"),
            "critical_unresolved": sum(c.get("severity") == "CRITICAL" and c["status"] != "PASS" and not skipped(c) for c in ch),
            "blocking": sum(blocks(c) for c in ch), "suspect": sum(suspect(c) for c in ch)}


def build(L, J, live_runs, is_runs, live_enabled=True):
    if not L and not J:
        return None
    checks = ([{**c, "source": "LIVE"} for c in L["checks"]] if L else []) + \
             ([{**c, "source": "IS"} for c in J["checks"]] if J else []) + cross(L, J)
    guard = []
    if L:  # every enabled required rule must have produced a result; a missing one is BLOCKED, never a silent pass
        got = {c.get("rule") for c in L["checks"]}
        for r, meta in (L.get("expected_rules") or {}).items():
            if r not in got:
                checks.append({"check_id": f"LIVE_MISSING_{r}", "rule": r, "stage": meta.get("stage") or "SA", "source": "LIVE",
                               "status": "BLOCKED", "passed": False, "severity": meta.get("severity"), "affected_pct": 0,
                               "message": f"{r} is enabled but produced no result in this run", "actual_value": None,
                               "expected_value": None, "sample_failing_rows": []})
                guard.append(f"Live {r} is enabled but produced no result; treated as BLOCKED.")
        if L.get("disabled_rules"):
            guard.append(f"Disabled in checks_input.json, not run: {', '.join(L['disabled_rules'])}.")
        if L.get("skipped_rules"):
            guard.append(f"Switched off or not applicable for this run, shown as Manual and not blocking. Put them in the release notes: {', '.join(L['skipped_rules'])}.")
    x = {"L": L, "J": J, "checks": checks, "diff": {}, "issues": [],
         "desc": {**((J or {}).get("rule_descriptions") or {}), **((L or {}).get("rule_descriptions") or {}), **XR_DESC}}

    for r, runs in ((L, live_runs), (J, is_runs)):
        if not r:
            continue
        p = prev(runs, r)
        if not p:
            continue
        pm = {c["check_id"]: c for c in p["checks"]}
        for c in r["checks"]:
            o = pm.get(c["check_id"])
            x["diff"][c["check_id"]] = ("New check" if not o else f"{o['status']} → {c['status']}" if o["status"] != c["status"]
                                        else "Message changed" if o.get("message") != c.get("message") else "")

    present, iss = {c["stage"] for c in checks}, x["issues"]
    iss.extend(guard)
    if J:
        s, re_ = J.get("summary") or {}, recount(J["checks"])
        for f in ("total_checks", "passed", "failed", "critical_failures", "high_failures", "manual_required"):
            if s.get(f) is not None and s[f] != re_[f]:
                iss.append(f"IS file summary says {f.replace('_', ' ')} = {s[f]}; its checks give {re_[f]}.")
        rt = s.get("stage_runtime_s") or {}
        gap = (s.get("total_runtime_s") or 0) - sum(rt.values())
        if s.get("total_runtime_s") and gap > 30:
            iss.append(f"IS run: {gap:,.1f}s of {s['total_runtime_s']:,.1f}s runtime not attributed to any stage.")
        miss = [LBL[t] for t in IS_ST if t not in present]
        if miss:
            iss.append(f"IS JSON has no checks for: {', '.join(miss)}.")
    else:
        iss.append("No IS JSON loaded. Transform stages are not checked.")
    if L:
        miss = [LBL[t] for t in IN_ST if t not in present]
        if miss:
            iss.append(f"Live inputs not configured: {', '.join(miss)}.")
    else:
        iss.append("Live input checks not run. Open Live input setup." if live_enabled else "Live input checks unavailable.")
    ids = {}
    for c in checks:
        ids[c["check_id"]] = ids.get(c["check_id"], 0) + 1
    dup = [i for i, n in ids.items() if n > 1]
    if dup:
        iss.append(f"Duplicate check IDs: {', '.join(dup)}.")
    for c in checks:
        if c["status"] == "BLOCKED":
            iss.append(f"{SRC_LBL[c['source']]} {c['stage']} {c['rule']} did not run: {str(c.get('message')).splitlines()[0][:220]}")
    rc = recount(checks)
    if rc["critical_unresolved"] > rc["critical_failures"]:
        iss.append(f"{rc['critical_unresolved'] - rc['critical_failures']} CRITICAL checks are unresolved (manual or blocked) and still block release.")
    hi = sum(c["status"] == "MANUAL_REQUIRED" and c.get("severity") == "HIGH" and not skipped(c) for c in checks)
    if hi:
        iss.append(f"{hi} HIGH checks are Manual (not mapped, or switched off) and block release.")
    for c in checks:
        if suspect(c):
            iss.append(f"{c['stage']} {c['rule']} passed with a negative change: \"{str(c['message']).splitlines()[0]}\". Confirm it tests absolute deviation.")

    x["counts"] = rc
    x["blocking"] = [c for c in checks if blocks(c)]
    x["need_del"] = bool(J) and "DELIVERY" not in present
    x["fail"] = bool(x["blocking"]) or x["need_del"]
    x["warn"] = any(c["status"] != "PASS" for c in checks) or rc["suspect"] > 0
    lp = ((L or {}).get("live_profile") or {}).get("SA") or {}
    c = is_counts(J) if J else {}
    x["trace"] = [(s, n) for s, n in (("Shelf audit (live SQL)", lp.get("stores")), ("Shelf audit (IS run)", c.get("saS")),
                                      ("Prep (IS)", c.get("prep")), ("Staging all stores (IS)", c.get("stg"))) if n is not None]
    return x
