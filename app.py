"""QC Gate: live SQL Server input checks + Intelligent Suite JSON results in one release gate.
Run: python -m streamlit run app.py
"""
import datetime as dt
import html
import json
import re
import uuid
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import qc_core as core

try:
    import qc_live as live
    LIVE_OK, LIVE_ERR = True, ""
except Exception as e:  # pyodbc missing: app still works for IS JSON
    LIVE_OK, LIVE_ERR = False, str(e)

ROOT = Path(__file__).parent
CFG_DIR, RES_DIR = ROOT / "configs", ROOT / "results"
CFG_DIR.mkdir(exist_ok=True)
RES_DIR.mkdir(exist_ok=True)
CATALOG = json.loads((ROOT / "checks_input.json").read_text(encoding="utf-8"))

FB_PATH, DISC_PATH = ROOT / "feedback.json", ROOT / "discussions.json"
REASONS = ["Expected", "Need to Review", "Other"]


def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except Exception:
        return []


def save_json(p, data):
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")

st.set_page_config(page_title="QC Gate", page_icon="🚦", layout="wide")

COLOR = {"PASS": "#00D891", "FAIL": "#E23C3C", "MANUAL_REQUIRED": "#F0A83C", "BLOCKED": "#7C66DC", "NONE": "#C7CBD1"}
STATUS_TXT = {"PASS": "🟢 Pass", "FAIL": "🔴 Fail", "MANUAL_REQUIRED": "🟠 Manual", "BLOCKED": "🟣 Blocked"}
SEV_TXT = {"CRITICAL": "Critical", "HIGH": "High", "WARNING": "Warning", "INFO": "Info"}
NAV = ["🚦 Gate", "🔎 Checks", "🧪 Live input setup", "📈 History", "🧮 SQL", "📖 Guide"]
ICON = {"SA": "🛒", "IMP": "🧩", "PRIOR": "⏮️", "SHELFVAR": "📍", "MDM": "🏷️", "PROJ": "⚖️", "POS": "💳"}
SRC = {s: (ICON[s], lbl, roles) for s, (lbl, roles) in core.INPUTS.items()}
ROLE = core.ROLE
MULTI, TEXT = {"grain", "measures"}, {"imputed_value"}
HINTS = CATALOG.get("column_hints") or {}
HINT = {"SA": "Load columns fills exact name matches (store_number, shelf_feet, facings …). Confirm every mapping; an unmapped column turns its check Manual.",
        "IMP": "Filtered to the expected delivery ID. Imputed status null = collected (SA007), imputed value = imputed (SA008, SA009). Any server.",
        "PRIOR": "Prior wave SA table for SA009: imputed-store shelf feet, facings and capacity vs prior wave (store-level sums).",
        "SHELFVAR": "Read-only lookup for SA005: SA location → shelf_var_id → description. Nothing is updated."}
TOP = {"cfg_report": "report_name", "cfg_expected": "expected_sa_stores", "cfg_delivery": "expected_delivery_id",
       "cfg_task": "expected_task", "cfg_weeks": "expected_weeks"}

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"]{font-family:'Inter',-apple-system,sans-serif}
.block-container{padding-top:2.6rem;max-width:1440px}
header[data-testid="stHeader"]{background:transparent}

