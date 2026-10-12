"""Live input QC on SQL Server via pyodbc.

Produces a run dict in the same schema as the Intelligent Suite QC JSON (source = "LIVE").
All heavy work runs in SQL (aggregates, TOP 10 samples); only distinct store/UPC keys come
back to Python for cross-source comparison, so it scales to large fact tables and works
across servers without linked servers. Credentials are never written to disk.
"""
import datetime as dt
import json
import re
import time
from decimal import Decimal
from pathlib import Path

import pyodbc

import qc_core as core

DRIVER = "ODBC Driver 17 for SQL Server"
VERSIONS = {"11": "2012", "12": "2014", "13": "2016", "14": "2017", "15": "2019", "16": "2022", "17": "2025"}
SOURCES = ("SA", "MDM", "PROJ", "POS")
LIST_MAX = 200
CUSTOM_MAX = 10000  # custom SQL: failing rows counted up to this
NO_GROUP = {"text", "ntext", "image", "xml", "geography", "geometry"}


class Missing(Exception):
    """A table, column mapping or expected value the check needs is not set."""


# ---------------- helpers ----------------
def q(name):
    n = str(name).strip().strip("[]")
    return "[" + n.replace("]", "]]") + "]"


def qt(table):
    return ".".join(q(p) for p in str(table).split("."))


def ver(v):
    v = str(v or "")
    y = VERSIONS.get(v.split(".")[0])
    return f"SQL Server {y} (build {v})" if y else f"SQL Server build {v}"


def pct(a, b):
    return round(100.0 * a / b, 4) if b else 0.0


def num(x):
    return 0 if x is None else x


def clean(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def keystr(v):
    if isinstance(v, (float, Decimal)) and v == int(v):
        return str(int(v))
    return str(v).strip()


def norm(s):
    return s.lstrip("0") or "0"


def kx(col):
    """Store/delivery key normalised in SQL so 0101721, 101721 and 101721.0 all match."""
    return f"COALESCE(CAST(TRY_CAST({col} AS bigint) AS varchar(50)), LTRIM(RTRIM(CAST({col} AS varchar(100)))))"


def pkey(v):
    """Python twin of kx(): numeric strings lose leading zeros."""
    s = keystr(v)
    return str(int(s)) if s.isdigit() else s


def _id(s):
    return re.sub(r"\W", "_", str(s))


# ---------------- connection ----------------
def connect(server, database, user, password, timeout=15):
    """user=None: Windows authentication (the account running the app, no UID/PWD). Otherwise SQL login."""
    if user is None:
        auth = "Trusted_Connection=yes;"
    else:
        auth = f"UID={user};PWD={{{str(password).replace('}', '}}')}}};"
    cs = f"DRIVER={{{DRIVER}}};SERVER={server};DATABASE={database};{auth}TrustServerCertificate=yes;APP=QC_App"
    return pyodbc.connect(cs, timeout=timeout)


def test_connection(server, database, user, password):
    c = connect(server, database, user, password)
    try:
        v, who = c.cursor().execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(50)), SUSER_SNAME()").fetchone()
        return f"{ver(v)} · as {who}"
    finally:
        c.close()


def list_columns(server, database, table, user, password):
    parts = [p.strip().strip("[]") for p in str(table).split(".")]
    if len(parts) == 3:
        db, sc, tb = parts
    elif len(parts) == 2:
        db, (sc, tb) = database, parts
    else:
        db, sc, tb = database, "dbo", parts[-1]
    c = connect(server, database, user, password)
    try:
        rows = c.cursor().execute(
            f"SELECT COLUMN_NAME, DATA_TYPE FROM {q(db)}.INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION", sc, tb).fetchall()
    finally:
        c.close()
    if not rows:
        raise ValueError(f"No columns found for {table}. Check database, schema and table name.")
    return [(r[0], r[1]) for r in rows]


# ---------------- SQL templates: SA x MDM x PROJ projected facings ----------------
ILD_WEIGHT = "dec(16,2)"  # same cast as the ILD SQL, so totals match it


def fq(src):
    """[db].[schema].[table] so SA, MDM and PROJ can be joined in one statement on one server."""
    parts = str(src["table"]).split(".")
    return qt(src["table"] if len(parts) == 3 else f"{src['database']}." + (src["table"] if len(parts) == 2 else f"dbo.{src['table']}"))


MDM_TOKEN = "{{MDM_MAP}}"
_lit = lambda v: "'" + str(v).replace("'", "''") + "'"


def _ild_parts(cfg, versions=""):
    """Validate the SA / MDM / PROJ mapping and build the delivery and version filters. SA and PROJ must share a server;
    MDM may sit on another one (its UPC/Product rows are pulled into the query at run time)."""
    srcs = cfg.get("sources") or {}
    S, M, P = (srcs.get(k) or {} for k in ("SA", "MDM", "PROJ"))
    need = (("SA", S, ("store_col", "upc_col", "facings_col", "shelf_feet_col")), ("MDM", M, ("upc_col", "product_col")),
            ("PROJ", P, ("store_col", "weight_col", "geo_col")))
    miss = [f"{core.INPUTS[k][0]} table" if not o.get("table") else f"{core.INPUTS[k][0]} → {core.ROLE[r]}"
            for k, o, roles in need for r in (("table",) if not o.get("table") else roles) if r == "table" or not o.get(r)]
    if miss:
        raise Missing("set " + "; ".join(dict.fromkeys(miss)))
    srv = lambda o: str(o.get("server") or "").strip().lower()
    if srv(S) != srv(P):
        raise ValueError(f"Shelf audit ({S.get('server')}) and Projections ({P.get('server')}) must be on the same SQL Server: they are joined in SQL.")
    d, sa_w = str(cfg.get("expected_delivery_id") or "").strip(), ""
    if S.get("delivery_col") and d:
        sa_w = f" WHERE {kx(q(S['delivery_col']))} = {_lit(pkey(d))}"
    ver, pr_w = [v.strip() for v in str(versions or "").split(",") if v.strip()], ""
    if ver:
        if not P.get("version_col"):
            raise Missing("set Projections → Version column to filter by version")
        pr_w = f" WHERE CAST({q(P['version_col'])} AS varchar(100)) IN ({', '.join(_lit(v) for v in ver)})"
    return {"S": S, "M": M, "P": P, "sa_w": sa_w, "pr_w": pr_w, "remote": srv(M) != srv(S)}


def ild_sql(cfg, kind, location="", versions="", top_n=100):
    """T-SQL text for the SQL tab, built from the mapped SA / MDM / PROJ sources (read-only, editable by the user).
    kind: 'ild' grouped projected facings | 'rows' combined row sample | 'counts' row counts after each join + fan-out indicators.
    If MDM is on another server than SA, the md CTE holds the {{MDM_MAP}} token, expanded at run time by expand_maps()."""
    x = _ild_parts(cfg, versions)
    S, M, P, sa_w, pr_w = x["S"], x["M"], x["P"], x["sa_w"], x["pr_w"]
    md = (f"-- MDM is on {M.get('server')}: its UPC and Product rows are pulled at run time into md\n" if x["remote"] else "")
    md_body = MDM_TOKEN if x["remote"] else f"SELECT {q(M['upc_col'])} AS u, {q(M['product_col'])} AS product FROM {fq(M)}"
    cte = (md + f"WITH sa AS (SELECT {q(S['store_col'])} AS s, {q(S['upc_col'])} AS u, {q(S['facings_col'])} AS f, {q(S['shelf_feet_col'])} AS lf\n"
           f"            FROM {fq(S)}{sa_w}),\n"
           f"md AS ({md_body}),\n"
           f"pr AS (SELECT {q(P['store_col'])} AS s, {q(P['geo_col'])} AS geo, {q(P['weight_col'])} AS w FROM {fq(P)}{pr_w}),\n"
           "j1 AS (SELECT sa.s, sa.u, sa.f, sa.lf, md.product FROM sa JOIN md ON md.u = sa.u),\n"
           "j2 AS (SELECT j1.s, j1.u, j1.f, j1.lf, j1.product, pr.geo, pr.w FROM j1 JOIN pr ON pr.s = j1.s)\n")
    if kind == "ild":
        loc = f"{_lit(location)} AS Location,\n       " if str(location or "").strip() else ""
        return (cte + f"SELECT geo AS cdm_geo, product AS Product,\n       {loc}COUNT(DISTINCT u) AS UPCCount,\n"
                f"       SUM(f * CAST(w AS {ILD_WEIGHT})) AS TotalFacings,\n       SUM(lf * CAST(w AS {ILD_WEIGHT})) AS TotalLinearFeet,\n"
                "       COUNT(DISTINCT s) AS storecount\nFROM j2\nGROUP BY geo, product\nORDER BY geo, product")
    if kind == "rows":
        return (cte + f"SELECT TOP {int(top_n)} s AS store, u AS upc, product, geo AS cdm_geo, f AS facings, lf AS shelf_feet, w AS weight,\n"
                f"       f * CAST(w AS {ILD_WEIGHT}) AS projected_facings, lf * CAST(w AS {ILD_WEIGHT}) AS projected_linear_feet\n"
                "FROM j2\nORDER BY s, u, geo")
    if kind == "counts":
        return (cte + "SELECT (SELECT COUNT_BIG(*) FROM sa) AS sa_rows, (SELECT COUNT_BIG(DISTINCT s) FROM sa) AS sa_stores,\n"
                "       (SELECT COUNT_BIG(*) FROM j1) AS after_mdm_join,\n"
                "       (SELECT COUNT_BIG(*) FROM sa WHERE NOT EXISTS (SELECT 1 FROM md WHERE md.u = sa.u)) AS sa_rows_no_mdm,\n"
                "       (SELECT COUNT_BIG(*) FROM (SELECT u FROM md GROUP BY u HAVING COUNT_BIG(*) > 1) x) AS mdm_dup_upcs,\n"
                "       (SELECT COUNT_BIG(*) FROM j2) AS after_proj_join, (SELECT COUNT_BIG(DISTINCT s) FROM j2) AS final_stores,\n"
                "       (SELECT COUNT_BIG(DISTINCT s) FROM j1 WHERE NOT EXISTS (SELECT 1 FROM pr WHERE pr.s = j1.s)) AS stores_no_projection,\n"
                "       (SELECT COUNT_BIG(*) FROM (SELECT s, geo FROM pr GROUP BY s, geo HAVING COUNT_BIG(*) > 1) x) AS proj_dup_store_geo_pairs")
    raise ValueError(f"Unknown template: {kind}")


def expand_maps(sql, cfg, user, password, max_rows=20000):
    """Replace {{MDM_MAP}} with the MDM (UPC, Product) rows for the UPCs in the shelf audit, as inline VALUES blocks, so the join
    runs on the SA server although MDM is on another one. Read-only: nothing is written anywhere. MDM duplicates are kept
    (the join fans out exactly as a same-server join would)."""
    if MDM_TOKEN not in str(sql or ""):
        return sql, {}
    x = _ild_parts(cfg)
    S, M = x["S"], x["M"]

    def fetch(o, query):
        c = connect(o["server"], o["database"], user, password)
        try:
            c.timeout = 1800
            cur = c.cursor()
            try:
                cur.execute(query)
                return cur.fetchall()
            finally:
                cur.close()
        finally:
            c.close()
    sa_upc = {pkey(r[0]) for r in fetch(S, f"SELECT DISTINCT {q(S['upc_col'])} FROM {fq(S)}{x['sa_w']}") if r[0] is not None}
    mdm = fetch(M, f"SELECT {q(M['upc_col'])}, {q(M['product_col'])} FROM {fq(M)} WHERE {q(M['upc_col'])} IS NOT NULL")
    rows = [(pkey(r[0]), r[1]) for r in mdm if pkey(r[0]) in sa_upc]
    if len(rows) > max_rows:
        raise ValueError(f"{len(rows):,} MDM rows match the shelf audit UPCs; the limit is {max_rows:,}. Filter the shelf audit (delivery ID) first.")
    digits = all(k.isdigit() for k, _ in rows)
    key = (lambda k: str(int(k))) if digits else _lit
    kt = "bigint" if digits else "varchar(100)"
    plit = lambda p: "NULL" if p is None else "N" + _lit(p)
    blocks = [f"SELECT CAST(u AS {kt}) AS u, CAST(p AS nvarchar(4000)) AS product FROM (VALUES "
              + ",".join(f"({key(k)},{plit(p)})" for k, p in rows[i:i + 1000]) + ") AS v(u, p)" for i in range(0, len(rows), 1000)]
    body = "\nUNION ALL\n".join(blocks) or f"SELECT CAST(NULL AS {kt}) AS u, CAST(NULL AS nvarchar(4000)) AS product WHERE 1 = 0"
    return str(sql).replace(MDM_TOKEN, body), {"server": M.get("server"), "sa_upcs": len(sa_upc), "mdm_rows": len(rows),
                                               "unmapped_upcs": len(sa_upc - {k for k, _ in rows})}


# ---------------- SQL IDE (read-only) ----------------
BLOCKED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|DENY|INTO|OPENROWSET|OPENQUERY|"
    r"OPENDATASOURCE|BULK|BACKUP|RESTORE|SHUTDOWN|WAITFOR|KILL|DBCC|USE|SP_\w+|XP_\w+)\b", re.I)
_TOKENS = re.compile(r"'(?:[^']|'')*'|\[[^\]]*\]|\"[^\"]*\"|--[^\n]*|/\*.*?\*/", re.S)


def check_readonly(sql):
    """One SELECT/WITH statement only. Comments, literals and quoted identifiers are stripped in a single pass
    (so '/*' inside a string cannot hide code). A guard, not a security boundary: use a read-only SQL login."""
    s = _TOKENS.sub(lambda m: "''" if m.group(0)[0] == "'" else " ", str(sql or "")).strip().rstrip(";").strip()
    if not s:
        raise ValueError("Enter a query.")
    if ";" in s:
        raise ValueError("One statement only.")
    if not re.match(r"(SELECT|WITH)\b", s, re.I):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    m = BLOCKED_SQL.search(s)
    if m:
        raise ValueError(f"Not allowed in read-only mode: {m.group(0).upper()}")
    return str(sql).strip().rstrip(";")


def run_query(server, database, user, password, sql, max_rows=1000, timeout=60):
    """Run one guarded query; returns {columns, rows, truncated, seconds}. Caps rows at max_rows (+1 fetched to detect more)."""
    sql, t0 = check_readonly(sql), time.time()
    c = connect(server, database, user, password)
    try:
        c.timeout = int(timeout)  # query timeout, seconds
        cur = c.cursor()
        cur.execute(sql)
        if cur.description is None:
            raise ValueError("Query returned no result set.")
        seen, cols = {}, []
        for d in cur.description:
            n = d[0] or "col"
            seen[n] = seen.get(n, 0) + 1
            cols.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
        rows = cur.fetchmany(int(max_rows) + 1)
        return {"columns": cols, "rows": [[clean(v) for v in r] for r in rows[:int(max_rows)]],
                "truncated": len(rows) > int(max_rows), "seconds": round(time.time() - t0, 2)}
    finally:
        c.close()