/* top nav look: pill tabs, black = active, gray text = inactive (matches Soda's nav bar) */
div[data-testid="stSegmentedControl"]{background:#F3F4F6;padding:4px;border-radius:999px;display:inline-flex}
div[data-testid="stSegmentedControl"] label{border-radius:999px!important;border:none!important;color:#5B616E!important;font-weight:500!important}
div[data-testid="stSegmentedControl"] label[data-checked="true"],
div[data-testid="stSegmentedControl"] label[aria-checked="true"]{background:#0F0F0F!important;color:#fff!important}

/* buttons: Soda uses a solid pill for primary actions */
.stButton>button[kind="primary"]{border-radius:999px;font-weight:600;border:none}
.stButton>button:not([kind="primary"]){border-radius:999px;border:1px solid #E3E5E8}

/* hero: flat white card with a coloured left rail, not a dark gradient banner */
.hero{display:grid;grid-template-columns:auto 1fr auto;gap:22px;align-items:center;padding:20px 26px;border-radius:12px;
 background:#fff;border:1px solid #E7E9EC;border-left:6px solid var(--hc,#C7CBD1);margin-bottom:18px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.light{display:none}
.eyebrow{color:#8A8F98;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.verdict{font-size:26px;font-weight:700;line-height:1.2;color:#0F0F0F;margin-top:2px}
.hsub{color:#5B616E;margin-top:4px;font-size:13.5px}
.hmeta{text-align:right;color:#8A8F98;font-size:12.5px;line-height:1.7}
.hmeta b{color:#0F0F0F;font-weight:600}

/* data-product cards: flat bordered cards, small coloured status dot like the Datasets list */
.grp{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px;color:#8A8F98;text-transform:uppercase;letter-spacing:.04em;margin:4px 0}
.pipe{display:grid;grid-template-columns:repeat(8,1fr);gap:10px;margin:0 0 18px}
.stg{background:#fff;border:1px solid #E7E9EC;border-radius:10px;padding:12px 14px 14px;position:relative}
.stg::before{content:"";position:absolute;top:14px;left:14px;width:8px;height:8px;border-radius:50%;background:var(--c)}
.stg h4{margin:0 0 2px 18px;font-size:12.5px;font-weight:600;color:#0F0F0F}
.stg .s{margin-left:18px;color:var(--c);font-weight:700;font-size:12.5px}
.stg .n{margin-left:18px;color:#8A8F98;font-size:11.5px;margin-top:2px}
.stg.none{background:#F2FDFA}
.stg .bar{margin-left:18px;height:4px;background:#EEF0F2;border-radius:3px;margin-top:8px;overflow:hidden}
.stg .bar b{display:block;height:100%;background:var(--c)}
.stg:nth-child(4){margin-right:10px}

/* marquee: slim scrolling banner; pauses on hover, static when the OS asks for reduced motion */
.mq{overflow:hidden;white-space:nowrap;border-radius:8px;background:rgba(128,128,128,.12);padding:7px 0;margin:0 0 12px;font-size:13px}
.mq span{display:inline-block;padding-left:100%;animation:mq 60s linear infinite}
.mq:hover span{animation-play-state:paused}
@keyframes mq{from{transform:translateX(0)}to{transform:translateX(-100%)}}
@media (prefers-reduced-motion:reduce){.mq span{animation:none;padding-left:12px}}

.empty{padding:48px;border-radius:12px;background:#F2FDFA;border:1px dashed #AAFDE2;text-align:center;color:#5B616E}
.empty h2{margin:0 0 6px;color:#0F0F0F}

/* table-ish headers: uppercase gray labels, like the Datasets/Checks list columns */
[data-testid="stDataFrame"] thead tr th{background:#F6F7F8!important;color:#8A8F98!important;font-size:11px!important;
 text-transform:uppercase;letter-spacing:.03em;font-weight:600!important;border-bottom:1px solid #E7E9EC!important}
[data-testid="stDataFrame"] tbody tr td{border-bottom:1px solid #F0F1F3!important}

@media (max-width:1100px){.pipe{grid-template-columns:repeat(4,1fr)}.hero{grid-template-columns:auto 1fr}.hmeta{display:none}}
</style>""", unsafe_allow_html=True)

BADGE = {"Gold": "🥇 Gold", "Silver": "🥈 Silver", "Bronze": "🥉 Bronze"}
DIM_ICON = {"PASS": "🟢", "FAIL": "🔴", "MANUAL_REQUIRED": "🟠", "BLOCKED": "🟣"}


def prod_df(src=None):
    src = src or {}
    return pd.DataFrame([{"Stage": core.LBL[s], "Owner": (src.get(s) or {}).get("owner", ""), "SLO": (src.get(s) or {}).get("slo", "")}
                         for s in core.STAGES])


def custom_df(rows):
    df = pd.DataFrame([{"Run": c["run"], "ID": c["id"], "Source": c["source"], "Test": c["test"], "Column": c["column"],
                        "Value / SQL": c["value"], "Severity": c["severity"], "Description": c["description"]} for c in rows or []],
                      columns=core.CUSTOM_COLS)
    return df.astype({"Run": bool})


# ---------------- state ----------------
ss = st.session_state
for k, v in {"live_runs": [], "is_runs": [], "sel_live": None, "sel_is": None, "creds": None,
             "cols": {}, "seen_up": set(), "nav": NAV[0], "celebrated": set(), "author": "",
             "feedback": load_json(FB_PATH), "discussions": load_json(DISC_PATH), "rules_off": [], "custom_checks": []}.items():
    ss.setdefault(k, v)
for _s, _d in (CATALOG.get("source_defaults") or {}).items():
    for _k, _v in _d.items():
        ss.setdefault(f"{_s}_{_k}", _v)
# Streamlit deletes a widget's value on any run where that widget is not drawn (e.g. while the SQL tab is open). Re-assigning the value
# as plain session state keeps the Live input setup and SQL tab inputs when switching tabs. Number inputs are left out: they pass an
# explicit default, which Streamlit warns about when the value was also set through session state.
KEEP = [*TOP, *(f"{s}_{k}" for s in SRC for k in ("server", "database", "table")),
        *(f"{s}_{r}" for s, (_, _, roles) in SRC.items() for r in roles), "sql_src", "sql_text", "sql_tpl", "sql_loc", "sql_ver"]
for _k in KEEP:
    if _k in ss:
        ss[_k] = ss[_k]
ss.setdefault("prod_base", prod_df())
ss.setdefault("custom_base", custom_df(ss.custom_checks))
ss._runs = ss.get("_runs", 0) + 1
ss.setdefault("prod", ss.prod_base)
if "nav_next" in ss:
    ss.nav = ss.pop("nav_next")
if "flash" in ss:
    st.toast(ss.pop("flash"), icon="✅")


def add_run(d):
    runs = ss.live_runs if d.get("source") == "LIVE" else ss.is_runs
    k = core.run_key(d)
    if all(core.run_key(r) != k for r in runs):
        runs.append(d)
        runs.sort(key=core.run_key)
    if runs is ss.live_runs:
        ss.sel_live = k
    else:
        ss.sel_is = k


def get_run(runs, key):
    return next((r for r in runs if core.run_key(r) == key), None)


# ---------------- sidebar ----------------
with st.sidebar:
    st.markdown("## 🚦 QC Gate")
    with st.expander("🔌 SQL Server", expanded=ss.creds is None):
        if not LIVE_OK:
            st.warning(f"Live SQL off: {LIVE_ERR}. Run `python -m pip install pyodbc`.")
        st.radio("Authentication", ["Windows", "SQL login"], key="cn_auth", horizontal=True,
                 help="Windows uses the account you are signed in to this computer with. No login or password is entered or stored.")
        win = ss.get("cn_auth", "Windows") == "Windows"
        st.text_input("Default server", key="cn_server")
        st.text_input("Default database", key="cn_db")
        if not win:
            st.text_input("SQL login", key="cn_user")
            st.text_input("Password", type="password", key="cn_pass", help="Held in memory for this session only. Never saved.")
        if st.button("Connect", type="primary", disabled=not LIVE_OK):
            try:
                if not win and not str(ss.get("cn_user") or "").strip():
                    raise ValueError("Enter the SQL login.")
                u, p = (None, None) if win else (ss.cn_user, ss.get("cn_pass") or "")
                v = live.test_connection(ss.cn_server, ss.cn_db, u, p)
                ss.creds = {"user": u, "password": p, "version": v, "auth": "Windows" if win else "SQL login"}
                st.toast(f"Connected: {v}", icon="🔌")
            except Exception as e:
                ss.creds = None
                st.error(f"Not connected: {e}")
        if ss.creds:
            st.success(f"Connected · {ss.creds.get('auth', 'SQL login')} · {ss.creds['version']}")
            if st.button("Disconnect"):
                ss.creds = None
                st.rerun()

    st.markdown("#### 📥 Load results")
    ups = st.file_uploader("IS qc_report JSON or saved live results", type="json", accept_multiple_files=True)
    for f in ups or []:
        if (f.name, f.size) in ss.seen_up:
            continue
        ss.seen_up.add((f.name, f.size))
        try:
            d = json.loads(f.getvalue().decode("utf-8-sig"))
            if not isinstance(d.get("checks"), list):
                raise ValueError("no checks[] array")
            d["_file"] = f.name
            add_run(d)
            st.toast(f"Loaded {f.name}", icon="📥")
        except Exception as e:
            st.error(f"{f.name} not loaded: {e}")

    def picker(label, runs, cur):
        if not runs:
            return None
        opts = [None] + [core.run_key(r) for r in runs]
        return st.selectbox(label, opts, index=opts.index(cur) if cur in opts else len(opts) - 1,
                            format_func=lambda k: "None" if k is None else k.replace("|", "  ·  run "))
    ss.sel_live = picker("Live run", ss.live_runs, ss.sel_live)
    ss.sel_is = picker("IS run", ss.is_runs, ss.sel_is)

MARQUEE = [str(m).strip() for m in (CATALOG.get("marquee") or []) if str(m).strip()]
if MARQUEE:
    _t = "  ·  ".join(html.escape(m) for m in MARQUEE)
    st.markdown(f'<div class="mq"><span style="animation-duration:{max(25, round(len(_t) * 0.28))}s">{_t}</span></div>', unsafe_allow_html=True)

view = st.segmented_control("View", NAV, key="nav", label_visibility="collapsed") or NAV[0]


# ---------------- gate view ----------------
def prod_map():
    inv = {v: k for k, v in core.LBL.items()}
    return {inv[r["Stage"]]: {"owner": str(r["Owner"] or "").strip(), "slo": str(r["SLO"] or "").strip()} for _, r in ss.prod.iterrows()}


def stage_strip(x):
    pm, cells = prod_map(), []
    for s in core.STAGES:
        cs = [c for c in x["checks"] if c["stage"] == s]
        if not cs:
            cells.append(f'<div class="stg none" style="--c:{COLOR["NONE"]}"><h4>{core.LBL[s]}</h4><div class="s">Not checked</div><div class="n">No checks</div></div>')
            continue
        worst = min((c["status"] for c in cs), key=lambda v: core.ST_O.get(v, 9))
        worst = worst if worst in COLOR else "BLOCKED"
        ok, p = sum(c["status"] == "PASS" for c in cs), pm[s]
        tier = core.certify(cs, p)
        own = html.escape(p["owner"]) if p["owner"] else "No owner"
        slo = f'<div class="n">SLO: {html.escape(p["slo"])}</div>' if p["slo"] else ""
        cells.append(f'<div class="stg" style="--c:{COLOR[worst]}"><h4>{core.LBL[s]}</h4><div class="s">{BADGE[tier] if tier else "Not certified"}</div>'
                     f'<div class="n">{ok} of {len(cs)} passed · {own}</div>{slo}<div class="bar"><b style="width:{100 * ok / len(cs):.0f}%"></b></div></div>')
    st.markdown('<div class="grp"><span>Inputs · live SQL Server</span><span>Transforms · Intelligent Suite</span></div>'
                f'<div class="pipe">{"".join(cells)}</div>', unsafe_allow_html=True)


def owners_view(x):
    pm = prod_map()
    rows = []
    for s in core.STAGES:
        cs = [c for c in x["checks"] if c["stage"] == s]
        p = pm.get(s, {"owner": "", "slo": ""})
        scans = [c["logged_at"] for c in cs if c.get("logged_at")]
        last = max(scans) if scans else ((x["J"] or {}).get("timestamp") if not scans and any(c["source"] == "IS" for c in cs) else None)
        rows.append({"Stage": core.LBL[s], "Badge": BADGE.get(core.certify(cs, p), "Not certified") if cs else "No checks",
                     "Failures": sum(c["status"] == "FAIL" for c in cs), "Owner": p["owner"] or "—", "Last scan": last or "—"})
    with st.expander("📦 Data products · each stage as a dataset", expanded=False):
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        st.caption("Click a column header to sort. Last scan comes from the live run's per-check timestamp, or the IS file's own "
                   "timestamp field when present; stages with neither show —.")
        st.markdown("###### Edit owner and SLO")
        st.caption("Needed for Silver and Gold. Bronze: nothing blocks. Silver: every check evaluated. Gold: every check passes. "
                   "Badges are informational; the release decision above is unchanged.")
        ss.prod = st.data_editor(ss.prod_base, hide_index=True, disabled=["Stage"], key="prod_ed",
                                 column_config={"SLO": st.column_config.TextColumn(width="large", help="What consumers can rely on, in one sentence")})


def discussions_view(x):
    report = (x["J"] or x["L"]).get("report_name") or "Unnamed report"
    with st.expander("💬 Discussions"):
        threads = [t for t in ss.discussions if t.get("report_name") == report]
        with st.form("new_disc", clear_on_submit=True):
            c1, c2 = st.columns([1, 3])
            stage = c1.selectbox("Stage (optional)", [""] + [core.LBL[s] for s in core.STAGES], key="disc_stage")
            title = c2.text_input("Title", key="disc_title")
            body = st.text_area("First comment", key="disc_body")
            by = st.text_input("Your name (optional)", value=ss.author, key="disc_by")
            if st.form_submit_button("Start discussion"):
                if not (title.strip() and body.strip()):
                    st.warning("Enter a title and a first comment.")
                else:
                    ss.author = by
                    ss.discussions.append({"id": str(uuid.uuid4()), "report_name": report, "stage": stage, "title": title.strip(),
                                           "status": "Open", "created_at": now(),
                                           "comments": [{"by": by.strip() or "Anonymous", "at": now(), "text": body.strip()}]})
                    save_json(DISC_PATH, ss.discussions)
                    st.rerun()
        if not threads:
            st.caption("No discussions yet for this report.")
        for t in sorted(threads, key=lambda t: t["created_at"], reverse=True):
            with st.container(border=True):
                h1, h2 = st.columns([4, 1])
                h1.markdown(f"**{t['title']}**" + (f" · {t['stage']}" if t.get("stage") else ""))
                h2.markdown("🟢 Open" if t["status"] == "Open" else "⚪ Resolved")
                for cm in t["comments"]:
                    st.caption(f"{cm['at']} · **{cm['by']}** · {cm['text']}")
                with st.form(f"cm_form_{t['id']}", clear_on_submit=True):
                    nc = st.text_area("Add a comment", key=f"nc_{t['id']}", label_visibility="collapsed", placeholder="Add a comment")
                    by2 = st.text_input("Your name (optional)", value=ss.author, key=f"ncby_{t['id']}")
                    b1, b2 = st.columns([1, 1])
                    post = b1.form_submit_button("Post comment")
                    toggle = b2.form_submit_button("Mark resolved" if t["status"] == "Open" else "Reopen")
                if post and nc.strip():
                    ss.author = by2
                    t["comments"].append({"by": by2.strip() or "Anonymous", "at": now(), "text": nc.strip()})
                    save_json(DISC_PATH, ss.discussions)
                    st.rerun()
                if toggle:
                    t["status"] = "Resolved" if t["status"] == "Open" else "Open"
                    save_json(DISC_PATH, ss.discussions)
                    st.rerun()


def diagnostics_view(x):
    L = x["L"]
    with st.expander("🔍 Diagnostics · schema changes since the last live run"):
        if not L:
            st.caption("Load a live run to see schema diagnostics.")
            return
        prev_run = core.prev(ss.live_runs, L)
        if not prev_run:
            st.caption("No earlier live run of this report to compare against yet.")
            return
        diffs = core.schema_diff(prev_run.get("schema"), L.get("schema"))
        rows = [{"Source": core.INPUTS[d["source"]][0], "Column": d["column"], "Event": d["event"], "Details": d["details"]} for d in diffs]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True)
        else:
            st.caption(f"No column or type changes vs run {prev_run.get('run_id')}.")
        errs = [f"{core.INPUTS[s][0]}: {v['error']}" for s, v in (L.get("schema") or {}).items() if v.get("error")]
        if errs:
            st.caption("Schema lookup failed, skipped from this comparison: " + "; ".join(errs))


def dims_view(x):
    m = core.dim_matrix(x["checks"], CATALOG.get("dimensions"))
    rows = [{"Stage": core.LBL[s], **{d.capitalize(): DIM_ICON.get(m.get((s, d)), "–") for d in core.DIMS}}
            for s in core.STAGES if any((s, d) in m for d in core.DIMS)]
    with st.expander("📐 Quality dimensions"):
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True)
            st.caption("Worst status per dimension. – means no mapped check. Mapping: checks_input.json → dimensions. IS JSON rules are not mapped.")
        else:
            st.caption("No mapped checks in this run.")


def hero(x):
    L, J = x["L"], x["J"]
    if J:
        title = "Do not release" if x["fail"] else "Release with caveats" if x["warn"] else "Release"
    else:
        title = "Fix inputs before processing" if x["fail"] else "Inputs ready to process"
    light = "r" if x["fail"] else "y" if x["warn"] else "g"
    r = J or L
    sub = f'{len(x["blocking"])} blocking check(s)' + (", delivery stage not checked" if x["need_del"] else "")
    if J and (J.get("summary") or {}).get("delivery_recommendation"):
        sub += f' · IS file says {J["summary"]["delivery_recommendation"]}'
    meta = (f'<div>Live run <b>{L["run_id"]}</b></div>' if L else "") + (f'<div>IS run <b>{J["run_id"]}</b></div>' if J else "")
    if L and J and L.get("report_name") != J.get("report_name"):
        meta += '<div style="color:#FF9B9E">Live and IS runs are different reports</div>'
    hc = {"r": COLOR["FAIL"], "y": COLOR["MANUAL_REQUIRED"], "g": COLOR["PASS"]}[light]
    st.markdown(f'<div class="hero" style="--hc:{hc}"><div><div class="eyebrow">{r.get("report_name") or r.get("_file") or "Report"}</div>'
                f'<div class="verdict">{title}</div><div class="hsub">{sub}</div></div><div class="hmeta">{meta}</div></div>',
                unsafe_allow_html=True)


def to_df(x):
    rows = [{"Source": core.SRC_LBL[c["source"]], "Stage": core.LBL.get(c["stage"], c["stage"]), "Rule": c.get("rule"),
             "Status": STATUS_TXT.get(c["status"], c["status"]), "Severity": SEV_TXT.get(c.get("severity"), c.get("severity")),
             "Affected %": min(float(c.get("affected_pct") or 0), 100.0), "Message": str(c.get("message") or "").splitlines()[0] if c.get("message") else "",
             "Blocks": "⛔" if core.blocks(c) else ("⚠️ suspect" if core.suspect(c) else ""), "Change": x["diff"].get(c["check_id"], ""),
             "_i": i, "_st": c["status"], "_sev": c.get("severity"), "_stage": c["stage"], "_src": c["source"]}
            for i, c in enumerate(x["checks"])]
    df = pd.DataFrame(rows)
    df["_b"] = df["Blocks"].eq("⛔")
    return df.sort_values(["_b", "_st", "_sev", "_stage"], ascending=[False, True, True, True],
                          key=lambda s: s.map(core.ST_O) if s.name == "_st" else s.map(core.SEV_O) if s.name == "_sev"
                          else s.map(lambda v: core.STAGE_ORDER.get(v, len(core.STAGES))) if s.name == "_stage" else s)


def charts(x):
    a, b = st.columns([1, 1.15])
    with a:
        st.markdown("##### 🏬 Store count through the pipeline")
        if x["trace"]:
            fig = go.Figure(go.Funnel(y=[s for s, _ in x["trace"]], x=[n for _, n in x["trace"]], textinfo="value+percent previous",
                                      marker={"color": ["#2B6CB0", "#4A86C8", "#E5484D" if len(x["trace"]) > 2 else "#6FA3DA", "#F5A524"][:len(x["trace"])]},
                                      connector={"line": {"color": "rgba(128,128,128,.35)"}}))
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, config={"displayModeBar": False})
            st.caption("Stages should keep or shrink the store count. Growth between steps usually means join fan-out or a different source table.")
        else:
            st.info("Store counts appear once a live SA run or an IS JSON with SA/Prep/Staging checks is loaded.")
    with b:
        st.markdown("##### 🎯 Where the checks landed (click to drill in)")
        d = pd.DataFrame([{"Stage": core.LBL.get(c["stage"], c["stage"]), "Status": STATUS_TXT.get(c["status"], "🟣 Blocked")[2:], "Rule": c.get("rule") or "?", "n": 1}
                          for c in x["checks"]])
        fig = px.sunburst(d, path=["Stage", "Status", "Rule"], values="n", color="Status",
                          color_discrete_map={"Pass": COLOR["PASS"], "Fail": COLOR["FAIL"], "Manual": COLOR["MANUAL_REQUIRED"], "Blocked": COLOR["BLOCKED"], "(?)": "#D3DAE2"})
        fig.update_traces(insidetextorientation="radial", hovertemplate="%{label}<br>%{value} check(s)<extra></extra>")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, config={"displayModeBar": False})


def gate_view():
    L, J = get_run(ss.live_runs, ss.sel_live), get_run(ss.is_runs, ss.sel_is)
    x = core.build(L, J, ss.live_runs, ss.is_runs, LIVE_OK)
    if not x:
        st.markdown('<div class="empty"><h2>🚦 Nothing on the gate yet</h2><p>Run live input checks from <b>Live input setup</b>, '
                    'or drop an Intelligent Suite <code>qc_report_*.json</code> in the sidebar. Both land here as one release decision.</p></div>',
                    unsafe_allow_html=True)
        return
    hero(x)
    gk = (ss.sel_live, ss.sel_is)
    if not x["fail"] and not x["warn"] and gk not in ss.celebrated:
        ss.celebrated.add(gk)
        st.balloons()

    pl, pj = (core.prev(ss.live_runs, L) if L else None), (core.prev(ss.is_runs, J) if J else None)
    px_ = core.build(pl or L, pj or J, ss.live_runs, ss.is_runs, LIVE_OK) if (pl or pj) else None
    cn, pc = x["counts"], (px_["counts"] if px_ else None)
    dl = lambda k: (cn[k] - pc[k]) if pc else None
    m = st.columns(5)
    m[0].metric("Checks", cn["total_checks"], dl("total_checks"), delta_color="off")
    m[1].metric("Passed", f'{cn["passed"]} ({100 * cn["passed"] / max(cn["total_checks"], 1):.0f}%)', dl("passed"))
    m[2].metric("Blocking", cn["blocking"], dl("blocking"), delta_color="inverse")
    m[3].metric("Manual or blocked", cn["manual_required"] + cn["blocked"], dl("manual_required"), delta_color="inverse")
    m[4].metric("Suspect passes", cn["suspect"], dl("suspect"), delta_color="inverse")

    strip = st.container()  # drawn after the editor runs so owner edits show on the same rerun
    owners_view(x)
    with strip:
        stage_strip(x)
    dims_view(x)
    diagnostics_view(x)
    discussions_view(x)
    charts(x)

    with st.expander(f"⚠️ Report integrity: {len(x['issues'])} finding(s)", expanded=bool(x["issues"])):
        for i in x["issues"]:
            st.markdown(f"- {i}")
        if J:
            s, r1, r2 = J.get("summary") or {}, core.recount(J["checks"]), core.recount(L["checks"]) if L else {}
            rows = [{"Count": k.replace("_", " ").capitalize(), "IS file": s.get(k), "IS recount": r1[k], "Live": r2.get(k)}
                    for k in ("total_checks", "passed", "failed", "critical_failures", "manual_required", "blocked", "critical_unresolved")]
            st.dataframe(pd.DataFrame(rows), hide_index=True)


# ---------------- checks view ----------------
def checks_view():
    L, J = get_run(ss.live_runs, ss.sel_live), get_run(ss.is_runs, ss.sel_is)
    x = core.build(L, J, ss.live_runs, ss.is_runs, LIVE_OK)
    if not x:
        st.info("No checks yet. Run live input checks or load an Intelligent Suite JSON from the sidebar.")
        return
    df = to_df(x)
    f1, f2, f3 = st.columns([1.2, 2.6, 1.4])
    srcs = f1.pills("Source", [core.SRC_LBL[s] for s in ("LIVE", "IS", "CROSS") if s in set(df["_src"])], selection_mode="multi", key="f_src")
    present_stages = sorted(set(df["_stage"]), key=lambda v: core.STAGE_ORDER.get(v, len(core.STAGES)))
    stgs = f2.pills("Stage", [core.LBL.get(s, s) for s in present_stages], selection_mode="multi", key="f_stage")
    txt = f3.text_input("Search", placeholder="Rule, column or message", key="f_txt")
    g1, g2, g3, g4 = st.columns([1.4, 1.4, 1, 1])
    sts = g1.multiselect("Status", list(STATUS_TXT.values()), key="f_st")
    sev = g2.multiselect("Severity", list(SEV_TXT.values()), key="f_sev")
    only_b = g3.toggle("Only blockers", key="f_b")
    only_c = g4.toggle("Changed since last run", key="f_c", disabled=not any(x["diff"].values()))
    v = df
    if srcs: v = v[v["Source"].isin(srcs)]
    if stgs: v = v[v["Stage"].isin(stgs)]
    if sts: v = v[v["Status"].isin(sts)]
    if sev: v = v[v["Severity"].isin(sev)]
    if only_b: v = v[v["_b"]]
    if only_c: v = v[v["Change"] != ""]
    if txt: v = v[(v["Rule"].fillna("") + " " + v["Message"]).str.contains(re.escape(txt), case=False)]
    st.caption(f"{len(v)} of {len(df)} checks · select a row for detail")
    ev = st.dataframe(v[["Blocks", "Source", "Stage", "Rule", "Status", "Severity", "Affected %", "Message", "Change"]], hide_index=True,
                      on_select="rerun", selection_mode="single-row", key="tbl", height=min(38 * len(v) + 40, 520),
                      column_config={"Blocks": st.column_config.TextColumn("", width="small"),
                                     "Affected %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
                                     "Message": st.column_config.TextColumn(width="large")})
    sel = ev.selection.rows if ev and ev.selection else []
    if sel and sel[0] < len(v):
        c = x["checks"][int(v.iloc[sel[0]]["_i"])]
        with st.container(border=True):
            a, b = st.columns([3, 1])
            a.markdown(f"**{c.get('rule')}** · {x['desc'].get(c.get('rule'), 'No description')}")
            b.markdown(f"{STATUS_TXT.get(c['status'], c['status'])} · {SEV_TXT.get(c.get('severity'), c.get('severity'))}")
            st.markdown(f"`{c['check_id']}`  \nActual / expected: **{core.fmt(c.get('actual_value')) or 'n/a'}** / **{core.fmt(c.get('expected_value')) or 'n/a'}**")
            st.code(str(c.get("message")), language=None, wrap_lines=True)

            pts = core.trend(c["check_id"], (J or L).get("report_name"), ss.live_runs + ss.is_runs)
            if len(pts) > 1:
                st.markdown("History")
                tf = pd.DataFrame(pts, columns=["Run", "Value"])
                fig = px.line(tf, x="Run", y="Value", markers=True)
                fig.update_layout(height=220, margin=dict(l=10, r=10, t=10, b=10), paper_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig, config={"displayModeBar": False})

            smp = c.get("sample_failing_rows") or []
            if smp:
                st.markdown("Sample failing rows")
                sdf = pd.DataFrame(smp)
                q = st.text_input("Search failed rows", key=f"fr_{c['check_id']}", placeholder="Filter by any column value")
                view = sdf
                if q:
                    mask = sdf.apply(lambda col: col.astype(str).str.contains(re.escape(q), case=False, na=False)).any(axis=1)
                    view = sdf[mask]
                    sty = view.style.map(lambda v: "background-color:#FFF3B0" if q.lower() in str(v).lower() else "")
                    st.dataframe(sty, hide_index=True)
                else:
                    st.dataframe(view, hide_index=True)
                if q:
                    st.caption(f"{len(view)} of {len(sdf)} sample rows match \"{q}\".")

            run_id = str((J or L).get("run_id"))
            fkey = f"{c['check_id']}|{run_id}"
            st.markdown("Feedback")
            for f in [f for f in ss.feedback if f"{f['check_id']}|{f['run_id']}" == fkey]:
                st.caption(f"{f['at']} · **{f.get('by') or 'Anonymous'}** · {'✅ Correctly classified' if f['is_correct'] else '❌ Not correct'}"
                          f" · {f['reason']}" + (f" — {f['comment']}" if f.get('comment') else ""))
            with st.form(f"fb_form_{fkey}", clear_on_submit=True):
                fc1, fc2 = st.columns([1, 2])
                correct = fc1.radio("Is this correctly classified?", ["Yes", "No"], horizontal=True, key=f"fb_c_{fkey}")
                reason = fc2.selectbox("Reason", REASONS, key=f"fb_r_{fkey}")
                comment = st.text_area("Comment (optional)", key=f"fb_m_{fkey}")
                by = st.text_input("Your name (optional)", value=ss.author, key=f"fb_by_{fkey}")
                if st.form_submit_button("Save feedback"):
                    ss.author = by
                    ss.feedback.append({"report_name": (J or L).get("report_name"), "check_id": c["check_id"], "run_id": run_id,
                                        "is_correct": correct == "Yes", "reason": reason, "comment": comment.strip(),
                                        "by": by.strip(), "at": now()})
                    save_json(FB_PATH, ss.feedback)
                    st.rerun()

    exc = df[(df["_st"] != "PASS") | df["Blocks"].eq("⚠️ suspect")].drop(columns=[k for k in df.columns if k.startswith("_")])
    name = re.sub(r"\W+", "_", str((J or L).get("report_name") or "report"))
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("⬇️ Exceptions CSV", exc.to_csv(index=False).encode("utf-8"), f"qc_exceptions_{name}.csv", "text/csv")
    d2.download_button("⬇️ Combined gate JSON", json.dumps({"report_name": (J or L).get("report_name"), "live_run": (L or {}).get("run_id"),
                       "is_run": (J or {}).get("run_id"), "gate": "FAIL" if x["fail"] else "PASS", "issues": x["issues"], "checks": x["checks"]},
                       indent=2, default=str), f"qc_gate_{name}.json", "application/json")


# ---------------- live setup view ----------------
def get_cfg(resolve):
    g = lambda k: str(ss.get(k) or "").strip()
    cfg = {v: g(k) for k, v in TOP.items()}
    cfg.update({"server": g("cn_server"), "database": g("cn_db"), "sources": {}, "products": prod_map(),
                "rules_off": list(ss.get("rules_off") or []), "custom_checks": list(ss.get("custom_checks") or [])})
    for s, (_, _, roles) in SRC.items():
        o = {"server": g(f"{s}_server") or (cfg["server"] if resolve else ""), "database": g(f"{s}_database") or (cfg["database"] if resolve else ""),
             "table": g(f"{s}_table")}
        for r in roles:
            o[r] = list(ss.get(f"{s}_{r}") or []) if r in MULTI else g(f"{s}_{r}")
        cfg["sources"][s] = o
    return cfg


def load_cfg():
    n = ss.get("cfg_pick")
    if not n:
        return
    c = json.loads((CFG_DIR / f"{n}.json").read_text(encoding="utf-8"))
    for k, v in TOP.items():
        ss[k] = c.get(v, "")
    if c.get("server"): ss.cn_server = c["server"]
    if c.get("database"): ss.cn_db = c["database"]
    for s, (_, _, roles) in SRC.items():
        o = (c.get("sources") or {}).get(s) or {}
        for k in ("server", "database", "table"):
            ss[f"{s}_{k}"] = o.get(k, "")
        for r in roles:
            ss[f"{s}_{r}"] = list(o.get(r) or []) if r in MULTI else (o.get(r) or "")
    ss.prod_base = prod_df(c.get("products"))
    ss.prod = ss.prod_base
    ss.pop("prod_ed", None)
    ss.rules_off = list(c.get("rules_off") or [])
    ss.pop("ready_ed", None)
    ss.custom_checks = list(c.get("custom_checks") or [])
    ss.custom_base = custom_df(ss.custom_checks)
    ss.pop("custom_ed", None)
    ss.flash = f"Loaded setup {n}"


def estimate(cfg):
    n = 4 + sum(1 for r in core.REQ if (CATALOG["rules"].get(r) or {}).get("enabled", True) is not False) + 4
    for o in cfg["sources"].values():
        if o.get("table"):
            n += 3 + 2 * len(o.get("grain") or []) + 4 * len(o.get("measures") or []) + 2
    return max(n, 1)


def load_cols(s, roles):
    """Load one source's columns and prefill exact name matches; never overwrites a chosen mapping."""
    ss.cols[s] = live.list_columns(ss.get(f"{s}_server") or ss.cn_server, ss.get(f"{s}_database") or ss.cn_db,
                                   ss.get(f"{s}_table"), ss.creds["user"], ss.creds["password"])
    low = {n.lower(): n for n, _ in ss.cols[s]}
    for r in roles:
        if r in MULTI or r in TEXT or ss.get(f"{s}_{r}"):
            continue
        hit = next((low[h.lower()] for h in HINTS.get(r, []) if h.lower() in low), None)
        if hit:
            ss[f"{s}_{r}"] = hit


def setup_view():
    if not LIVE_OK:
        st.error(f"Live SQL is off: {LIVE_ERR}. Install with `python -m pip install pyodbc`, then restart.")
        return
    if ss.get("_setup_last") != ss._runs - 1:  # Setup was not drawn in the previous run, so the editor lost its state
        ss.custom_base = custom_df(ss.custom_checks)
        ss.pop("custom_ed", None)
    ss._setup_last = ss._runs
    saved = sorted(p.stem for p in CFG_DIR.glob("*.json"))
    legacy = (CATALOG["rules"].get("SA-005") or {}).get("enabled", True) is not False
    gap = "<div style='height:28px'></div>"

    # 1. Report and expected values
    a, b, c, e = st.columns([2, 1.3, 1.3, 1.3])
    a.text_input("Report name", key="cfg_report", placeholder="Report name")
    b.text_input("Expected delivery ID", key="cfg_delivery", help="SA001; filters the imputed store sample for SA007, SA008, SA009")
    c.text_input("Expected task number", key="cfg_task", help="SA002")
    e.text_input("Expected IRI week(s)", key="cfg_weeks", help="SA004; comma separated")
    f, g, h, i = st.columns([2.2, 0.7, 1.8, 1.3])
    f.selectbox("Saved setups", [""] + saved, key="cfg_pick", format_func=lambda v: v or "Choose a saved setup")
    g.markdown(gap, unsafe_allow_html=True)
    g.button("Load", on_click=load_cfg, disabled=not saved)
    h.markdown(gap, unsafe_allow_html=True)
    load_all = h.button("Load columns · all sources", disabled=ss.creds is None, help="Loads columns for every source that has a table and fills exact name matches")
    if legacy:  # SA-005 is disabled in checks_input.json; the field only appears if it is re-enabled
        i.text_input("Expected SA stores", key="cfg_expected", help="Legacy SA-005 only")
    if load_all:
        errs, n = [], 0
        for s, (_, lbl, roles) in SRC.items():
            if ss.get(f"{s}_table"):
                try:
                    load_cols(s, roles)
                    n += 1
                except Exception as ex:
                    errs.append(f"{lbl}: {ex}")
        ss.load_err = errs
        if n:
            ss.flash = f"Columns loaded for {n} source(s)"
        st.rerun()
    for m in ss.pop("load_err", []):
        st.error(m)

    # 2. Status, run and save: drawn at the top, filled after the source tabs so every widget still renders on every run
    cfg = get_cfg(True)
    top = st.container()

    # 3. One tab per source (static labels: a changing label would reset the selected tab)
    tabs = st.tabs([f"{icon} {label}" for icon, label, _ in SRC.values()])
    for tab, (s, (icon, label, roles)) in zip(tabs, SRC.items()):
        with tab:
            cols = ss.cols.get(s, [])
            if s in HINT:
                st.caption(HINT[s])
            need = list(dict.fromkeys(m for r in core.REQ if (CATALOG["rules"].get(r) or {}).get("enabled", True) is not False
                                      for m in core.missing_for(r, cfg) if m.startswith(core.INPUTS[s][0])))
            st.caption("✅ Everything the enabled checks need from this source is mapped." if not need else "🟠 Needs: " + "; ".join(need))
            x1, x2, x3, x4 = st.columns([1.1, 1.1, 2.2, 0.9])
            x1.text_input("Server", key=f"{s}_server", placeholder="Default server")
            x2.text_input("Database", key=f"{s}_database", placeholder="Default database")
            x3.text_input("Table", key=f"{s}_table", placeholder="schema.table or db.schema.table · blank to skip")
            x4.markdown(gap, unsafe_allow_html=True)
            if x4.button("Load columns", key=f"lc_{s}", disabled=ss.creds is None):
                try:
                    load_cols(s, roles)
                    st.rerun()
                except Exception as ex:
                    st.error(str(ex))
            if cols:
                st.caption(f"{len(cols)} columns loaded")
            types = dict(cols)
            names = list(types)
            rc = st.columns(4)
            for j, r in enumerate(roles):
                k, box = f"{s}_{r}", rc[j % 4]
                if r in TEXT:
                    box.text_input(ROLE[r], key=k)
                elif r in MULTI:
                    cur = list(ss.get(k) or [])
                    box.multiselect(ROLE[r], names + [v for v in cur if v not in names], key=k,
                                    format_func=lambda v, t=types: f"{v} ({t[v]})" if v in t else v, placeholder="Load columns, then choose")
                else:
                    cur = ss.get(k) or ""
                    opts = [""] + names + ([cur] if cur and cur not in names else [])
                    box.selectbox(ROLE[r], opts, key=k, format_func=lambda v, t=types: (f"{v} ({t[v]})" if v in t else v) if v else "Not used")

    with top:
        off, rd = set(ss.get("rules_off") or []), []
        for r in core.REQ:
            meta = CATALOG["rules"].get(r) or {}
            if meta.get("enabled", True) is False:
                continue
            miss = core.missing_for(r, cfg)
            rd.append({"Run": r not in off, "Check": r, "Severity": SEV_TXT.get(meta.get("severity"), meta.get("severity")),
                       "Description": meta.get("description", ""), "Status": "🟠 Manual" if miss else "✅ Ready", "Needs": "; ".join(miss)})
        nr = sum(x["Status"] == "✅ Ready" for x in rd)
        crit = sum(x["Status"] == "🟠 Manual" and x["Severity"] in ("Critical", "High") for x in rd)
        b1, b2, msg = st.columns([1.3, 1, 3])
        go_ = b1.button("▶ Run live checks", type="primary", disabled=ss.creds is None)
        save_ = b2.button("💾 Save setup")
        if ss.creds is None:
            msg.info("Connect to SQL Server in the sidebar to load columns and run checks.")
        with st.expander(f"✅ Check readiness: {nr} of {len(rd)} ready" + (f" · {crit} Critical or High will block as Manual" if crit else ""), expanded=nr < len(rd)):
            if st.button("Reset: run all checks"):
                ss.rules_off = []
                ss.pop("ready_ed", None)
                st.rerun()
            ed = st.data_editor(pd.DataFrame(rd), hide_index=True, key="ready_ed", height=min(36 * len(rd) + 40, 460),
                                disabled=["Check", "Severity", "Description", "Status", "Needs"],
                                column_config={"Run": st.column_config.CheckboxColumn("Run", help="Untick to switch a check off for this run"),
                                               "Description": st.column_config.TextColumn(width="large")})
            ss.rules_off = [r for r, on_ in zip(ed["Check"], ed["Run"]) if not on_]
            if ss.rules_off:
                crit_off = [r for r, sv in zip(ed["Check"], ed["Severity"]) if r in ss.rules_off and sv in ("Critical", "High")]
                st.warning(f"{len(ss.rules_off)} check(s) switched off for this run: {', '.join(ss.rules_off)}. They show as Manual in the results."
                           + (f" Critical or High ones block release: {', '.join(crit_off)}." if crit_off else " Put them in the release notes."))
            dis = [r for r in core.REQ if (CATALOG["rules"].get(r) or {}).get("enabled", True) is False]
            if dis:
                st.caption("Disabled in checks_input.json, not listed: " + ", ".join(dis))
            st.markdown("###### ➕ Your own checks")
            st.caption("Add a row to create a check on a mapped source. Tests: **Not null**, **Unique** (comma-separated columns), **Allowed values** (comma separated), "
                       "**Range** (min,max, either may be blank), **Custom SQL** (a SELECT that returns the failing rows). A blank ID becomes CUS-001, CUS-002 …")
            ce = st.data_editor(ss.custom_base, key="custom_ed", num_rows="dynamic", hide_index=True,
                                column_config={"Run": st.column_config.CheckboxColumn("Run", default=True),
                                               "ID": st.column_config.TextColumn("ID", help="Blank = CUS-001, CUS-002 …"),
                                               "Source": st.column_config.SelectboxColumn("Source", options=core.CUSTOM_SRC, required=True),
                                               "Test": st.column_config.SelectboxColumn("Test", options=core.CUSTOM_TESTS, required=True),
                                               "Column": st.column_config.TextColumn("Column", help="Unique accepts several, comma separated"),
                                               "Value / SQL": st.column_config.TextColumn("Value / SQL", width="large", help="Allowed values: A,B · Range: 0,10 · Custom SQL: a SELECT"),
                                               "Severity": st.column_config.SelectboxColumn("Severity", options=core.CUSTOM_SEV, default="WARNING", required=True),
                                               "Description": st.column_config.TextColumn("Description", width="large")})
            ss.custom_checks = core.custom_clean(ce.to_dict("records"))
            taken, probs = set(), []
            for c in ss.custom_checks:
                p = core.custom_problem(c, cfg, set(CATALOG["rules"]), taken)
                taken.add(c["id"])
                if p and c["run"]:
                    probs.append(f"{c['id']}: {p}")
            if probs:
                st.warning("These will run as Manual: " + "; ".join(probs))
            elif ss.custom_checks:
                st.caption(f"{len(ss.custom_checks)} of your own check(s) will run with the live checks and count in the gate like any other. They are saved with the setup.")
        if save_:
            if not cfg["report_name"]:
                st.warning("Enter a report name before saving.")
            else:
                n = re.sub(r"[^\w\-]", "_", cfg["report_name"])
                (CFG_DIR / f"{n}.json").write_text(json.dumps(get_cfg(False), indent=2), encoding="utf-8")
                st.toast(f"Saved setup {n}. Passwords are never saved.", icon="💾")
        if go_:
            cfg["rules_off"] = list(ss.get("rules_off") or [])
            cfg["custom_checks"] = list(ss.get("custom_checks") or [])
            if not cfg["report_name"]:
                st.warning("Enter a report name.")
                return
            if not any(o["table"] for o in cfg["sources"].values()):
                st.warning("Enter at least one source table.")
                return
            est, done = estimate(cfg), {"n": 0}
            bar = st.progress(0.0, text="Starting")
            box = st.status("Running live checks on SQL Server…", expanded=True)

            def on_check(c):
                done["n"] += 1
                bar.progress(min(done["n"] / est, 0.99), text=f"{done['n']} checks done")
                box.write(f"{STATUS_TXT.get(c['status'], c['status'])[:1]} **{core.LBL.get(c['stage'], c['stage'])}** · {c['rule']} · {c['message']}")
            try:
                r = live.LiveRunner(ss.creds["user"], ss.creds["password"], CATALOG, on_check).run(cfg, RES_DIR)
            except Exception as ex:
                box.update(label=f"Run failed: {ex}", state="error")
                return
            bar.progress(1.0, text="Done")
            nb = sum(core.blocks(c) for c in r["checks"])
            box.update(label=f"Done: {len(r['checks'])} checks, {nb} blocking · saved results/{r.get('saved_as')}",
                       state="error" if nb else "complete", expanded=False)
            add_run(r)
            ss.flash = f"Live run {r['run_id']} added to the gate"
            ss.nav_next = NAV[0]
            st.rerun()


# ---------------- history view ----------------
def history_view():
    runs = ss.live_runs + ss.is_runs
    if not runs:
        st.info("Load or run at least one report to see history.")
        return
    reports = sorted({r.get("report_name") or "?" for r in runs})
    rep = st.selectbox("Report", reports)
    rows = []
    for r in runs:
        if (r.get("report_name") or "?") != rep:
            continue
        rc = core.recount(r["checks"])
        rows.append({"Run": str(r.get("run_id")), "Source": "Live SQL" if r.get("source") == "LIVE" else "IS JSON", "Checks": rc["total_checks"],
                     "Pass rate %": round(100 * rc["passed"] / max(rc["total_checks"], 1), 1), "Blocking": rc["blocking"],
                     "Manual/blocked": rc["manual_required"] + rc["blocked"], "File": r.get("_file") or r.get("saved_as") or ""})
    h = pd.DataFrame(rows).sort_values("Run")
    if len(h) > 1:
        a, b = st.columns(2)
        f1 = px.line(h, x="Run", y="Pass rate %", color="Source", markers=True, color_discrete_sequence=["#2B6CB0", "#F5A524"])
        f2 = px.bar(h, x="Run", y="Blocking", color="Source", barmode="group", color_discrete_sequence=["#E5484D", "#7C66DC"])
        for f in (f1, f2):
            f.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10), paper_bgcolor="rgba(0,0,0,0)", legend_title_text="")
        a.plotly_chart(f1, config={"displayModeBar": False})
        b.plotly_chart(f2, config={"displayModeBar": False})
    else:
        st.caption("Load a second run of this report to see trends.")
    st.dataframe(h, hide_index=True, column_config={"Pass rate %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%")})


# ---------------- SQL IDE view ----------------
SQL_TPL = {"ild": "ILD projected facings (cdm_geo × product)", "rows": "Combined SA × MDM × PROJ (row sample)",
           "counts": "Join row counts and fan-out check"}


def sql_insert():
    """Fill the editor with a template built from the sources mapped in Live input setup; run it with Source = Shelf audit."""
    try:
        ss.sql_text = live.ild_sql(get_cfg(True), ss.sql_tpl, ss.get("sql_loc") or "", ss.get("sql_ver") or "")
        ss.sql_src = "SA"
        ss.pop("sql_tpl_err", None)
    except Exception as e:
        ss.sql_tpl_err = str(e)


def sql_view():
    if not LIVE_OK:
        st.error(f"Live SQL is off: {LIVE_ERR}. Install with `python -m pip install pyodbc`, then restart.")
        return
    labels = {s: f"{SRC[s][0]} {SRC[s][1]}" for s in SRC}
    a, b, c = st.columns([2, 1, 1])
    s = a.selectbox("Source", [""] + list(labels), key="sql_src", format_func=lambda v: labels.get(v, "Sidebar default connection"))
    b.number_input("Max rows", 1, 100000, 1000, key="sql_max", help="Rows beyond this are not returned")
    c.number_input("Timeout (s)", 5, 1800, 60, key="sql_timeout")
    server = str((ss.get(f"{s}_server") if s else "") or ss.get("cn_server") or "").strip()
    db = str((ss.get(f"{s}_database") if s else "") or ss.get("cn_db") or "").strip()
    tbl = str(ss.get(f"{s}_table") or "").strip() if s else ""
    st.caption(f"Server: {server or '—'} · Database: {db or '—'}" + (f" · Mapped table: {tbl}" if tbl else "")
               + " · Read-only: one SELECT / WITH statement.")
    t1, t2, t3, t4 = st.columns([2, 1.3, 1.3, 0.8])
    t1.selectbox("Template (built from mapped sources)", [""] + list(SQL_TPL), key="sql_tpl", format_func=lambda v: SQL_TPL.get(v, "None"))
    t2.text_input("Location label", key="sql_loc", help="ILD template: output column, blank to omit")
    t3.text_input("Projection version(s)", key="sql_ver", help="Comma separated; blank = all versions")
    t4.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    t4.button("Insert", on_click=sql_insert, disabled=not ss.get("sql_tpl") or not LIVE_OK)
    if ss.get("sql_tpl_err"):
        st.error(ss.sql_tpl_err)
    st.text_area("Query", key="sql_text", height=220, placeholder="SELECT TOP 100 * FROM schema.table")
    r1, r2 = st.columns([1, 4])
    go_ = r1.button("▶ Run query", type="primary", disabled=ss.creds is None or not (server and db))
    if ss.creds is None:
        r2.info("Connect to SQL Server in the sidebar first.")
    elif not (server and db):
        r2.info("Server and database are blank for this source. Set them in Live input setup or the sidebar.")
    if go_:
        try:
            sql, note = ss.sql_text, ""
            if live.MDM_TOKEN in str(sql or ""):
                with st.spinner("Pulling the MDM mapping…"):
                    sql, info = live.expand_maps(sql, get_cfg(True), ss.creds["user"], ss.creds["password"])
                note = (f"MDM mapping pulled from {info['server']}: {info['mdm_rows']:,} rows for {info['sa_upcs']:,} SA UPCs; "
                        f"{info['unmapped_upcs']:,} SA UPCs not found in MDM (dropped by the join).")
            res = live.run_query(server, db, ss.creds["user"], ss.creds["password"], sql, ss.sql_max, ss.sql_timeout)
            ss.sql_res = {**res, "label": f"{labels.get(s, 'Sidebar default')} · {server}/{db}", "note": note}
        except Exception as e:
            ss.sql_res = {"error": str(e)}
    res = ss.get("sql_res")
    if not res:
        return
    if res.get("error"):
        st.error(res["error"])
        return
    df = pd.DataFrame(res["rows"], columns=res["columns"])
    st.caption(f"{len(df):,} rows · {len(df.columns)} columns · {res['seconds']}s · {res['label']}")
    if res.get("note"):
        st.caption(res["note"])
    if res["truncated"]:
        st.warning(f"Showing the first {len(df):,} rows; more exist. Add a filter or raise Max rows.")
    st.dataframe(df, hide_index=True)
    st.download_button("⬇️ Result CSV", df.to_csv(index=False).encode("utf-8"), "sql_result.csv", "text/csv")


# ---------------- guide view ----------------
GUIDE, RUNBOOK = ROOT / "USER_GUIDE.md", ROOT / "QC_Gate_Manual_Fallback_Runbook.md"


def guide_view():
    if not GUIDE.exists():
        st.warning("USER_GUIDE.md was not found next to app.py.")
    else:
        txt = GUIDE.read_text(encoding="utf-8")
        d1, d2, _ = st.columns([1, 1.3, 3])
        d1.download_button("⬇️ User guide", txt.encode("utf-8"), "QC_Gate_User_Guide.md", "text/markdown")
        if RUNBOOK.exists():
            d2.download_button("⬇️ Manual fallback runbook", RUNBOOK.read_bytes(), RUNBOOK.name, "text/markdown")
        head, *secs = re.split(r"(?m)^## ", txt)
        st.markdown(head)
        for i, sec in enumerate(secs):
            title, _, body = sec.partition("\n")
            with st.expander(title.strip(), expanded=i == 0):
                st.markdown(body)
    stage_ord = {s: i for i, s in enumerate(core.STAGES)}
    rows = [{"Rule": r, "Stage": core.LBL.get(m.get("stage"), m.get("stage") or "Any source"), "Severity": SEV_TXT.get(m.get("severity"), m.get("severity")),
             "Enabled": "Yes" if m.get("enabled", True) is not False else "No", "Description": m.get("description", "")}
            for r, m in sorted(CATALOG["rules"].items(), key=lambda kv: (stage_ord.get(kv[1].get("stage"), 99), kv[0]))]
    with st.expander(f"📋 Check reference · {sum(x['Enabled'] == 'Yes' for x in rows)} enabled of {len(rows)} (from checks_input.json)"):
        st.dataframe(pd.DataFrame(rows), hide_index=True, column_config={"Description": st.column_config.TextColumn(width="large")})
        st.caption("Disabled rules are kept in the file for reference and are not run. Checks you add yourself in Live input setup are not listed here; they are saved with the setup.")


{NAV[0]: gate_view, NAV[1]: checks_view, NAV[2]: setup_view, NAV[3]: history_view, NAV[4]: sql_view, NAV[5]: guide_view}[view]()