# ---------------- runner ----------------
class LiveRunner:
    def __init__(self, user, password, catalog, on_check=None):
        self.user, self.password = user, password
        self.rules = catalog.get("rules", {})
        self.on_check = on_check or (lambda c: None)
        self.results, self.prof, self.keys, self._conns = [], {}, {}, {}
        self.cfg, self._sample, self._sas, self._dval = {}, None, None, None
        self.off = set()  # rules the user switched off for this run (Live input setup → Check readiness)

    def rule(self, r):
        return self.rules.get(r, {})

    def on(self, r):
        return self.rule(r).get("enabled", True) is not False and r not in self.off

    def thr(self, r, k, d=0.0):
        return float(self.rule(r).get(k, d))

    def _conn(self, src):
        k = (src["server"], src["database"])
        if k not in self._conns:
            c = connect(src["server"], src["database"], self.user, self.password)
            c.timeout = 1800  # query timeout, seconds
            self._conns[k] = c
        return self._conns[k]

    def rows(self, src, sql, params=(), clean_vals=True):
        cur = self._conn(src).cursor()
        try:
            cur.execute(sql, *params)
            cols = [d[0] for d in cur.description]
            f = clean if clean_vals else (lambda v: v)
            return [{k: f(v) for k, v in zip(cols, r)} for r in cur.fetchall()]
        finally:
            cur.close()

    def one(self, src, sql, params=()):
        r = self.rows(src, sql, params)
        return r[0] if r else {}

    def add(self, stage, rule, sub, status, msg, pct_=0.0, actual=None, expected=None, sample=None):
        cid = f"LIVE_{stage}_{_id(rule)}" + (f"_{_id(sub)}" if sub else "")
        c = {"check_id": cid, "rule": rule, "stage": stage, "source": "LIVE", "status": status,
             "passed": status == "PASS", "severity": self.rule(rule).get("severity", "INFO"),
             "message": msg, "error_message": msg, "affected_pct": float(pct_),
             "actual_value": actual, "expected_value": expected, "sample_failing_rows": sample or [],
             "logged_at": dt.datetime.now().isoformat(timespec="seconds")}
        self.results.append(c)
        self.on_check(c)

    def safe(self, stage, rule, fn, sub="", force=False):
        if not force and not self.on(rule):
            if rule in self.off and not any(c["rule"] == rule and "switched off" in c["message"] for c in self.results):
                self.add(stage, rule, "", "MANUAL_REQUIRED", f"{rule} not run: switched off for this run")
            return
        try:
            fn()
        except Missing as e:  # unmapped input: MANUAL_REQUIRED at the rule's severity
            self.add(stage, rule, sub, "MANUAL_REQUIRED", f"{rule} not run: {e}")
        except Exception as e:  # SQL/driver error: BLOCKED under its own rule ID, never a crash
            self.add(stage, rule, sub, "BLOCKED", f"{rule} did not run: {e}")

    # ---- per-source checks ----
    def source(self, st, src, cfg):
        t = qt(src["table"])
        P = self.prof.setdefault(st, {"table": src["table"], "rows": 0})

        def c_rows():
            n = int(num(self.one(src, f"SELECT COUNT_BIG(*) AS n FROM {t}").get("n")))
            P["rows"] = n
            self.add(st, "IN-001", "", "PASS" if n else "FAIL", f"{n:,} rows" if n else "Table has no rows", 0 if n else 100, n)
        self.safe(st, "IN-001", c_rows)
        rows = P["rows"]

        for role, kind in (("store_col", "store"), ("upc_col", "upc")):
            col = src.get(role)
            if not col:
                continue

            def c_keys(col=col, kind=kind):
                raw = {keystr(r["k"]) for r in self.rows(
                    src, f"SELECT DISTINCT {q(col)} AS k FROM {t} WHERE {q(col)} IS NOT NULL", clean_vals=False)}
                nrm = {norm(k) for k in raw}
                self.keys[f"{st}.{kind}"] = nrm
                P[kind + "s"] = len(raw)
                if self.on("IN-004"):
                    self.add(st, "IN-004", kind, "PASS",
                             f"{len(raw):,} distinct {kind} values ({len(nrm):,} after trimming leading zeros)", 0, len(raw))
            self.safe(st, "IN-004", c_keys, kind, force=True)  # keys feed XS / cross-checks even when IN-004 is off

        g = [x for x in (src.get("grain") or []) if x]
        keys = list(dict.fromkeys(k for k in [src.get("store_col"), src.get("upc_col"), *g] if k))
        if keys:
            def c_nulls():
                sel = ", ".join(f"SUM(CASE WHEN {q(k)} IS NULL THEN 1 ELSE 0 END) AS n{i}" for i, k in enumerate(keys))
                r = self.one(src, f"SELECT {sel} FROM {t}")
                for i, k in enumerate(keys):
                    n = int(num(r.get(f"n{i}")))
                    self.add(st, "IN-002", k, "FAIL" if n else "PASS", f"{k}: {n:,} null rows", pct(n, rows), n, 0)
            self.safe(st, "IN-002", c_nulls)

        if not g:
            if self.on("IN-003"):
                self.add(st, "IN-003", "", "MANUAL_REQUIRED", "Grain not declared. Select the columns that must be unique together.")
        else:
            def c_dup():
                gl = ", ".join(q(x) for x in g)
                r = self.one(src, f"SELECT COUNT_BIG(*) AS k, ISNULL(SUM(c), 0) AS n FROM "
                                  f"(SELECT COUNT_BIG(*) AS c FROM {t} GROUP BY {gl} HAVING COUNT_BIG(*) > 1) x")
                dk, dn = int(num(r.get("k"))), int(num(r.get("n")))
                smp = self.rows(src, f"SELECT TOP 10 {gl}, COUNT_BIG(*) AS dup_count FROM {t} GROUP BY {gl} "
                                     f"HAVING COUNT_BIG(*) > 1 ORDER BY dup_count DESC") if dk else []
                self.add(st, "IN-003", "", "FAIL" if dk else "PASS",
                         f"{dk:,} duplicate keys ({dn:,} rows) at grain [{', '.join(g)}]", pct(dn, rows), dk, 0, smp)
            self.safe(st, "IN-003", c_dup)

        m = [x for x in (src.get("measures") or []) if x]
        if m:
            def c_meas():
                sel = ", ".join(f"SUM(CASE WHEN {q(c)} IS NULL THEN 1 ELSE 0 END) AS z{i}, "
                                f"SUM(CASE WHEN {q(c)} < 0 THEN 1 ELSE 0 END) AS g{i}" for i, c in enumerate(m))
                r = self.one(src, f"SELECT {sel} FROM {t}")
                mx = self.thr("IN-007", "max_pct")
                for i, c in enumerate(m):
                    z, neg = int(num(r.get(f"z{i}"))), int(num(r.get(f"g{i}")))
                    p = pct(z, rows)
                    if self.on("IN-007"):
                        self.add(st, "IN-007", c, "FAIL" if p > mx else "PASS", f"{c}: {z:,} null rows ({p}%)", p, z)
                    if self.on("IN-005"):
                        self.add(st, "IN-005", c, "FAIL" if neg else "PASS", f"{c}: {neg:,} negative values", pct(neg, rows), neg, 0)
            if self.on("IN-007") or self.on("IN-005"):
                self.safe(st, "IN-005", c_meas, force=True)

        if st == "SA":
            exp = str(cfg.get("expected_sa_stores") or "").replace(",", "").strip()
            if exp and P.get("stores") is not None:
                def c_sample():
                    e, a, tol = float(exp), float(P["stores"]), self.thr("SA-005", "tol_pct", 5)
                    dev = pct(abs(a - e), e)
                    self.add("SA", "SA-005", "", "FAIL" if dev > tol else "PASS",
                             f"SA stores {a:,.0f} vs expected {e:,.0f} ({dev}% absolute deviation, tol {tol:g}%)", dev, a, e)
                self.safe("SA", "SA-005", c_sample)

        if st == "PROJ" and src.get("weight_col"):
            def c_wgt():
                w, cw = q(src["weight_col"]), f"TRY_CAST({q(src['weight_col'])} AS float)"
                r = self.one(src, f"SELECT SUM(CASE WHEN {w} IS NULL THEN 1 ELSE 0 END) AS z, "
                                  f"SUM(CASE WHEN {w} IS NOT NULL AND {cw} IS NULL THEN 1 ELSE 0 END) AS nn, "
                                  f"SUM(CASE WHEN {cw} <= 0 THEN 1 ELSE 0 END) AS np FROM {t}")
                z, nn, npos = int(num(r.get("z"))), int(num(r.get("nn"))), int(num(r.get("np")))
                bad = z + nn + npos
                msg = f"{z:,} null, {nn:,} non-numeric, and {npos:,} zero/negative weights"
                self.add("PROJ", "PRJ-001", "", "FAIL" if bad else "PASS", msg, pct(bad, rows), bad, 0)
            self.safe("PROJ", "PRJ-001", c_wgt)

    # ---- imputed stores (Audit_Delivery_Store_Sample) ----
    def imputation(self, cfg):
        srcs = cfg.get("sources") or {}
        sa, im = srcs.get("SA") or {}, srcs.get("IMP") or {}
        if not im.get("table"):
            return
        man = lambda m: self.on("IMP-001") and self.add("SA", "IMP-001", "", "MANUAL_REQUIRED", m)
        if not sa.get("table") or not sa.get("store_col"):
            return man("Imputed store sample needs the SA table and SA store column mapped.")
        if not im.get("store_col") or not im.get("status_col") or not im.get("delivery_col"):
            return man("Map the store, delivery ID and imputed status columns of the imputed store sample.")
        if im["server"] != sa["server"]:
            return man("Imputed store sample must be on the same server as SA (it is joined in SQL).")
        dsa, dval = sa.get("delivery_col"), str(im.get("delivery_value") or cfg.get("expected_delivery_id") or "").strip()
        if not dsa and not dval:
            return man("SA has no delivery ID column mapped. Enter the expected delivery ID.")

        t, sc = qt(sa["table"]), q(sa["store_col"])
        parts = str(im["table"]).split(".")
        fq = qt(im["table"] if len(parts) == 3 else f"{im['database']}." + (im["table"] if len(parts) == 2 else f"dbo.{im['table']}"))
        where, base = f"CAST({q(im['status_col'])} AS varchar(20)) = ?", [str(im.get("imputed_value") or "Y")]
        if dsa:  # join on delivery ID + store
            sel_d, join_d = f", {kx(q(im['delivery_col']))} AS d_k", f" AND i.d_k = {kx('a.' + q(dsa))}"
        else:    # SA has no delivery column: filter the sample to one delivery
            where += f" AND CAST({q(im['delivery_col'])} AS varchar(100)) = ?"
            base.append(dval)
            sel_d, join_d = "", ""
        cte = f"WITH imp AS (SELECT DISTINCT {kx(q(im['store_col']))} AS s_k{sel_d} FROM {fq} WHERE {where})"
        ak = kx("a." + sc)
        how = f"joined on delivery ID column {dsa} + store" if dsa else f"delivery ID {dval} + store"
        P = self.prof.setdefault("SA", {"table": sa["table"], "rows": 0})

        def c_share():
            r = self.one(sa, f"{cte} SELECT COUNT(DISTINCT CASE WHEN i.s_k IS NOT NULL THEN {ak} END) AS i, COUNT(DISTINCT {ak}) AS n "
                             f"FROM {t} a LEFT JOIN imp i ON i.s_k = {ak}{join_d}", base)
            i, n = int(num(r.get("i"))), int(num(r.get("n")))
            p, mx = pct(i, n), self.thr("IMP-001", "max_pct", 20)
            P["imputed_stores"] = i
            self.add("SA", "IMP-001", "", "FAIL" if p > mx else "PASS",
                     f"{i:,} of {n:,} SA stores are imputed ({p}%, max {mx:g}%); {how}", p, i, None)
        self.safe("SA", "IMP-001", c_share)

        def c_orphan():
            ex = f"FROM imp i WHERE NOT EXISTS (SELECT 1 FROM {t} a WHERE {ak} = i.s_k{join_d})"
            r = self.one(sa, f"{cte} SELECT COUNT(*) AS n, (SELECT COUNT(*) FROM imp) AS tot {ex}", base)
            n, tot = int(num(r.get("n"))), int(num(r.get("tot")))
            smp = self.rows(sa, f"{cte} SELECT TOP 10 i.s_k AS imputed_store {ex} ORDER BY i.s_k", base) if n else []
            p = pct(n, tot)
            self.add("SA", "IMP-002", "", "FAIL" if p > self.thr("IMP-002", "max_pct") else "PASS",
                     f"{n:,} of {tot:,} imputed stores in the sample are not in the SA data ({p}%)", p, n, 0, smp)
        self.safe("SA", "IMP-002", c_orphan)

        uc = sa.get("upc_col")
        ms = [x for x in (sa.get("measures") or []) if x]
        if not uc or not ms:
            if self.on("IMP-003"):
                self.add("SA", "IMP-003", "", "MANUAL_REQUIRED", "Map the SA UPC column and measures (shelf feet, facings, capacity) to test imputed stores for overstatement.")
            return
        for m in ms:
            def c_over(m=m):
                ratio, nmin, mx, mc = self.thr("IMP-003", "ratio", 2), self.thr("IMP-003", "min_base_stores", 5), self.thr("IMP-003", "max_pct", 1), q(m)
                body = (f"{cte}, x AS (SELECT {ak} AS store_k, a.{q(uc)} AS upc, CAST(a.{mc} AS float) AS v, "
                        f"CASE WHEN EXISTS (SELECT 1 FROM imp i WHERE i.s_k = {ak}{join_d}) THEN 1 ELSE 0 END AS is_imp "
                        f"FROM {t} a WHERE a.{mc} IS NOT NULL), "
                        f"b AS (SELECT upc, AVG(v) AS mu FROM x WHERE is_imp = 0 GROUP BY upc HAVING COUNT(DISTINCT store_k) >= ?), "
                        f"f AS (SELECT x.store_k, x.upc, x.v, b.mu, x.v / NULLIF(b.mu, 0) AS ratio FROM x JOIN b ON b.upc = x.upc WHERE x.is_imp = 1)")
                prm = base + [int(nmin)]
                r = self.one(sa, f"{body} SELECT COUNT(*) AS n, SUM(CASE WHEN ratio > ? THEN 1 ELSE 0 END) AS k, AVG(v) AS iv, AVG(mu) AS bv FROM f", prm + [ratio])
                n, k = int(num(r.get("n"))), int(num(r.get("k")))
                iv, bv = r.get("iv"), r.get("bv")
                lift = (iv / bv) if iv is not None and bv else None
                smp = self.rows(sa, f"{body} SELECT TOP 10 store_k AS store, upc, ROUND(v, 2) AS imputed_value, ROUND(mu, 2) AS non_imputed_upc_avg, "
                                    f"ROUND(ratio, 2) AS ratio FROM f WHERE ratio > ? ORDER BY ratio DESC", prm + [ratio]) if k else []
                p = pct(k, n)
                msg = (f"{m}: {k:,} of {n:,} imputed store x UPC rows exceed {ratio:g}x the non-imputed average for the same UPC ({p}%, max {mx:g}%)"
                       + (f"; imputed rows average {lift:.2f}x non-imputed" if lift else "")
                       if n else f"{m}: no imputed rows with a comparable UPC baseline (min {nmin:g} non-imputed stores per UPC)")
                self.add("SA", "IMP-003", m, "FAIL" if n and p > mx else "PASS", msg, p, k, 0, smp)
            self.safe("SA", "IMP-003", c_over, m)

    # ================= spec checks (SA001-SA015, MDM001-002, PRJ001-005, POS001-005) =================
    def setup_custom(self, items):
        """Register user-added checks so severity and description flow through add() and rule_descriptions."""
        self.base_ids = set(self.rules)
        self.rules = {**self.rules, **{c["id"]: {"stage": c["source"], "severity": c["severity"], "enabled": True, "custom": True,
                                                 "description": c.get("description") or f"Custom: {c['test']} on {c['column'] or c['source']}"}
                                       for c in items if c["id"] not in self.base_ids}}

    def custom_checks(self, items):
        taken = set()
        for c in items:
            stage = c["source"] if c["source"] in core.CUSTOM_SRC else "SA"
            if not c["run"]:
                self.add(stage, c["id"], "", "MANUAL_REQUIRED", f"{c['id']} not run: switched off for this run")
            else:
                self.safe(stage, c["id"], lambda c=c, t=set(taken): self.custom_one(c, t), force=True)
            taken.add(c["id"])

    def custom_one(self, c, taken):
        p = core.custom_problem(c, self.cfg, self.base_ids, taken)
        if p:
            raise Missing(p)
        rid, s, t, o = c["id"], c["source"], c["test"], self.src(c["source"])
        tb, cols = self.t(s), [x.strip() for x in c["column"].split(",") if x.strip()]
        if t == "Custom SQL":
            cur = self._conn(o).cursor()
            try:
                cur.execute(check_readonly(c["value"]))
                if cur.description is None:
                    raise ValueError("Query returned no result set.")
                names, got = [d[0] or "col" for d in cur.description], cur.fetchmany(CUSTOM_MAX + 1)
            finally:
                cur.close()
            k, rows = min(len(got), CUSTOM_MAX), self.prof.get(s, {}).get("rows") or 0
            smp = [{n: clean(v) for n, v in zip(names, r)} for r in got[:10]]
            return self.add(s, rid, "", "FAIL" if k else "PASS", f"{k:,}{'+' if len(got) > CUSTOM_MAX else ''} failing rows returned by the custom query",
                            pct(k, rows), k, 0, smp)
        if t == "Unique":
            nn = " AND ".join(f"{q(x)} IS NOT NULL" for x in cols)
            gl = ", ".join(q(x) for x in cols)
            r = self.one(o, f"SELECT COUNT_BIG(*) AS k, ISNULL(SUM(n), 0) AS n, (SELECT COUNT_BIG(*) FROM {tb}) AS tot FROM "
                            f"(SELECT COUNT_BIG(*) AS n FROM {tb} WHERE {nn} GROUP BY {gl} HAVING COUNT_BIG(*) > 1) x")
            k, n, tot = int(num(r.get("k"))), int(num(r.get("n"))), int(num(r.get("tot")))
            smp = self.rows(o, f"SELECT TOP 10 {gl}, COUNT_BIG(*) AS dup_count FROM {tb} WHERE {nn} GROUP BY {gl} "
                               f"HAVING COUNT_BIG(*) > 1 ORDER BY dup_count DESC") if k else []
            return self.add(s, rid, "", "FAIL" if k else "PASS", f"[{', '.join(cols)}]: {k:,} duplicate keys ({n:,} rows of {tot:,}); null keys ignored",
                            pct(n, tot), k, 0, smp)
        col, prm = q(cols[0]), []
        if t == "Not null":
            bad, what = f"{col} IS NULL", "null"
        elif t == "Allowed values":
            vals = [v.strip() for v in c["value"].split(",") if v.strip()]
            bad, prm, what = (f"{col} IS NOT NULL AND LTRIM(RTRIM(CAST({col} AS varchar(200)))) NOT IN ({', '.join('?' * len(vals))})", vals,
                              f"not in [{', '.join(vals)}]")
        else:
            lo, hi = core.custom_range(c["value"])
            bad = f"{col} IS NOT NULL AND (TRY_CAST({col} AS float) IS NULL" + (" OR TRY_CAST(%s AS float) < ?" % col if lo is not None else "") \
                  + (" OR TRY_CAST(%s AS float) > ?" % col if hi is not None else "") + ")"
            prm = [x for x in (lo, hi) if x is not None]
            what = f"non-numeric or outside {'' if lo is None else format(lo, 'g')}..{'' if hi is None else format(hi, 'g')}"
        r = self.one(o, f"SELECT SUM(CASE WHEN {bad} THEN 1 ELSE 0 END) AS k, COUNT_BIG(*) AS tot FROM {tb}", prm)
        k, tot = int(num(r.get("k"))), int(num(r.get("tot")))
        smp = self.rows(o, f"SELECT TOP 10 * FROM {tb} WHERE {bad}", prm) if k else []
        self.add(s, rid, "", "FAIL" if k else "PASS", f"{cols[0]}: {k:,} of {tot:,} rows {what} ({pct(k, tot)}%)", pct(k, tot), k, 0, smp)

    def src(self, s):
        return (self.cfg.get("sources") or {}).get(s) or {}

    def t(self, s):
        return qt(self.src(s)["table"])

    def need_rule(self, rule):
        miss = core.missing_for(rule, {**self.cfg, "expected_delivery_id": "x", "expected_task": "x", "expected_weeks": "x"})
        if miss:
            raise Missing("set " + "; ".join(miss))

    def idc(self, s):
        o = self.src(s)
        return "".join(f"{q(o[r])}, " for r in ("store_col", "upc_col") if o.get(r))

    def list_check(self, stage, rule, s, role, label, expected):
        """expected=None: pure listing (PASS). '' : list for manual review. value(s): compare."""
        self.need_rule(rule)
        o, c = self.src(s), q(self.src(s)[role])
        r = self.one(o, f"SELECT COUNT_BIG(DISTINCT {c}) AS n, SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS z, COUNT_BIG(*) AS tot FROM {self.t(s)}")
        n, z, tot = int(num(r.get("n"))), int(num(r.get("z"))), int(num(r.get("tot")))
        vals = self.rows(o, f"SELECT TOP {LIST_MAX} {c} AS v, COUNT_BIG(*) AS row_count FROM {self.t(s)} GROUP BY {c} ORDER BY {c}", clean_vals=False)
        smp = [{o[role]: clean(v["v"]), "row_count": int(v["row_count"])} for v in vals]
        nn = [v for v in vals if v["v"] is not None]
        txt = ", ".join(keystr(v["v"]) for v in nn[:15]) + (" …" if n > 15 else "")
        nul = f"; {z:,} null rows" if z else ""
        if expected is None:
            return self.add(stage, rule, "", "PASS", f"{n:,} distinct {label}: {txt}{nul}", 0, n, None, smp)
        exp = [e.strip() for e in str(expected).split(",") if e.strip()]
        if not exp:
            return self.add(stage, rule, "", "MANUAL_REQUIRED",
                            f"{n:,} distinct {label}: {txt}{nul}. No expected value entered; confirm against the requirement.", 0, n, None, smp)
        want = {pkey(e) for e in exp}
        bad_rows = sum(int(v["row_count"]) for v in nn if pkey(v["v"]) not in want) + z
        extra = sorted({keystr(v["v"]) for v in nn if pkey(v["v"]) not in want})
        miss = sorted(e for e in exp if pkey(e) not in {pkey(v["v"]) for v in nn})
        more = n > LIST_MAX
        ok = not (extra or miss or z or more)
        msg = (f"{n:,} distinct {label} ({txt}) vs expected {', '.join(exp)}"
               + (f"; unexpected: {', '.join(extra[:10])}" if extra else "") + (f"; expected but absent: {', '.join(miss)}" if miss else "")
               + (f"; more than {LIST_MAX} values, not all compared" if more else "") + nul)
        self.add(stage, rule, "", "PASS" if ok else "FAIL", msg, pct(bad_rows, tot), n, ", ".join(exp), smp)

    def dcount(self, s, role):
        c = q(self.src(s)[role])
        r = self.one(self.src(s), f"SELECT COUNT_BIG(DISTINCT {c}) AS n, COUNT_BIG({c}) AS nn, COUNT_BIG(*) AS tot FROM {self.t(s)}")
        return int(num(r.get("n"))), int(num(r.get("nn"))), int(num(r.get("tot")))

    def count_check(self, stage, rule, s, role, kind):
        self.need_rule(rule)
        n, nn, tot = self.dcount(s, role)
        k = self.keys.get(f"{s}.{kind}")
        trim = f" ({len(k):,} after trimming leading zeros)" if k is not None and len(k) != n else ""
        self.add(stage, rule, "", "PASS", f"{n:,} distinct {self.src(s)[role]}{trim}; {tot - nn:,} null rows of {tot:,}", 0, n)

    def not_null(self, stage, rule, s, role, cond=None, label=""):
        self.need_rule(rule)
        o, c = self.src(s), q(self.src(s)[role])
        cond = cond() if cond else ""
        base = f"{cond} AND " if cond else ""
        r = self.one(o, f"SELECT SUM(CASE WHEN {base}{c} IS NULL THEN 1 ELSE 0 END) AS k, "
                        f"SUM(CASE WHEN {cond or '1=1'} THEN 1 ELSE 0 END) AS n FROM {self.t(s)}")
        k, n = int(num(r.get("k"))), int(num(r.get("n")))
        smp = self.rows(o, f"SELECT TOP 10 * FROM {self.t(s)} WHERE {base}{c} IS NULL") if k else []
        self.add(stage, rule, "", "FAIL" if k else "PASS", f"{o[role]} null on {k:,} of {n:,} rows{label} ({pct(k, n)}%)", pct(k, n), k, 0, smp)

    def sa003(self):
        self.need_rule("SA003")
        o = self.src("SA")
        cols = [o[r] for r in ("delete_tstamp_col", "status_cd_col", "dropped_reason_col")]
        anyc = " OR ".join(f"{q(c)} IS NOT NULL" for c in cols)
        sel = ", ".join(f"SUM(CASE WHEN {q(c)} IS NOT NULL THEN 1 ELSE 0 END) AS n{i}" for i, c in enumerate(cols))
        r = self.one(o, f"SELECT {sel}, SUM(CASE WHEN {anyc} THEN 1 ELSE 0 END) AS k, COUNT_BIG(*) AS tot FROM {self.t('SA')}")
        k, tot = int(num(r.get("k"))), int(num(r.get("tot")))
        per = "; ".join(f"{c}: {int(num(r.get(f'n{i}'))):,}" for i, c in enumerate(cols))
        smp = self.rows(o, f"SELECT TOP 10 {self.idc('SA')}{', '.join(q(c) for c in cols)} FROM {self.t('SA')} WHERE {anyc}") if k else []
        self.add("SA", "SA003", "", "FAIL" if k else "PASS", f"{k:,} of {tot:,} rows have a non-null value ({per})", pct(k, tot), k, 0, smp)

    def sa005(self):
        self.need_rule("SA005")
        o, v = self.src("SA"), self.src("SHELFVAR")
        locs = self.rows(o, f"SELECT {q(o['location_col'])} AS loc, COUNT_BIG(*) AS n FROM {self.t('SA')} GROUP BY {q(o['location_col'])}", clean_vals=False)
        ref = {}
        for r in self.rows(v, f"SELECT DISTINCT {q(v['id_col'])} AS id, {q(v['desc_col'])} AS d FROM {self.t('SHELFVAR')}", clean_vals=False):
            if r["id"] is not None:
                ref.setdefault(pkey(r["id"]), set()).add(str(r["d"]).strip() if r["d"] is not None else None)
        tot = sum(int(r["n"]) for r in locs)
        un, amb, smp = [], [], []
        for r in locs:
            k = pkey(r["loc"]) if r["loc"] is not None else None
            d = ref.get(k) if k is not None else None
            row = {"location": clean(r["loc"]), "description": None, "sa_rows": int(r["n"])}
            if not d or d == {None}:
                un.append(row)
            elif len(d) > 1:
                row["description"] = " | ".join(sorted(x for x in d if x))
                amb.append(row)
            else:
                row["description"] = next(iter(d))
                smp.append(row)
        bad = sum(r["sa_rows"] for r in un + amb)
        msg = (f"{len(locs):,} distinct SA locations: {len(smp):,} resolve to one description, {len(un):,} not found or blank in "
               f"shelf_var_master ({sum(r['sa_rows'] for r in un):,} rows), {len(amb):,} with more than one description "
               f"(UPDATE would fan out); read-only, IDs compared after trimming leading zeros")
        self.add("SA", "SA005", "", "FAIL" if un or amb else "PASS", msg, pct(bad, tot), len(un) + len(amb), 0, (un + amb + smp)[:50])

    def sa006_grain(self):
        self.need_rule("SA006")
        o, g = self.src("SA"), [x for x in self.src("SA")["grain"] if x]
        gl = ", ".join(q(x) for x in g)
        r = self.one(o, f"SELECT COUNT_BIG(*) AS k, ISNULL(SUM(c), 0) AS n, (SELECT COUNT_BIG(*) FROM {self.t('SA')}) AS tot FROM "
                        f"(SELECT COUNT_BIG(*) AS c FROM {self.t('SA')} GROUP BY {gl} HAVING COUNT_BIG(*) > 1) x")
        dk, dn, tot = int(num(r.get("k"))), int(num(r.get("n"))), int(num(r.get("tot")))
        smp = self.rows(o, f"SELECT TOP 10 {gl}, COUNT_BIG(*) AS dup_count FROM {self.t('SA')} GROUP BY {gl} "
                           f"HAVING COUNT_BIG(*) > 1 ORDER BY dup_count DESC") if dk else []
        self.add("SA", "SA006", "grain", "FAIL" if dk else "PASS", f"Grain [{', '.join(g)}]: {dk:,} duplicate keys ({dn:,} rows of {tot:,})",
                 pct(dn, tot), dk, 0, smp)

    def sa006_row(self):
        o = self.src("SA")
        if not o.get("table"):
            raise Missing("set Shelf audit (SA) table")
        cols = list_columns(o["server"], o["database"], o["table"], self.user, self.password)
        use = [c for c, tp in cols if str(tp).lower() not in NO_GROUP]
        skip = [c for c, tp in cols if str(tp).lower() in NO_GROUP]
        gl = ", ".join(q(c) for c in use)
        r = self.one(o, f"SELECT COUNT_BIG(*) AS k, ISNULL(SUM(c), 0) AS n, (SELECT COUNT_BIG(*) FROM {self.t('SA')}) AS tot FROM "
                        f"(SELECT COUNT_BIG(*) AS c FROM {self.t('SA')} GROUP BY {gl} HAVING COUNT_BIG(*) > 1) x")
        dk, dn, tot = int(num(r.get("k"))), int(num(r.get("n"))), int(num(r.get("tot")))
        show = ", ".join(q(c) for c in use[:10])
        smp = self.rows(o, f"SELECT TOP 10 {show}, COUNT_BIG(*) AS dup_count FROM {self.t('SA')} GROUP BY {gl} "
                           f"HAVING COUNT_BIG(*) > 1 ORDER BY dup_count DESC") if dk else []
        self.add("SA", "SA006", "row", "FAIL" if dk else "PASS",
                 f"Full row ({len(use)} columns): {dk:,} duplicated rows ({dn:,} rows of {tot:,})"
                 + (f"; not comparable, excluded: {', '.join(skip)}" if skip else ""), pct(dn, tot), dk, 0, smp)

    def delivery(self):
        if self._dval is not None:
            return self._dval
        e = str(self.cfg.get("expected_delivery_id") or "").strip()
        if e:
            self._dval = e
            return e
        o = self.src("SA")
        if o.get("table") and o.get("delivery_col"):
            d = q(o["delivery_col"])
            vals = self.rows(o, f"SELECT DISTINCT TOP 2 {d} AS v FROM {self.t('SA')} WHERE {d} IS NOT NULL", clean_vals=False)
            if len(vals) == 1:
                self._dval = keystr(vals[0]["v"])
                return self._dval
            raise Missing(f"enter Expected delivery ID (SA has {'no' if not vals else 'more than one'} delivery ID)")
        raise Missing("enter Expected delivery ID or map the SA delivery ID column")

    def sample(self):
        if self._sample is None:
            im, d = self.src("IMP"), self.delivery()
            rows = self.rows(im, f"SELECT DISTINCT {kx(q(im['store_col']))} AS s, CAST({q(im['status_col'])} AS varchar(20)) AS st "
                                 f"FROM {self.t('IMP')} WHERE {kx(q(im['delivery_col']))} = ?", [pkey(d)], clean_vals=False)
            iv = str(im.get("imputed_value") or "Y").strip().upper()
            C, I, O = set(), set(), set()
            for r in rows:
                if r["s"] is None:
                    continue
                k = norm(keystr(r["s"]))
                (C if r["st"] is None else I if r["st"].strip().upper() == iv else O).add(k)
            self._sample = (C, I, O, d, iv)
        return self._sample

    def sa_stores(self):
        if self._sas is None:
            o, d = self.src("SA"), self.delivery()
            w, p = (f" WHERE {kx(q(o['delivery_col']))} = ?", [pkey(d)]) if o.get("delivery_col") else ("", [])
            rows = self.rows(o, f"SELECT DISTINCT {kx(q(o['store_col']))} AS s FROM {self.t('SA')}{w}", p, clean_vals=False)
            self._sas = ({norm(keystr(r["s"])) for r in rows if r["s"] is not None}, bool(w))
        return self._sas

    def sa007(self):
        self.need_rule("SA007")
        C, I, O, d, _ = self.sample()
        S, filt = self.sa_stores()
        non = S - I
        a, b = sorted(C - S), sorted(non - C)
        ok = bool(C) and len(non) == len(C) and not a and not b
        self.prof.setdefault("SA", {})["collected_stores"] = len(C)
        scope = f"delivery {d}" + ("" if filt else "; SA not filtered by delivery, no SA delivery column mapped")
        msg = (f"SA non-imputed stores {len(non):,} vs collected stores {len(C):,} ({scope}); {len(a):,} collected stores missing from SA; "
               f"{len(b):,} SA stores neither collected nor imputed" + ("; no collected stores in the sample for this delivery" if not C else ""))
        smp = [{"store": s, "issue": "collected, not in SA"} for s in a[:10]] + [{"store": s, "issue": "in SA, not in sample"} for s in b[:10]]
        self.add("SA", "SA007", "", "PASS" if ok else "FAIL", msg, pct(len(a) + len(b), max(len(C), 1)), len(non), len(C), smp)

    def sa008(self):
        self.need_rule("SA008")
        C, I, O, d, iv = self.sample()
        S, filt = self.sa_stores()
        U = C | I
        a, b, both = sorted(U - S), sorted(S - U), sorted(C & I)
        ok = bool(U) and not (a or b or both)
        self.prof.setdefault("SA", {})["imputed_stores"] = len(I)
        msg = (f"Sample for delivery {d}: {len(I):,} imputed ({iv}), {len(C):,} collected (null), {len(O):,} other status (excluded); "
               f"{len(a):,} sample stores missing from SA; {len(b):,} SA stores not in the sample; {len(both):,} stores both imputed and collected"
               + ("" if filt else "; SA not filtered by delivery") + ("; sample has no rows for this delivery" if not U else ""))
        smp = ([{"store": s, "issue": "in sample, not in SA"} for s in a[:10]] + [{"store": s, "issue": "in SA, not in sample"} for s in b[:10]]
               + [{"store": s, "issue": "imputed and collected"} for s in both[:10]])
        self.add("SA", "SA008", "", "PASS" if ok else "FAIL", msg, pct(len(a) + len(b) + len(both), max(len(U | S), 1)), len(U), len(S), smp)

    def sa009(self):
        self.need_rule("SA009")
        C, I, O, d, _ = self.sample()
        thr, base = self.thr("SA009", "max_increase_pct", 50), str(self.rule("SA009").get("base", "prior"))
        roles = ("shelf_feet_col", "facings_col", "capacity_col")

        def sums(s, by_delivery):
            o = self.src(s)
            w, p = (f" WHERE {kx(q(o['delivery_col']))} = ?", [pkey(d)]) if by_delivery and o.get("delivery_col") else ("", [])
            sel = ", ".join(f"SUM(CAST({q(o[r])} AS float)) AS m{i}" for i, r in enumerate(roles))
            rs = self.rows(o, f"SELECT {kx(q(o['store_col']))} AS s, {sel} FROM {self.t(s)}{w} GROUP BY {kx(q(o['store_col']))}", p, clean_vals=False)
            return {norm(keystr(r["s"])): r for r in rs if r["s"] is not None}
        cur, pri = sums("SA", True), sums("PRIOR", False)
        for i, r in enumerate(roles):
            col = self.src("SA")[r]
            if not I:
                self.add("SA", "SA009", col, "PASS", f"{col}: no imputed stores in the sample for delivery {d}", 0, 0, 0)
                continue
            flg, new, gone = [], 0, 0
            for s in sorted(I):
                c, p = (cur.get(s) or {}).get(f"m{i}"), (pri.get(s) or {}).get(f"m{i}")
                if c is None:
                    gone += 1
                    continue
                if p is None:
                    new += 1
                    continue
                c, p = float(c), float(p)
                den = p if base == "prior" else c
                ch = (c - p) / den * 100 if den else (float("inf") if c > p else 0.0)
                if ch > thr:
                    flg.append((ch, {"store": s, "prior_wave": round(p, 2), "current_wave": round(c, 2),
                                     "per_change": round(ch, 2) if ch != float("inf") else "prior 0"}))
            flg.sort(key=lambda x: -x[0])
            n_cmp = len(I) - new - gone
            msg = (f"{col}: {len(flg):,} of {n_cmp:,} imputed stores up more than {thr:g}% vs prior wave (store-level sum, change on {base}-wave base); "
                   f"{new:,} imputed stores not in prior wave (not assessed); {gone:,} imputed stores not in current SA")
            self.add("SA", "SA009", col, "FAIL" if flg else "PASS", msg, pct(len(flg), max(n_cmp, 1)), len(flg), 0, [x[1] for x in flg[:10]])

    def sa016(self):
        self.need_rule("SA016")
        o = self.src("SA")
        g, cat = list(dict.fromkeys(x for x in o["grain"] if x)), q(o["category_col"])
        sel = ", ".join(f"SUM(CASE WHEN {cat} IS NOT NULL AND {q(c)} IS NULL THEN 1 ELSE 0 END) AS n{i}" for i, c in enumerate(g))
        r = self.one(o, f"SELECT {sel}, SUM(CASE WHEN {cat} IS NOT NULL THEN 1 ELSE 0 END) AS tot FROM {self.t('SA')}")
        tot = int(num(r.get("tot")))
        for i, c in enumerate(g):
            k = int(num(r.get(f"n{i}")))
            smp = self.rows(o, f"SELECT TOP 10 {self.idc('SA')}{cat} AS category, {q(c)} FROM {self.t('SA')} "
                               f"WHERE {cat} IS NOT NULL AND {q(c)} IS NULL") if k else []
            self.add("SA", "SA016", c, "FAIL" if k else "PASS", f"{c} (grain) null on {k:,} of {tot:,} rows with a category ({pct(k, tot)}%)",
                     pct(k, tot), k, 0, smp)

    def calc(self, rule, target, inputs, expr, label):
        self.need_rule(rule)
        o, tol, dp = self.src("SA"), self.thr(rule, "tol", 0), self.rule(rule).get("round_dp")
        f = [f"CAST({q(o[r])} AS float)" for r in inputs]
        tg, cx = f"CAST({q(o[target])} AS float)", expr(f)
        if dp is not None:  # compare at the stored precision; decimal(38,10) strips float noise so 2.675 rounds to 2.68
            tg, cx = (f"ROUND(CAST({x} AS decimal(38,10)), {int(dp)})" for x in (tg, cx))
        nn = " AND ".join(f"{q(o[r])} IS NOT NULL" for r in (target, *inputs))
        cond = f"ABS({tg} - ({cx})) > ?"
        r = self.one(o, f"SELECT SUM(CASE WHEN {nn} THEN 1 ELSE 0 END) AS ev, SUM(CASE WHEN {nn} AND {cond} THEN 1 ELSE 0 END) AS k, "
                        f"COUNT_BIG(*) AS tot FROM {self.t('SA')}", (tol,))
        ev, k, tot = int(num(r.get("ev"))), int(num(r.get("k"))), int(num(r.get("tot")))
        smp = self.rows(o, f"SELECT TOP 10 {self.idc('SA')}{', '.join(q(o[x]) for x in inputs)}, {q(o[target])} AS stored, ({cx}) AS calculated, "
                           f"{tg} - ({cx}) AS diff FROM {self.t('SA')} WHERE {nn} AND {cond} ORDER BY ABS({tg} - ({cx})) DESC", (tol,)) if k else []
        self.add("SA", rule, "", "FAIL" if k else "PASS",
                 f"{o[target]} vs {label}: {k:,} of {ev:,} evaluated rows differ (tolerance {tol:g}"
                 + (f", both sides rounded to {int(dp)} dp" if dp is not None else "") + f"); {tot - ev:,} rows with a null input not evaluated",
                 pct(k, ev), k, 0, smp)

    def mdm001(self):
        self.need_rule("MDM001")
        o, u = self.src("MDM"), q(self.src("MDM")["upc_col"])
        r = self.one(o, f"SELECT COUNT_BIG(*) AS k, ISNULL(SUM(c), 0) AS n, (SELECT COUNT_BIG(*) FROM {self.t('MDM')}) AS tot, "
                        f"(SELECT COUNT_BIG(*) FROM {self.t('MDM')} WHERE {u} IS NULL) AS z FROM "
                        f"(SELECT COUNT_BIG(*) AS c FROM {self.t('MDM')} WHERE {u} IS NOT NULL GROUP BY {u} HAVING COUNT_BIG(*) > 1) x")
        k, n, tot, z = (int(num(r.get(x))) for x in ("k", "n", "tot", "z"))
        smp = self.rows(o, f"SELECT TOP 10 {u}, COUNT_BIG(*) AS dup_count FROM {self.t('MDM')} WHERE {u} IS NOT NULL GROUP BY {u} "
                           f"HAVING COUNT_BIG(*) > 1 ORDER BY dup_count DESC") if k else []
        self.add("MDM", "MDM001", "", "FAIL" if k or z else "PASS", f"{k:,} duplicated UPCs ({n:,} rows of {tot:,}); {z:,} null UPC rows",
                 pct(n + z, tot), k, 0, smp)

    def mdm002(self):
        self.need_rule("MDM002")
        o = self.src("MDM")
        p = [f"TRY_CAST({q(o[r])} AS bigint)" for r in ("sys_col", "vendor_col", "item_col")]
        u = f"TRY_CAST({q(o['upc_col'])} AS bigint)"
        calc = f"{p[0]} * CAST(10000000000 AS bigint) + {p[1]} * CAST(100000 AS bigint) + {p[2]}"
        cls = (f"CASE WHEN {' OR '.join(x + ' IS NULL' for x in p + [u])} THEN 'not numeric or null' "
               f"WHEN {p[0]} NOT BETWEEN 0 AND 99 OR {p[1]} NOT BETWEEN 0 AND 99999 OR {p[2]} NOT BETWEEN 0 AND 99999 "
               f"THEN 'part exceeds 2-5-5 width' WHEN {calc} <> {u} THEN 'mismatch' ELSE 'ok' END")
        cte = (f"WITH x AS (SELECT {', '.join(q(o[r]) for r in ('upc_col', 'sys_col', 'vendor_col', 'item_col'))}, {cls} AS cls "
               f"FROM {self.t('MDM')})")
        agg = {r["cls"]: int(num(r["n"])) for r in self.rows(o, f"{cte} SELECT cls, COUNT_BIG(*) AS n FROM x GROUP BY cls")}
        tot, bad = sum(agg.values()), sum(v for k, v in agg.items() if k != "ok")
        smp = self.rows(o, f"{cte} SELECT TOP 10 * FROM x WHERE cls <> 'ok' ORDER BY cls") if bad else []
        det = "; ".join(f"{k}: {v:,}" for k, v in sorted(agg.items()) if k != "ok")
        self.add("MDM", "MDM002", "", "FAIL" if bad else "PASS",
                 f"{bad:,} of {tot:,} rows where sys|vendor|item (2-5-5, bigint) does not equal upc_number" + (f" ({det})" if det else ""),
                 pct(bad, tot), bad, 0, smp)

    def pos001(self):
        self.need_rule("POS001")
        s, _, t1 = self.dcount("POS", "store_col")
        u, _, _ = self.dcount("POS", "upc_col")
        self.add("POS", "POS001", "", "PASS", f"{s:,} distinct stores and {u:,} distinct UPCs in {t1:,} rows", 0, s)

    def pos002(self):
        self.need_rule("POS002")
        n, nn, tot = self.dcount("POS", "upc_col")
        self.add("POS", "POS002", "", "PASS", f"{nn:,} non-null UPC rows, {n:,} distinct UPCs, {tot - nn:,} null UPC rows", 0, nn)

    def spec_checks(self):
        cfg = self.cfg
        cat = lambda: f"{q(self.src('SA')['category_col'])} IS NOT NULL"
        plan = [
            ("SA", "SA001", lambda: self.list_check("SA", "SA001", "SA", "delivery_col", "delivery IDs", cfg.get("expected_delivery_id") or ""), ""),
            ("SA", "SA002", lambda: self.list_check("SA", "SA002", "SA", "task_col", "task numbers", cfg.get("expected_task") or ""), ""),
            ("SA", "SA003", self.sa003, ""),
            ("SA", "SA004", lambda: self.list_check("SA", "SA004", "SA", "week_col", "IRI weeks", cfg.get("expected_weeks") or ""), ""),
            ("SA", "SA005", self.sa005, ""),
            ("SA", "SA006", self.sa006_grain, "grain"),
            ("SA", "SA006", self.sa006_row, "row"),
            ("SA", "SA007", self.sa007, ""),
            ("SA", "SA008", self.sa008, ""),
            ("SA", "SA009", self.sa009, ""),
            ("SA", "SA010", lambda: self.not_null("SA", "SA010", "SA", "shelf_feet_col", cat, " with a category"), ""),
            ("SA", "SA011", lambda: self.not_null("SA", "SA011", "SA", "facings_col", cat, " with a category"), ""),
            ("SA", "SA012", lambda: self.count_check("SA", "SA012", "SA", "store_col", "store"), ""),
            ("SA", "SA013", lambda: self.count_check("SA", "SA013", "SA", "upc_col", "upc"), ""),
            ("SA", "SA014", lambda: self.calc("SA014", "shelf_feet_col", ("facings_col", "upc_width_col"),
                                              lambda f: f"{f[0]} * {f[1]} / 12.0", "facings x upc_width / 12"), ""),
            ("SA", "SA015", lambda: self.calc("SA015", "capacity_col", ("facings_col", "units_deep_col", "units_high_col"),
                                              lambda f: f"{f[0]} * {f[1]} * {f[2]}", "facings x units_deep x units_high"), ""),
            ("SA", "SA016", self.sa016, ""),
            ("MDM", "MDM001", self.mdm001, ""),
            ("MDM", "MDM002", self.mdm002, ""),
            ("PROJ", "PRJ001", lambda: self.list_check("PROJ", "PRJ001", "PROJ", "geo_col", "geographies", None), ""),
            ("PROJ", "PRJ002", lambda: self.count_check("PROJ", "PRJ002", "PROJ", "store_col", "store"), ""),
            ("PROJ", "PRJ003", lambda: self.list_check("PROJ", "PRJ003", "PROJ", "outlet_col", "outlets", None), ""),
            ("PROJ", "PRJ004", lambda: self.list_check("PROJ", "PRJ004", "PROJ", "geo_level_col", "geo levels", None), ""),
            ("PROJ", "PRJ005", lambda: self.list_check("PROJ", "PRJ005", "PROJ", "week_col", "IRI weeks", None), ""),
            ("PROJ", "PRJ006", lambda: self.list_check("PROJ", "PRJ006", "PROJ", "version_col", "versions", None), ""),
            ("POS", "POS001", self.pos001, ""),
            ("POS", "POS002", self.pos002, ""),
            ("POS", "POS003", lambda: self.list_check("POS", "POS003", "POS", "week_col", "IRI weeks", None), ""),
            ("POS", "POS004", lambda: self.not_null("POS", "POS004", "POS", "dollars_col"), ""),
            ("POS", "POS005", lambda: self.not_null("POS", "POS005", "POS", "units_col"), ""),
        ]
        for stage, rule, fn, sub in plan:
            self.safe(stage, rule, fn, sub)

    # ---- cross-source (in memory, works across servers) ----
    def cmp(self, st, rule, frm, to, label, coverage=False):
        A, B = self.keys.get(frm), self.keys.get(to)
        if A is None or B is None:
            return
        miss = sorted(A - B)
        p = pct(len(miss), len(A))
        smp = [{f"sa_{label}": k} for k in miss[:10]]
        if coverage:
            cov, mn = round(100 - p, 4), self.thr(rule, "min_pct", 90)
            status = "FAIL" if cov < mn else "PASS"
            msg = f"{cov}% of SA {label}s found in {st} ({len(miss):,} of {len(A):,} missing; min {mn:g}%)"
        else:
            mx = self.thr(rule, "max_pct")
            status = "FAIL" if p > mx else "PASS"
            msg = f"{len(miss):,} of {len(A):,} SA {label}s missing in {st} ({p}%)"
        self.add(st, rule, label, status, msg + "; compared after trimming leading zeros", p, len(miss), 0, smp)

    def run(self, cfg, results_dir=None):
        t0, rt, seen = time.time(), {}, set()
        self.cfg = cfg
        custom = [c for c in (cfg.get("custom_checks") or []) if c.get("id")]
        self.setup_custom(custom)
        self.off = {r for r in (cfg.get("rules_off") or []) if r in self.rules and self.rule(r).get("enabled", True) is not False}
        try:
            for st in SOURCES:
                src = (cfg.get("sources") or {}).get(st) or {}
                if not src.get("table"):
                    continue
                s = time.time()
                if src["server"] not in seen:
                    seen.add(src["server"])

                    def c_ver(st=st, src=src):
                        r = self.one(src, "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(50)) AS v, "
                                          "CAST(SERVERPROPERTY('Edition') AS varchar(100)) AS e")
                        self.add(st, "IN-000", src["server"], "PASS", f"{src['server']}: {ver(r.get('v'))}, {r.get('e')}")
                    self.safe(st, "IN-000", c_ver)
                self.source(st, src, cfg)
                rt[st] = round(time.time() - s, 1)
            if ((cfg.get("sources") or {}).get("IMP") or {}).get("table") and any(self.on(r) for r in ("IMP-001", "IMP-002", "IMP-003")):
                s = time.time()
                self.imputation(cfg)
                rt["IMP"] = round(time.time() - s, 1)
            s = time.time()
            self.spec_checks()
            rt["SPEC"] = round(time.time() - s, 1)
            if custom:
                s = time.time()
                self.custom_checks(custom)
                rt["CUSTOM"] = round(time.time() - s, 1)
            self.safe("MDM", "XS-001", lambda: self.cmp("MDM", "XS-001", "SA.upc", "MDM.upc", "upc"))
            self.safe("PROJ", "XS-002", lambda: self.cmp("PROJ", "XS-002", "SA.store", "PROJ.store", "store"))
            self.safe("POS", "XS-003", lambda: (self.cmp("POS", "XS-003", "SA.store", "POS.store", "store", True),
                                                 self.cmp("POS", "XS-003", "SA.upc", "POS.upc", "upc", True)))
        finally:
            for c in self._conns.values():
                try:
                    c.close()
                except Exception:
                    pass

        schema = {}  # one INFORMATION_SCHEMA lookup per configured source; feeds the Diagnostics (schema-change) view
        for s, (_, _) in core.INPUTS.items():
            src = (cfg.get("sources") or {}).get(s) or {}
            if not src.get("table"):
                continue
            try:
                cols = list_columns(src.get("server") or cfg.get("server"), src.get("database") or cfg.get("database"),
                                    src["table"], self.user, self.password)
                schema[s] = {"table": src["table"], "columns": {c: t for c, t in cols}}
            except Exception as e:
                schema[s] = {"table": src["table"], "error": str(e)}

        ch = self.results
        blocking = [c for c in ch if core.blocks(c)]
        now = dt.datetime.now()
        run = {
            "report_name": cfg.get("report_name"), "run_id": now.strftime("%Y%m%dT%H%M%S"), "source": "LIVE",
            "timestamp": now.isoformat(timespec="seconds"),
            "summary": {
                "total_checks": len(ch), "passed": sum(c["status"] == "PASS" for c in ch),
                "failed": sum(c["status"] != "PASS" for c in ch),
                "critical_failures": sum(c["status"] == "FAIL" and c["severity"] == "CRITICAL" for c in ch),
                "high_failures": sum(c["status"] == "FAIL" and c["severity"] == "HIGH" for c in ch),
                "manual_required": sum(c["status"] == "MANUAL_REQUIRED" for c in ch),
                "blocks_delivery": bool(blocking), "final_status": "FAIL" if blocking else "PASS",
                "stage_runtime_s": rt, "total_runtime_s": round(time.time() - t0, 1)},
            "live_profile": self.prof, "schema": schema, "checks": ch,
            "rule_descriptions": {k: v.get("description", "") for k, v in self.rules.items()},
            "expected_rules": {k: {"stage": v.get("stage"), "severity": v.get("severity")} for k, v in self.rules.items()
                               if v.get("required") and v.get("enabled", True) is not False},
            "disabled_rules": [k for k, v in self.rules.items() if v.get("enabled", True) is False],
            "skipped_rules": sorted(self.off | {c["id"] for c in custom if not c["run"]}),
            "config": cfg,
        }
        if results_dir:
            name = re.sub(r"[^\w\-]", "_", str(cfg.get("report_name") or "report"))
            p = Path(results_dir) / f"qc_live_{name}_{run['run_id']}.json"
            p.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
            run["saved_as"] = str(p.name)
        return run
