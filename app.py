"""QC Gate: live SQL Server input checks + Intelligent Suite JSON results in one release gate.
Run: python -m streamlit run app.py
"""
import datetime as dt
import html
import json
import re
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import qc_core as core
import qc_help

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
# Sidebar logo: an image with "circana" in its name (e.g. circana_logo.jpg) next to app.py, or in assets/. Nothing is shown if absent.
LOGO = next((p for d in (ROOT, ROOT / "assets") for p in sorted(d.glob("*circana*")) if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".svg")), None)

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

COLOR = {"PASS": "#0B6B4F", "FAIL": "#B42318", "MANUAL_REQUIRED": "#7A5200", "BLOCKED": "#4A36B8", "NONE": "#A1A1AC"}
PILL = {"FAIL": "p-f", "MANUAL_REQUIRED": "p-m", "BLOCKED": "p-b"}
STATUS_TXT = {"PASS": "🟢 Pass", "FAIL": "🔴 Fail", "MANUAL_REQUIRED": "🟠 Manual", "BLOCKED": "🟣 Blocked"}
SEV_TXT = {"CRITICAL": "Critical", "HIGH": "High", "WARNING": "Warning", "INFO": "Info"}
NAV = ["🚦 Gate", "🔎 Checks", "🧪 Live input setup", "📈 History", "🧮 SQL", "🧲 MSL", "📖 Guide"]
ICON = {"SA": "🛒", "IMP": "🧩", "PRIOR": "⏮️", "SHELFVAR": "📍", "MDM": "🏷️", "PROJ": "⚖️", "POS": "💳"}
SRC = {s: (ICON[s], lbl, roles) for s, (lbl, roles) in core.INPUTS.items()}
ROLE = core.ROLE
MULTI, TEXT = {"grain", "measures", "product_col"}, {"imputed_value"}
HINTS = CATALOG.get("column_hints") or {}
SERVERS = {"WIN0681": "10.106.10.51", "WIN0679": "10.106.10.49", "WIN0680": "10.106.11.180", "WIN0526": "10.106.11.211"}


def srv_opts(cur):
    """Server dropdown options: blank, the known servers, plus any other saved value so an older saved setup still loads."""
    return [""] + list(SERVERS) + ([cur] if cur and cur not in SERVERS else [])


def srv_fmt(blank):
    return lambda v: f"{v} · {SERVERS[v]}" if v in SERVERS else (v or blank)
HINT = {"SA": "Load columns fills exact name matches (store_number, shelf_feet, facings …). Confirm every mapping; an unmapped column turns its check Manual.",
        "IMP": "Filtered to the expected delivery ID. Imputed status null = collected (SA007), imputed value = imputed (SA008, SA009). Any server.",
        "PRIOR": "Prior wave SA table for SA009: imputed-store shelf feet, facings and capacity vs prior wave (store-level sums).",
        "MDM": "For additional checks run the Streamlit MDM validation tool: " + str((CATALOG["rules"].get("MDM005") or {}).get("url") or "https://mdm-validation-app.streamlit.app/"),
        "SHELFVAR": "Read-only lookup for SA005: SA location → shelf_var_id → description. Nothing is updated."}
TOP = {"cfg_report": "report_name", "cfg_expected": "expected_sa_stores", "cfg_delivery": "expected_delivery_id",
       "cfg_task": "expected_task", "cfg_weeks": "expected_weeks"}

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400&family=Roboto+Condensed:wght@400;700&display=swap');
html, body, [class*="css"]{font-family:'Roboto Condensed','Arial Narrow',sans-serif}
.block-container{padding-top:2.6rem;max-width:1440px}
header[data-testid="stHeader"]{background:transparent}

/* header row */
.qhead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin:0 0 8px}
.qhead .t{font-family:'Poppins',sans-serif;font-size:24px;color:#4E106F}
.qhead .m{color:#595967;font-size:13px}
.chip{display:inline-block;border-radius:999px;padding:3px 12px;font-size:13px;background:#E8E7ED;color:#595967}
.navlbl{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#595967;margin:0 0 2px}

/* grouped nav: pills, Circana Purple = active */
div[data-testid="stSegmentedControl"]{background:#E8E7ED;padding:4px;border-radius:999px;display:inline-flex}
div[data-testid="stSegmentedControl"] label{border-radius:999px!important;border:none!important;color:#000!important;font-weight:400!important}
div[data-testid="stSegmentedControl"] label[data-checked="true"],
div[data-testid="stSegmentedControl"] label[aria-checked="true"]{background:#4E106F!important;color:#fff!important;font-weight:700!important}

/* buttons: purple pill primary, outlined secondary */
.stButton>button[kind="primary"]{border-radius:999px;font-weight:700;border:none;background:#4E106F;color:#fff}
.stButton>button[kind="primary"]:hover{background:#6A5EFF;color:#fff}
.stButton>button:not([kind="primary"]){border-radius:999px;border:1px solid #4E106F;color:#4E106F}

/* key numbers: Roboto Condensed Bold */
[data-testid="stMetricValue"]{font-family:'Roboto Condensed',sans-serif;font-weight:700;font-size:2.6rem}
[data-testid="stMetricLabel"]{color:#595967}

/* verdict card: flat, bordered, blockers inside */
.hero{border:1px solid #D1D1D8;border-radius:12px;padding:24px 28px;background:#fff;margin-bottom:12px}
.hero .top{display:flex;flex-wrap:wrap;justify-content:space-between;gap:24px;margin-bottom:14px}
.eyebrow{color:#595967;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.verdict{font-family:'Poppins',sans-serif;font-size:40px;line-height:1.15;color:#000;margin-top:2px}
.hsub{color:#595967;margin-top:4px;font-size:15px}
.hmeta{text-align:right;color:#595967;font-size:13px;line-height:1.7}
.hmeta b{color:#000;font-weight:700}
.bl{display:grid;grid-template-columns:90px 140px 210px minmax(0,1fr);column-gap:16px;padding:10px 12px;border-bottom:1px solid #E8E7ED;font-size:15px;align-items:center}
.bl.h{background:#E8E7ED;border:none;border-radius:6px;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:#595967}
.pill{border-radius:999px;padding:3px 10px;font-size:13px;white-space:nowrap}
.p-f{background:#FDECEA;color:#B42318}.p-m{background:#FFF4DC;color:#7A5200}.p-b{background:#EEEBFF;color:#4A36B8}

.kn{font-size:42px;font-weight:700;line-height:1}.kl{font-size:14px;color:#595967;margin-top:4px}.kd{font-size:12px;color:#595967}
.nums{display:flex;flex-wrap:wrap;gap:36px}
.p-p{background:#E3F6EE;color:#0B6B4F}
.bar-row{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:13px;color:#595967}
.bar-row .bl2{width:170px;flex:none}.bar-row .bt{flex:1;background:#E8E7ED;border-radius:3px;height:16px}
.bar-row .bt i{display:block;height:100%;border-radius:3px}.bar-row b{width:80px;text-align:right;color:#000;flex:none}
.th{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:#595967;background:#E8E7ED;padding:8px 6px;border-radius:4px}
.rt{font-family:'Poppins',sans-serif;font-size:24px;line-height:1.2;margin-top:2px}
.msgbox{background:#E8E7ED;border-radius:8px;padding:12px 14px;font-size:14px;line-height:1.5}
.st-key-hero_card{padding:10px 14px}
[class*="st-key-row"]{border-bottom:1px solid #E8E7ED;padding:2px 0;gap:0!important}
[class*="st-key-rowsel"]{background:#F1EEFF;border-left:3px solid #6A5EFF}
[class*="st-key-row"] p{margin:0}
[class*="st-key-row"] button{border:none!important;background:transparent!important;padding:0!important;min-height:0!important;color:#000!important}
[class*="st-key-row"] button p{font-weight:700}
/* stage cards */
.grp{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px;color:#595967;text-transform:uppercase;letter-spacing:.06em;margin:4px 0}
.pipe{display:grid;grid-template-columns:repeat(8,1fr);gap:10px;margin:0 0 18px}
.stg{background:#fff;border:1px solid #D1D1D8;border-radius:10px;padding:12px 14px 14px;position:relative}
.stg::before{content:"";position:absolute;top:14px;left:14px;width:8px;height:8px;border-radius:50%;background:var(--c)}
.stg h4{margin:0 0 2px 18px;font-size:14px;font-weight:700;color:#000}
.stg .s{margin-left:18px;color:#000;font-size:13px}
.stg .n{margin-left:18px;color:#595967;font-size:12px;margin-top:2px}
.stg.none{background:#F7F6FA;border:1px dashed #A1A1AC}
.stg .bar{margin-left:18px;height:4px;background:#E8E7ED;border-radius:3px;margin-top:8px;overflow:hidden}
.stg .bar b{display:block;height:100%;background:var(--c)}

/* setup progress strip */
.steps{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:4px 0 14px}
.stp{border-top:4px solid #D1D1D8;padding-top:8px}
.stp.d{border-color:#4E106F}.stp.c{border-color:#FD0B95}
.stp .n{font-size:12px;color:#595967}.stp b{font-size:16px;display:block}

/* marquee: slim scrolling banner; pauses on hover, static when the OS asks for reduced motion */
.mq{overflow:hidden;white-space:nowrap;border-radius:8px;background:#E8E7ED;padding:7px 0;margin:0 0 12px;font-size:13px}
.mq span{display:inline-block;padding-left:100%;animation:mq 60s linear infinite}
.mq:hover span{animation-play-state:paused}
@keyframes mq{from{transform:translateX(0)}to{transform:translateX(-100%)}}
@media (prefers-reduced-motion:reduce){.mq span{animation:none;padding-left:12px}}

.empty{padding:48px;border-radius:12px;background:#F7F6FA;border:1px dashed #A1A1AC;text-align:center;color:#595967}
.empty h2{margin:0 0 6px;color:#000;font-family:'Poppins',sans-serif;font-weight:400}

[data-testid="stDataFrame"] thead tr th{background:#E8E7ED!important;color:#595967!important;font-size:11px!important;
 text-transform:uppercase;letter-spacing:.05em;font-weight:700!important;border-bottom:1px solid #D1D1D8!important}
[data-testid="stDataFrame"] tbody tr td{border-bottom:1px solid #E8E7ED!important}

@media (max-width:1100px){.pipe{grid-template-columns:repeat(4,1fr)}.hmeta{display:none}.steps{grid-template-columns:repeat(2,1fr)}.bl{grid-template-columns:70px 1fr}}
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
        *(f"{s}_{r}" for s, (_, _, roles) in SRC.items() for r in roles), "sql_src", "sql_text", "sql_tpl", "sql_loc", "sql_prod", "sql_pval", "msl_level", "msl_thr", "msl_rule", "msl_all_pf"]
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


@st.cache_resource
def job_registry():
    """Process-wide, so a refreshed browser session can find the run it started (the run id travels in the URL)."""
    return {}


class RunJob:
    """A live run in a background thread. Streamlit reruns the whole script on every click, which would stop a run made inline,
    so the run lives here and the page only reads its progress."""

    def __init__(self, user, password, cfg, est):
        self.id = uuid.uuid4().hex[:12]
        self.user, self.password, self.cfg, self.est = user, password, cfg, est
        self.n, self.last, self.run, self.error = 0, "Starting", None, None
        self.done = self.stop_flag = self.cancelled = False
        self.finished_at = None
        self.thread = threading.Thread(target=self._go, daemon=True)

    def _on_check(self, c):
        self.n += 1
        self.last = f"{c['rule']} · {STATUS_TXT.get(c['status'], c['status'])}"

    def _go(self):
        try:
            r = live.LiveRunner(self.user, self.password, CATALOG, self._on_check)
            r.stop = lambda: self.stop_flag
            self.run = r.run(self.cfg, RES_DIR)
        except live.Cancelled:
            self.cancelled = True
        except Exception as e:
            self.error = str(e)
        finally:
            self.password, self.finished_at, self.done = None, time.time(), True  # done is set last

    def start(self):
        reg = job_registry()
        for k in [k for k, v in reg.items() if v.done and time.time() - v.finished_at > 3600]:
            del reg[k]
        reg[self.id] = self
        self.thread.start()


def job_running():
    j = ss.get("job")
    return bool(j) and j.id not in ss.absorbed_jobs


def absorb_job():
    """Runs at the top of every rerun: re-attach to a run started before a refresh, and add a finished run to the gate once."""
    ss.setdefault("absorbed_jobs", set())
    jid = st.query_params.get("job")
    if "job" not in ss and jid:
        if jid in job_registry():
            ss.job = job_registry()[jid]
        else:
            st.query_params.pop("job", None)
            st.toast("The earlier live run was lost when the app restarted. Check results/ for a saved file, or run again.", icon="⚠️")
    j = ss.get("job")
    if not j or not j.done or j.id in ss.absorbed_jobs:
        return
    ss.absorbed_jobs.add(j.id)
    st.query_params.pop("job", None)
    if j.cancelled:
        st.toast("Live run stopped. Nothing was saved or added to the gate.", icon="⏹")
        return
    if j.error:
        st.toast(f"Live run failed: {j.error}", icon="❌")
        return
    add_run(j.run)
    nb = sum(core.blocks(c) for c in j.run["checks"])
    st.toast(f"Live run {j.run['run_id']} done: {len(j.run['checks'])} checks, {nb} blocking. Saved results/{j.run.get('saved_as')}", icon="✅")
    if ss.get("nav") == NAV[2]:  # only move the user to the Gate if they are still on the setup page
        ss.nav = NAV[0]


absorb_job()


# ---------------- sidebar ----------------
with st.sidebar:
    if LOGO:
        st.image(str(LOGO), width=260)
    with st.expander("🔌 SQL Server", expanded=ss.creds is None):
        if not LIVE_OK:
            st.warning(f"Live SQL off: {LIVE_ERR}. Run `python -m pip install pyodbc`.")
        st.radio("Authentication", ["Windows", "SQL login"], key="cn_auth", horizontal=True,
                 help="Windows uses the account you are signed in to this computer with. No login or password is entered or stored.")
        win = ss.get("cn_auth", "Windows") == "Windows"
        st.selectbox("Default server", srv_opts(ss.get("cn_server") or ""), key="cn_server", format_func=srv_fmt("Select server"))
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

    if job_running():
        def _progress():  # refreshes itself every second without rerunning the page, so the user can switch tabs
            j = ss.job
            st.markdown("#### ⏳ Live run")
            st.progress(1.0 if j.done else min(j.n / max(j.est, 1), 0.99), text=f"{j.n} checks done · {j.last}")
            if j.done:
                st.rerun()
            st.caption("Stopping after the current check…" if j.stop_flag else "Keeps running if you switch tabs or refresh. It opens on the Gate when finished.")
            st.button("⏹ Stop run", key="stop_job", disabled=j.stop_flag, on_click=lambda: setattr(j, "stop_flag", True))
        st.fragment(_progress, run_every=1)()

MARQUEE = [str(m).strip() for m in (CATALOG.get("marquee") or []) if str(m).strip()]
if MARQUEE:
    _t = "  ·  ".join(html.escape(m) for m in MARQUEE)
    st.markdown(f'<div class="mq"><span style="animation-duration:{max(25, round(len(_t) * 0.28))}s">{_t}</span></div>', unsafe_allow_html=True)

_hl, _hj = get_run(ss.live_runs, ss.sel_live), get_run(ss.is_runs, ss.sel_is)
_hr = _hl or _hj
_hm = " · ".join([str((_hr or {}).get("report_name") or "No report loaded")] + ([f"Live run {_hl['run_id']}"] if _hl else []) + ([f"IS run {_hj['run_id']}"] if _hj else []))
_cn = f'<span class="chip">Connected · {html.escape(str(ss.creds.get("auth", "SQL login")))}</span>' if ss.creds else '<span class="chip">Not connected</span>'
st.markdown(f'<div class="qhead"><div style="display:flex;align-items:baseline;gap:14px"><span class="t">QC Gate</span>'
            f'<span class="m">{html.escape(_hm)}</span></div>{_cn}</div>', unsafe_allow_html=True)

NAV_GROUPS = (("Review", [NAV[0], NAV[1], NAV[3]]), ("Build", [NAV[2], NAV[4], NAV[5]]), ("Reference", [NAV[6]]))


def _pick(k):
    if ss.get(k):
        ss.nav = ss[k]


if ss.get("nav") not in NAV:
    ss.nav = NAV[0]
for _i, (_c, (_lbl, _opts)) in enumerate(zip(st.columns([3.1, 3.4, 1.2]), NAV_GROUPS)):
    ss[f"nav_{_i}"] = ss.nav if ss.nav in _opts else None  # one active pill across the three groups
    _c.markdown(f'<div class="navlbl">{_lbl}</div>', unsafe_allow_html=True)
    _c.segmented_control(_lbl, _opts, key=f"nav_{_i}", label_visibility="collapsed", on_change=_pick, args=(f"nav_{_i}",))
view = ss.nav


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
        nb, p = sum(core.blocks(c) for c in cs), pm[s]
        tier = core.certify(cs, p)
        own = html.escape(p["owner"]) if p["owner"] else "No owner"
        slo = f'<div class="n">SLO: {html.escape(p["slo"])}</div>' if p["slo"] else ""
        cells.append(f'<div class="stg" style="--c:{COLOR[worst]}"><h4>{core.LBL[s]}</h4><div class="s">{tier or "Not certified"}</div>'
                     f'<div class="n">{f"{nb} blocking" if nb else own}</div>{slo}</div>')
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
    with st.expander("📦 Data products · each stage as a dataset", expanded=True):
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
    with st.expander("💬 Discussions", expanded=True):
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
    with st.expander("🔍 Diagnostics · schema changes since the last live run", expanded=True):
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
    with st.expander("📐 Quality dimensions", expanded=True):
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True)
            st.caption("Worst status per dimension. – means no mapped check. Mapping: checks_input.json → dimensions. IS JSON rules are not mapped.")
            det = core.dim_detail(x["checks"], CATALOG.get("dimensions"))
            if det:
                st.markdown("###### Checks behind the non-green cells")
                st.dataframe(pd.DataFrame([{"Stage": core.LBL.get(d["stage"], d["stage"]), "Dimension": d["dimension"].capitalize(), "Rule": d["rule"],
                                            "Status": STATUS_TXT.get(d["status"], d["status"]), "Severity": SEV_TXT.get(d["severity"], d["severity"]),
                                            "Blocks": "⛔" if core.blocks({"status": d["status"], "severity": d["severity"]}) else "",
                                            "Message": d["message"]} for d in det]),
                             hide_index=True, column_config={"Message": st.column_config.TextColumn(width="large")})
                st.caption("One row per non-passing check, grouped by the cell it turns non-green. ⛔ = blocks release. Open Checks and search the rule for the full message.")
            else:
                st.caption("Every mapped check passed.")
        else:
            st.caption("No mapped checks in this run.")


def hero(x, cn, dl):
    L, J = x["L"], x["J"]
    if J:
        title = "Do not release" if x["fail"] else "Release with caveats" if x["warn"] else "Release"
    else:
        title = "Fix inputs before processing" if x["fail"] else "Inputs ready to process"
    sub = f'{len(x["blocking"])} blocking check(s)' + (" · Delivery stage has no checks" if x["need_del"] else "")
    if J and (J.get("summary") or {}).get("delivery_recommendation"):
        sub += f' · IS file says {J["summary"]["delivery_recommendation"]}'
    if L and J and L.get("report_name") != J.get("report_name"):
        sub += ' · <span style="color:#B42318">Live and IS runs are different reports</span>'

    def num(label, val, k, red=False):
        d = dl(k)
        return (f'<div><div class="kn"{" style=color:#B42318" if red and val else ""}>{val}</div><div class="kl">{label}</div>'
                + (f'<div class="kd">{d:+d} vs last run</div>' if d else "") + '</div>')
    nums = (num("Checks", cn["total_checks"], "total_checks") + num("Passed", cn["passed"], "passed") + num("Blocking", cn["blocking"], "blocking", True)
            + num("Manual or blocked", cn["manual_required"] + cn["blocked"], "manual_required") + num("Suspect passes", cn["suspect"], "suspect"))
    bl = x["blocking"][:5]
    with st.container(border=True, key="hero_card"):
        st.markdown(f'<div class="top"><div><div class="eyebrow">Release decision</div><div class="verdict">{title}</div><div class="hsub">{sub}</div></div>'
                    f'<div class="nums">{nums}</div></div>', unsafe_allow_html=True)
        if not bl:
            st.markdown('<div class="hsub">Nothing blocks release.</div>', unsafe_allow_html=True)
            return
        h, b = st.columns([3, 1.1])
        h.markdown('<div style="font-size:15px;font-weight:700;padding-top:6px">What blocks release</div>', unsafe_allow_html=True)
        b.button("Open all blockers in Checks", type="primary", on_click=go_blockers)
        rows = "".join(f'<div class="bl"><b>{html.escape(str(c.get("rule")))}</b><span>{html.escape(core.LBL.get(c["stage"], c["stage"]))}</span>'
                       f'<span><span class="pill {PILL.get(c["status"], "p-f")}">{html.escape(qc_help._why(c))}</span></span>'
                       f'<span style="color:#595967">{html.escape(qc_help._first(c))}</span></div>' for c in bl)
        more = (f'<div style="padding:8px 12px;color:#595967;font-size:13px">… and {len(x["blocking"]) - len(bl)} more in Checks</div>'
                if len(x["blocking"]) > len(bl) else "")
        st.markdown(f'<div class="bl h"><span>Rule</span><span>Stage</span><span>Why it blocks</span><span>Message</span></div>{rows}{more}', unsafe_allow_html=True)


def go_blockers():
    ss.nav_next = NAV[1]
    ss.f_b = True


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
    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**Store count through the pipeline**")
        if x["trace"]:
            mx, pal = max(n for _, n in x["trace"]) or 1, ["#4E106F", "#6A5EFF", "#FD0B95", "#FF8275"]
            st.markdown("".join(f'<div class="bar-row"><div class="bl2">{html.escape(sn)}</div><div class="bt"><i style="width:{100 * n / mx:.1f}%;background:{pal[i % 4]}"></i></div>'
                                f'<b>{core.fmt(n)}</b></div>' for i, (sn, n) in enumerate(x["trace"])), unsafe_allow_html=True)
            st.caption("Stages should keep or shrink the store count. Growth between steps usually means join fan-out or a different source table.")
        else:
            st.info("Store counts appear once a live SA run or an IS JSON with SA/Prep/Staging checks is loaded.")
    with b.container(border=True):
        st.markdown("**Where the checks landed**")
        tot = max(len(x["checks"]), 1)
        st.markdown("".join(f'<div class="bar-row"><div class="bl2" style="width:110px">{lbl}</div><div class="bt"><i style="width:{100 * n / tot:.1f}%;background:{COLOR[k]}"></i></div><b>{n}</b></div>'
                            for lbl, k in (("Pass", "PASS"), ("Fail", "FAIL"), ("Manual", "MANUAL_REQUIRED"), ("Blocked", "BLOCKED"))
                            for n in [sum(c["status"] == k for c in x["checks"])]), unsafe_allow_html=True)
        st.caption("Open Checks to drill into a stage or rule.")


def gate_view():
    L, J = get_run(ss.live_runs, ss.sel_live), get_run(ss.is_runs, ss.sel_is)
    x = core.build(L, J, ss.live_runs, ss.is_runs, LIVE_OK)
    if not x:
        st.markdown('<div class="empty"><h2>🚦 Nothing on the gate yet</h2><p>Run live input checks from <b>Live input setup</b>, '
                    'or drop an Intelligent Suite <code>qc_report_*.json</code> in the sidebar. Both land here as one release decision.</p></div>',
                    unsafe_allow_html=True)
        return
    gk = (ss.sel_live, ss.sel_is)
    if not x["fail"] and not x["warn"] and gk not in ss.celebrated:
        ss.celebrated.add(gk)
        st.balloons()

    pl, pj = (core.prev(ss.live_runs, L) if L else None), (core.prev(ss.is_runs, J) if J else None)
    px_ = core.build(pl or L, pj or J, ss.live_runs, ss.is_runs, LIVE_OK) if (pl or pj) else None
    cn, pc = x["counts"], (px_["counts"] if px_ else None)
    dl = lambda k: (cn[k] - pc[k]) if pc else None
    hero(x, cn, dl)

    strip = st.container()  # drawn after the editor runs so owner edits show on the same rerun
    charts(x)
    DET = {"prod": "Data products", "dims": "Quality dimensions", "schema": "Schema changes", "disc": "Discussions",
           "integ": f"Report integrity · {len(x['issues'])} finding(s)"}
    st.markdown('<div class="navlbl">Details</div>', unsafe_allow_html=True)
    pick = st.pills("Details", list(DET), key="gate_det", format_func=DET.get, label_visibility="collapsed")
    if pick == "prod":
        owners_view(x)
    with strip:
        stage_strip(x)
    if pick == "dims":
        dims_view(x)
    elif pick == "schema":
        diagnostics_view(x)
    elif pick == "disc":
        discussions_view(x)
    elif pick == "integ":
        with st.container(border=True):
            for i in x["issues"]:
                st.markdown(f"- {i}")
            if J:
                s, r1, r2 = J.get("summary") or {}, core.recount(J["checks"]), core.recount(L["checks"]) if L else {}
                rows = [{"Count": k.replace("_", " ").capitalize(), "IS file": s.get(k), "IS recount": r1[k], "Live": r2.get(k)}
                        for k in ("total_checks", "passed", "failed", "critical_failures", "manual_required", "blocked", "critical_unresolved")]
                st.dataframe(pd.DataFrame(rows), hide_index=True)


# ---------------- checks view ----------------
def ckey(c):
    return f"{c['source']}|{c['check_id']}"


def pick_check(k):
    ss.sel_chk = k


def checks_view():
    L, J = get_run(ss.live_runs, ss.sel_live), get_run(ss.is_runs, ss.sel_is)
    x = core.build(L, J, ss.live_runs, ss.is_runs, LIVE_OK)
    if not x:
        st.info("No checks yet. Run live input checks or load an Intelligent Suite JSON from the sidebar.")
        return
    df = to_df(x)
    f1, f2, f3, f4 = st.columns([1.1, 2.4, 1.4, 2])
    srcs = f1.pills("Source", [core.SRC_LBL[x_] for x_ in ("LIVE", "IS", "CROSS") if x_ in set(df["_src"])], selection_mode="multi", key="f_src")
    present_stages = sorted(set(df["_stage"]), key=lambda v: core.STAGE_ORDER.get(v, len(core.STAGES)))
    stgs = f2.pills("Stage", [core.LBL.get(s_, s_) for s_ in present_stages], selection_mode="multi", key="f_stage")
    sev = f3.pills("Severity", list(SEV_TXT.values()), selection_mode="multi", key="f_sev")
    sts = f4.pills("Status", list(STATUS_TXT.values()), selection_mode="multi", key="f_st")
    g1, g2, g3 = st.columns([1, 1.4, 2])
    only_b = g1.toggle("Only blockers", key="f_b")
    only_c = g2.toggle("Changed since last run", key="f_c", disabled=not any(x["diff"].values()))
    txt = g3.text_input("Search", placeholder="Search rule, column or message", key="f_txt", label_visibility="collapsed")
    v = df
    if srcs: v = v[v["Source"].isin(srcs)]
    if stgs: v = v[v["Stage"].isin(stgs)]
    if sts: v = v[v["Status"].isin(sts)]
    if sev: v = v[v["Severity"].isin(sev)]
    if only_b: v = v[v["_b"]]
    if only_c: v = v[v["Change"] != ""]
    if txt: v = v[(v["Rule"].fillna("") + " " + v["Message"]).str.contains(re.escape(txt), case=False)]
    n_show = int(ss.get("chk_n", 30))
    st.caption(f"{min(len(v), n_show)} of {len(v)} shown ({len(df)} checks in the run) · select a row for detail")
    tcol, dcol = st.columns([1.5, 1])
    W = [0.35, 0.95, 1.5, 1.2, 0.9, 4]
    with tcol:
        for col, t in zip(st.columns(W), ["", "Rule", "Stage", "Status", "Severity", "Message"]):
            col.markdown(f'<div class="th">{t}</div>', unsafe_allow_html=True)
        for _, r in v.head(n_show).iterrows():
            c = x["checks"][int(r["_i"])]
            on = ckey(c) == ss.get("sel_chk")
            with st.container(key=f"row{'sel' if on else ''}_{int(r['_i'])}"):
                cc = st.columns(W)
                cc[0].markdown("⛔" if core.blocks(c) else "⚠️" if core.suspect(c) else "")
                cc[1].button(str(c.get("rule")), key=f"pick_{int(r['_i'])}", on_click=pick_check, args=(ckey(c),))
                cc[2].markdown(core.LBL.get(c["stage"], c["stage"]))
                cc[3].markdown(f'<span class="pill {PILL.get(c["status"], "p-p")}">{STATUS_TXT.get(c["status"], c["status"])[2:]}</span>', unsafe_allow_html=True)
                cc[4].markdown(SEV_TXT.get(c.get("severity"), c.get("severity")) or "")
                cc[5].markdown(f'<span style="color:#595967">{html.escape(r["Message"])}</span>', unsafe_allow_html=True)
        if len(v) > n_show:
            st.button(f"Show {min(30, len(v) - n_show)} more", on_click=lambda: ss.update(chk_n=n_show + 30))
    with dcol:
        c = next((k for k in x["checks"] if ckey(k) == ss.get("sel_chk")), None)
        if c is not None:
            with st.container(border=True):
                msg = str(c.get("message") or "").splitlines() or [""]
                st.markdown(f'<div class="eyebrow">Selected check</div><div class="rt">{html.escape(str(c.get("rule")))}</div>'
                            f'<div style="margin:6px 0"><span class="pill {PILL.get(c["status"], "p-p")}">{STATUS_TXT.get(c["status"], c["status"])[2:]} · '
                            f'{SEV_TXT.get(c.get("severity"), c.get("severity"))}{" · blocks release" if core.blocks(c) else ""}</span></div>'
                            f'<div style="font-size:15px">{html.escape(str(x["desc"].get(c.get("rule"), "No description")))}</div>'
                            f'<div class="kd" style="margin-top:4px">{core.SRC_LBL[c["source"]]} · {core.LBL.get(c["stage"], c["stage"])} · affected {float(c.get("affected_pct") or 0):.1f}%'
                            f'{" · " + x["diff"][c["check_id"]] if x["diff"].get(c["check_id"]) else ""} · <code>{html.escape(c["check_id"])}</code></div>'
                            f'<div style="display:flex;gap:32px;margin:12px 0"><div><div class="kd">Actual</div><div style="font-size:32px;font-weight:700;line-height:1.1">{html.escape(core.fmt(c.get("actual_value")) or "n/a")}</div></div>'
                            f'<div><div class="kd">Expected</div><div style="font-size:32px;font-weight:700;line-height:1.1">{html.escape(core.fmt(c.get("expected_value")) or "n/a")}</div></div></div>'
                            f'<div class="msgbox"><b>{html.escape(msg[0])}</b>' + "".join(f'<div style="color:#595967">{html.escape(m_)}</div>' for m_ in msg[1:]) + '</div>',
                            unsafe_allow_html=True)

                pts = core.trend(c["check_id"], (J or L).get("report_name"), ss.live_runs + ss.is_runs)
                if len(pts) > 1:
                    st.markdown("History")
                    tf = pd.DataFrame(pts, columns=["Run", "Value"])
                    fig = px.line(tf, x="Run", y="Value", markers=True, color_discrete_sequence=["#4E106F"])
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

                pf = c.get("profile")
                if pf:
                    st.markdown(f"Distinct values in **{pf['column']}** (null excluded): {pf['distinct_count']:,} · {pf['null_rows']:,} null rows"
                                + (f" · first {len(pf['values']):,} shown" if pf.get("truncated") else ""))
                    pdf_ = pd.DataFrame(pf["values"])
                    st.dataframe(pdf_, hide_index=True)
                    dv_name = re.sub(r"\W+", "_", str(pf["column"]))
                    st.download_button("⬇️ Distinct values CSV", pdf_.to_csv(index=False).encode("utf-8"),
                                       f"distinct_{dv_name}.csv", "text/csv", key=f"dv_{c['check_id']}")

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
        else:
            st.caption("Select a row in the table to see its detail.")

    exc = df[(df["_st"] != "PASS") | df["Blocks"].eq("⚠️ suspect")].drop(columns=[k for k in df.columns if k.startswith("_")])
    name = re.sub(r"\W+", "_", str((J or L).get("report_name") or "report"))
    prof_rows = [{"Stage": core.LBL.get(c["stage"], c["stage"]), "Column": c["profile"]["column"], "Value": v["value"], "Row count": v["row_count"]}
                 for c in x["checks"] if c.get("profile") for v in c["profile"]["values"]]
    d1, d2, d3, _ = st.columns([1, 1, 1.3, 2.7])
    if prof_rows:
        d3.download_button("⬇️ Distinct values CSV (all columns)", pd.DataFrame(prof_rows).to_csv(index=False).encode("utf-8"),
                           f"qc_distinct_values_{name}.csv", "text/csv", key="dv_all")
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
            v = o.get(r) or ""
            ss[f"{s}_{r}"] = ([v] if isinstance(v, str) and v else [] if isinstance(v, str) else list(v)) if r in MULTI else v
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
        if r in TEXT or ss.get(f"{s}_{r}"):
            continue
        hit = next((low[h.lower()] for h in HINTS.get(r, []) if h.lower() in low), None)
        if hit:
            ss[f"{s}_{r}"] = [hit] if r in MULTI else hit


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

    steps_c = st.container()  # progress strip, filled once readiness is known

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
    main_c, top = st.columns([3, 1.25])  # source tabs left, readiness rail right
    ready = st.container()  # full-width readiness and your-own-checks tables below

    # 3. One tab per source (static labels: a changing label would reset the selected tab)
    tabs = main_c.tabs([f"{icon} {label}" for icon, label, _ in SRC.values()])
    for tab, (s, (icon, label, roles)) in zip(tabs, SRC.items()):
        with tab:
            cols = ss.cols.get(s, [])
            if s in HINT:
                st.caption(HINT[s])
            need = list(dict.fromkeys(m for r in core.REQ if (CATALOG["rules"].get(r) or {}).get("enabled", True) is not False
                                      and r not in core.not_applicable(cfg)
                                      for m in core.missing_for(r, cfg) if m.startswith(core.INPUTS[s][0])))
            st.caption("✅ Everything the enabled checks need from this source is mapped." if not need else "🟠 Needs: " + "; ".join(need))
            x1, x2, x3, x4 = st.columns([1.1, 1.1, 2.2, 0.9])
            x1.selectbox("Server", srv_opts(ss.get(f"{s}_server") or ""), key=f"{s}_server", format_func=srv_fmt("Default server"))
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
        na_ = core.not_applicable(cfg)
        for r in core.REQ:
            meta = CATALOG["rules"].get(r) or {}
            if meta.get("enabled", True) is False:
                continue
            if r in na_:  # optional source not entered (e.g. no prior wave): not Manual, not blocking
                rd.append({"Run": r not in off, "Check": r, "Kind": "Spec check", "Severity": SEV_TXT.get(meta.get("severity"), meta.get("severity")),
                           "Description": meta.get("description", ""), "Applies to": core.LBL.get(meta.get("stage"), ""),
                           "Status": "⏭ Not applicable", "Needs": na_[r]})
                continue
            miss = core.missing_for(r, cfg)
            rd.append({"Run": r not in off, "Check": r, "Kind": "Spec check", "Severity": SEV_TXT.get(meta.get("severity"), meta.get("severity")),
                       "Description": meta.get("description", ""), "Applies to": core.LBL.get(meta.get("stage"), ""),
                       "Status": "🟠 Manual" if miss else "✅ Ready", "Needs": "; ".join(miss)})
        nr, ns = sum(x["Status"] == "✅ Ready" for x in rd), sum(x["Status"] in ("✅ Ready", "🟠 Manual") for x in rd)
        crit = sum(x["Status"] == "🟠 Manual" and x["Severity"] in ("Critical", "High") for x in rd)
        for r in core.GEN:  # input checks: run on every mapped source, so they are shown with what they will test
            meta = CATALOG["rules"].get(r) or {}
            if meta.get("enabled", True) is False:
                continue
            runs = core.generic_runs(r, cfg)
            rd.append({"Run": r not in off, "Check": r, "Kind": "Input check", "Severity": SEV_TXT.get(meta.get("severity"), meta.get("severity")),
                       "Description": meta.get("description", ""),
                       "Applies to": "; ".join(f"{core.LBL[s]}: {', '.join(c)}" for s, c in runs.items()) or "No source mapped for it yet",
                       "Status": "✅ Will run" if runs else "⏭ Not run", "Needs": ""})
        st.markdown('<div class="navlbl">Check readiness</div>', unsafe_allow_html=True)
        r1, r2 = st.columns(2)
        r1.metric("Ready", nr)
        r2.metric("Would run as Manual", ns - nr)
        if crit:
            st.error(f"⛔ {crit} Critical or High check(s) will block release until mapped")
        st.markdown("**Next to map, blockers first**")
        st.markdown("\n".join(qc_help.map_answer({"cfg": cfg, "catalog": CATALOG, "connected": ss.creds is not None})[:7]))
        go_ = st.button("▶ Run live checks", type="primary", disabled=ss.creds is None or job_running(), use_container_width=True)
        save_ = st.button("💾 Save setup", use_container_width=True)
        if ss.creds is None:
            st.info("Connect to SQL Server in the sidebar to load columns and run checks.")
        elif job_running():
            st.info("A live run is in progress (see the sidebar). You can switch tabs; it keeps running.")
        flags = [ss.creds is not None, bool(cfg["report_name"] and (cfg["expected_delivery_id"] or cfg["expected_task"] or cfg["expected_weeks"])),
                 nr == ns, nr == ns and not off, bool(ss.sel_live)]
        cur = next((i for i, f in enumerate(flags) if not f), len(flags))
        cells = "".join(f'<div class="stp {"d" if f else "c" if i == cur else ""}"><div class="n">Step {i + 1}</div><b>{n}</b>'
                        f'<div class="n">{"Done" if f else "In progress" if i == cur else "Waiting"}</div></div>'
                        for i, (n, f) in enumerate(zip(["Connect", "Expected values", "Map sources", "Readiness", "Run"], flags)))
        with steps_c:
            st.markdown(f'<div class="steps">{cells}</div>', unsafe_allow_html=True)
        with ready.expander(f"✅ Check readiness: {nr} of {ns} spec checks ready" + (f" · {crit} Critical or High will block as Manual" if crit else ""), expanded=nr < len(rd)):
            st.caption("**Spec checks** (SA, MDM, PRJ, POS) need specific columns: if one is not mapped the check runs as **Manual**. "
                       "**Input checks** (IN-001 table has rows, IN-002 key columns not null, IN-005 no negative measures, PRJ-001 projection weights) "
                       "run automatically on every source that has something mapped for them; **Applies to** shows the sources and columns each one will test. "
                       "A source with nothing mapped is skipped, never Manual. IN-000 (server version, Info) always runs. "
                       "Untick **Run** to switch a check off for this run: it then shows as Manual, does not block release, and goes in the release notes.")
            if st.button("Reset: run all checks"):
                ss.rules_off = []
                ss.pop("ready_ed", None)
                st.rerun()
            ed = st.data_editor(pd.DataFrame(rd), hide_index=True, key="ready_ed", height=min(36 * len(rd) + 40, 520),
                                disabled=["Check", "Kind", "Severity", "Description", "Applies to", "Status", "Needs"],
                                column_config={"Run": st.column_config.CheckboxColumn("Run", help="Untick to switch a check off for this run"),
                                               "Description": st.column_config.TextColumn(width="large"),
                                               "Applies to": st.column_config.TextColumn(width="large")})
            ss.rules_off = [r for r, on_ in zip(ed["Check"], ed["Run"]) if not on_]
            if ss.rules_off:
                st.warning(f"{len(ss.rules_off)} check(s) switched off for this run: {', '.join(ss.rules_off)}. They show as Manual, do not block release, "
                           "and must go in the release notes.")
            dis = [r for r in core.REQ if (CATALOG["rules"].get(r) or {}).get("enabled", True) is False]
            if dis:
                st.caption("Disabled in checks_input.json, not listed: " + ", ".join(dis))
            st.markdown("###### ➕ Your own checks")
            st.caption("Add a row to create a check on a mapped source. Fill Source, Test, Column and Severity; Value / SQL depends on the test. "
                       "A blank ID becomes CUS-001, CUS-002 …\n\n"
                       "- **Not null**: Column `facings`, Value blank\n"
                       "- **Unique**: Column `store_number,cdm_geo` (comma separated), Value blank\n"
                       "- **Allowed values**: Column `side`, Value `L,R`\n"
                       "- **Range**: Column `facings`, Value `0,50` (min,max, either may be blank)\n"
                       "- **Custom SQL**: Column blank, Value a SELECT that returns the failing rows, e.g. "
                       "`SELECT store_number FROM <schema.table> WHERE facings > 0 AND shelf_feet = 0`\n\n"
                       "Column names must exist in the source table. Examples are illustrations: use your own columns and values. "
                       "A CRITICAL or HIGH row that fails or cannot run blocks release.")
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
            ss.job = RunJob(ss.creds["user"], ss.creds["password"], cfg, estimate(cfg))
            ss.job.start()
            st.query_params["job"] = ss.job.id
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
        f1 = px.line(h, x="Run", y="Pass rate %", color="Source", markers=True, color_discrete_sequence=["#4E106F", "#FD0B95"])
        f2 = px.bar(h, x="Run", y="Blocking", color="Source", barmode="group", color_discrete_sequence=["#4E106F", "#6A5EFF"])
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
        ss.sql_text = live.ild_sql(get_cfg(True), ss.sql_tpl, ss.get("sql_loc") or "",
                                   product=ss.get("sql_prod") or "", product_vals=ss.get("sql_pval") or "")
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
    c.number_input("Timeout (s)", 5, 3600, 600, key="sql_timeout", help="Query timeout. Large Shelf audit and projection joins can take several minutes")
    server = str((ss.get(f"{s}_server") if s else "") or ss.get("cn_server") or "").strip()
    db = str((ss.get(f"{s}_database") if s else "") or ss.get("cn_db") or "").strip()
    tbl = str(ss.get(f"{s}_table") or "").strip() if s else ""
    st.caption(f"Server: {server or '—'} · Database: {db or '—'}" + (f" · Mapped table: {tbl}" if tbl else "")
               + " · Read-only: one SELECT / WITH statement.")
    prods = list(get_cfg(True)["sources"]["MDM"].get("product_col") or [])
    if ss.get("sql_prod") not in prods:
        ss.pop("sql_prod", None)
    t1, t2, t3, t4, t5 = st.columns([2, 1.3, 1.5, 1.5, 0.8])
    t1.selectbox("Template (built from mapped sources)", [""] + list(SQL_TPL), key="sql_tpl", format_func=lambda v: SQL_TPL.get(v, "None"))
    t2.selectbox("Product", prods, key="sql_prod", help="Product column mapped on the Product master. The ILD template groups by it")
    t3.text_input("Product value(s)", key="sql_pval", help="Optional filter on the chosen Product column. Comma separated. Blank = all products")
    t4.text_input("Location", key="sql_loc", help="Optional filter on the Shelf audit location column (matched to the Shelf var master description when mapped). "
                  "Also the Location output column. Blank = all locations, no column")
    t5.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    t5.button("Insert", on_click=sql_insert, disabled=not ss.get("sql_tpl") or not LIVE_OK)
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
                    sql, info = live.expand_maps(sql, get_cfg(True), ss.creds["user"], ss.creds["password"], product=ss.get("sql_prod") or None)
                note = (f"MDM mapping pulled from {info['server']}: {info['mdm_rows']:,} rows for {info['sa_upcs']:,} SA UPCs; "
                        f"{info['unmapped_upcs']:,} SA UPCs not found in MDM (dropped by the join).")
            res = live.run_query(server, db, ss.creds["user"], ss.creds["password"], sql, ss.sql_max, ss.sql_timeout)
            ss.sql_res = {**res, "label": f"{labels.get(s, 'Sidebar default')} · {server}/{db}", "note": note}
        except Exception as e:
            ss.sql_res = {"error": str(e) + (f"\n\nThe query ran longer than Timeout (s) = {ss.get('sql_timeout')}. Raise Timeout (s), or add Product value(s) / Location to cut the rows." if "HYT00" in str(e) else "")}
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


# ---------------- MSL view ----------------
MSL_COUNTS = [("sa_stores", "SA stores (distinct, delivery filter applied)"), ("sa_rows", "SA rows (delivery filter applied)"), ("sa_rows_no_mdm", "SA rows with no MDM match (dropped by the join)"),
              ("mdm_dup_upcs", "UPCs repeated in MDM (fan-out)"), ("after_mdm_join", "Rows after the MDM join"),
              ("null_product", "Joined rows with no value at the chosen Product"), ("null_facings", "Joined rows with null facings"),
              ("null_location", "Joined rows with a null location column"), ("rows_used", "Rows used for MSL")]


def msl_view():
    if not LIVE_OK:
        st.error(f"Live SQL is off: {LIVE_ERR}. Install with `python -m pip install pyodbc`, then restart.")
        return
    cfg = get_cfg(True)
    S, M = cfg["sources"]["SA"], cfg["sources"]["MDM"]
    levels = list(M.get("product_col") or [])
    if ss.get("msl_level") not in levels:
        ss.pop("msl_level", None)
    a, b, c = st.columns([1.4, 1, 2.2])
    level = a.selectbox("Product", levels, key="msl_level", help="Product columns mapped on the Product master (category, segment, sub brand, brand …)")
    thr = b.selectbox("Threshold", core.MSL_THRESHOLDS, key="msl_thr", format_func=lambda v: f"{v}%")
    rule = c.selectbox("Rule", list(core.MSL_RULES), key="msl_rule")
    miss = core.msl_missing(cfg, rule, level or "")
    d = cfg.get("expected_delivery_id")
    st.caption(f"Shelf audit: {S.get('table') or '—'} · Product master: {M.get('table') or '—'} · Basis: facings · "
               + (f"SA filtered to delivery {d}" if S.get("delivery_col") and d else "SA not filtered by delivery (map the SA delivery ID column and enter the expected delivery ID)"))
    if miss:
        st.warning("Set in Live input setup: " + "; ".join(miss))
    with st.expander("📘 How this rule works (worked example)"):
        ex = core.msl_example(rule, thr)
        grain = ", ".join(core.MSL_EX_LBL[r].lower() for r in core.MSL_RULES[rule])
        st.markdown(f"**{rule}** adds up the product's facings for each store and **{grain}**. The grouping with the most facings is the store's dominant location. "
                    f"Share = 100 × dominant facings ÷ the product's total facings in that store. The store passes when the share is **{thr}% or more**.")
        st.caption("Illustrative numbers for one product (X), not client data. Facings by physical position:")
        st.dataframe(pd.DataFrame(core.MSL_EX_BASE, columns=core.MSL_EX_COLS), hide_index=True)
        for e in ex:
            lines = " · ".join(f"{g}: {f}" for g, f in e["groups"])
            st.markdown(f"**Store {e['store']}** → {lines} · total {e['total']}  \n"
                        f"Dominant: {e['dominant']} ({e['dom_fac']}) → 100 × {e['dom_fac']} ÷ {e['total']} = **{e['share']:.1f}%** → "
                        + ("✅ passes" if e["passes"] else "❌ fails") + f" at {thr}%")
        np_ = sum(e["passes"] for e in ex)
        st.markdown(f"Roll-up: **{np_} of {len(ex)} stores** at or above {thr}%. Ties on facings go to the lowest location value.")
        cmp_ = pd.DataFrame([{"Rule": r.split(": ", 1)[-1], **{f"Store {e['store']} share %": round(e["share"], 1) for e in core.msl_example(r, thr)}}
                             for r in core.MSL_RULES])
        st.markdown("Same stores under every rule: the finer the grain, the lower the share.")
        st.dataframe(cmp_, hide_index=True)
    r1, r1b, r2 = st.columns([1, 1.3, 3.2])
    go_ = r1.button("▶ Run MSL", type="primary", disabled=ss.creds is None or bool(miss))
    go_all = r1b.button("▶ Run all 5 rules", disabled=ss.creds is None or not level,
                        help="Runs Rules 1 to 5 one after another for the chosen Product and Threshold. A rule whose columns are not mapped is skipped and listed.")
    if ss.creds is None:
        r2.info("Connect to SQL Server in the sidebar first.")
    if go_:
        with st.spinner("Running MSL on SQL Server…"):
            try:
                ss.msl_res = live.msl_run(cfg, level, rule, thr, ss.creds["user"], ss.creds["password"])
            except Exception as e:
                ss.msl_res = {"error": str(e)}
    if go_all:
        runs, n = [], len(core.MSL_RULES)
        bar = st.progress(0.0, text="Starting")
        for i, rl in enumerate(core.MSL_RULES):
            bar.progress(i / n, text=f"Running {rl} ({i + 1} of {n})")
            m = core.msl_missing(cfg, rl, level or "")
            if m:
                runs.append({"rule": rl, "error": "not run, set in Live input setup: " + "; ".join(m)})
                continue
            try:
                runs.append(live.msl_run(cfg, level, rl, thr, ss.creds["user"], ss.creds["password"]))
            except Exception as e:
                runs.append({"rule": rl, "error": str(e)})
        bar.progress(1.0, text="Done")
        ss.msl_all = {"level": level, "threshold": thr, "runs": runs}
    ra = ss.get("msl_all")
    if ra:
        st.markdown(f"##### All rules · Product = {ra['level']} · threshold {ra['threshold']}%")
        ok = [r for r in ra["runs"] if not r.get("error")]
        for r in ra["runs"]:
            if r.get("error"):
                st.warning(f"{r['rule']}: {r['error']}")
        if ok:
            order = [r["rule"] for r in ok]
            lg = pd.DataFrame([{"Rule": r["rule"], **s} for r in ok for s in r["summary"]])
            if lg.empty:
                st.info("No product x store rows to test under any rule.")
            else:
                first = lg[lg["Rule"] == order[0]].sort_values("Stores", ascending=False)
                opts = sorted(lg["Product"].astype(str).unique())
                ss.msl_all_pf = [v for v in ss.get("msl_all_pf", []) if v in opts]
                apick = st.multiselect("Product filter (all rules)", opts, key="msl_all_pf", help="Blank = the 5 products with the most stores")
                keep = apick or [str(p) for p in first["Product"].head(5)]
                lg["Product"] = lg["Product"].astype(str)
                pv = (lg[lg["Product"].isin(keep)].pivot_table(index="Product", columns="Rule", values="% stores at or above threshold", aggfunc="first")
                      .reindex(columns=order))
                st.caption("% of stores carrying the product whose dominant location holds the threshold share of its facings. Finer grain (higher rule number, except Rule 5) gives lower values.")
                st.dataframe(pv.reset_index(), hide_index=True,
                             column_config={r: st.column_config.NumberColumn(format="%.1f%%") for r in order})
                with st.expander("Stores, facings share and data checks per rule"):
                    st.dataframe(lg[lg["Product"].isin(keep)].sort_values(["Product", "Rule"]), hide_index=True,
                                 column_config={"% stores at or above threshold": st.column_config.NumberColumn(format="%.1f"),
                                                "Facings-weighted dominant share %": st.column_config.NumberColumn(format="%.1f")})
                    st.dataframe(pd.DataFrame([{"Rule": r["rule"], "Seconds": r["seconds"], "Rows used": int(r["counts"].get("rows_used") or 0),
                                                "Null facings": int(r["counts"].get("null_facings") or 0), "Null location": int(r["counts"].get("null_location") or 0),
                                                "Null product": int(r["counts"].get("null_product") or 0), "SA rows with no MDM": int(r["counts"].get("sa_rows_no_mdm") or 0),
                                                "UPCs repeated in MDM": int(r["counts"].get("mdm_dup_upcs") or 0)} for r in ok]), hide_index=True)
                    if any(int(r["counts"].get("mdm_dup_upcs") or 0) for r in ok):
                        st.warning("UPCs repeat in MDM: facings are counted once per MDM row, so shares are inflated. Fix MDM first.")
                st.download_button("⬇️ All rules by product CSV", lg.to_csv(index=False).encode("utf-8"), "msl_all_rules.csv", "text/csv")
    res = ss.get("msl_res")
    if not res:
        return
    if res.get("error"):
        st.error(res["error"])
        return
    cn = res["counts"]
    st.caption(f"Result for Product = {res['level']} · {res['rule']} · threshold {res['threshold']}% · {res['seconds']}s"
               + (f" · MDM pulled from {res['info']['server']}: {res['info']['mdm_rows']:,} rows" if res.get("info") else ""))
    flags = [(cn.get("mdm_dup_upcs"), "UPC(s) repeat in MDM: facings are counted once per MDM row, so shares are inflated for those products."),
             (cn.get("sa_rows_no_mdm"), "SA row(s) have no MDM match and are not in any product."),
             (cn.get("null_product"), "joined row(s) have no value at the chosen Product and are left out."),
             (cn.get("null_facings"), "joined row(s) have null facings and are left out."),
             (cn.get("null_location"), "joined row(s) have a null location column and are left out.")]
    for n, t in flags:
        if n:
            st.warning(f"{int(n):,} {t}")
    with st.expander("Row counts through the query", expanded=False):
        st.dataframe(pd.DataFrame([{"Step": lbl, "Rows": int(cn.get(k) or 0)} for k, lbl in MSL_COUNTS]), hide_index=True)
    if not res["rows"]:
        st.info("No product x store rows to test.")
        return
    sm = pd.DataFrame(res["summary"])
    opts = sorted(sm["Product"].astype(str))
    ss.msl_pf = [v for v in ss.get("msl_pf", []) if v in opts]
    pick = st.multiselect("Product filter", opts, key="msl_pf", help="Insight lines and the store table show the selected products; blank = the 5 products with the most stores")
    rl = res["rule"].split(": ", 1)[-1]
    show = [r for r in res["summary"] if str(r["Product"]) in pick] if pick else sorted(res["summary"], key=lambda r: -r["Stores"])[:5]
    for r in show:
        st.markdown(f'<b><u>{r["Stores at or above threshold"]:,} stores out of {r["Stores"]:,} carrying the product show a dominant location at the {html.escape(rl)} rule for '
                    f'{html.escape(str(res["level"]))} = {html.escape(str(r["Product"]))} ({res["threshold"]}% or more of its store facings in one location)</u></b>', unsafe_allow_html=True)
    if not pick and len(res["summary"]) > len(show):
        st.caption(f"Showing the {len(show)} products with the most stores. Select products in the filter to see others.")
    st.markdown("##### By product")
    st.dataframe(sm, hide_index=True, column_config={"% stores at or above threshold": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
                                                    "Facings-weighted dominant share %": st.column_config.NumberColumn(format="%.1f")})
    df = pd.DataFrame(res["rows"], columns=res["columns"])
    df["Dominant location"] = df[[f"l{i}" for i in range(res["n_loc"])]].astype(str).agg(" / ".join, axis=1)
    df["Result"] = df["share_pct"].astype(float).ge(res["threshold"]).map({True: "Pass", False: "Fail"})
    det = df[["product", "store", "Dominant location", "fac", "tot", "share_pct", "Result"]].rename(
        columns={"product": "Product", "store": "Store", "fac": "Dominant facings", "tot": "Product facings", "share_pct": "Dominant share %"})
    st.markdown("##### By product and store")
    if pick:
        det = det[det["Product"].astype(str).isin(pick)]
    st.dataframe(det, hide_index=True, column_config={"Dominant share %": st.column_config.NumberColumn(format="%.1f")})
    st.caption("Ties on facings go to the lowest location value. Store passes when its dominant share is at or above the threshold.")
    st.download_button("⬇️ MSL by product and store CSV", det.to_csv(index=False).encode("utf-8"), "msl_product_store.csv", "text/csv")


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


# ---------------- help (floating button, every view; no network, no LLM) ----------------
st.markdown("""<style>
.st-key-help_fab{position:fixed;bottom:22px;right:26px;z-index:1000;width:auto!important}
.st-key-help_fab button{background:#4E106F!important;color:#fff!important;border:none!important;border-radius:999px!important;
 padding:.55rem 1.1rem!important;font-weight:600!important;box-shadow:0 4px 14px rgba(16,24,40,.28)}
.st-key-help_fab button:hover{background:#6A5EFF!important}
div[data-testid="stPopoverBody"]{min-width:min(480px,92vw);max-height:72vh;overflow-y:auto}
</style>""", unsafe_allow_html=True)


@st.cache_resource(show_spinner=False)
def help_index(stamp):
    return qc_help.build_index(ROOT, CATALOG)


def help_panel():
    stamp = tuple(p.stat().st_mtime if p.exists() else 0 for p in (GUIDE, RUNBOOK))
    with st.container(key="help_fab"), st.popover("💬 Need help?"):
        st.caption("Answers come from the user guide, the runbook, checks_input.json and your current setup and run. Nothing leaves the app.")
        for k, lbl in qc_help.QUICK.items():
            if st.button(lbl, key=f"help_{k}", use_container_width=True):
                ss.help_q = (k, "")
        with st.form("help_form"):
            q = st.text_input("Ask about the app", key="help_text", placeholder="e.g. why is SA007 manual")
            if st.form_submit_button("Ask"):
                ss.help_q = ("ask", q)
        if not ss.get("help_q"):
            return
        L, J = get_run(ss.live_runs, ss.sel_live), get_run(ss.is_runs, ss.sel_is)
        ctx = {"x": core.build(L, J, ss.live_runs, ss.is_runs, LIVE_OK), "cfg": get_cfg(True), "catalog": CATALOG,
               "products": prod_map(), "connected": ss.creds is not None}
        kind, q = ss.help_q
        res = qc_help.answer(help_index(stamp), kind, q, ctx)
        st.divider()
        st.markdown(f"**{qc_help.QUICK.get(kind, q)}**")
        st.markdown("\n".join(res["lines"]))
        for d in res["hits"]:
            body, cut = qc_help.snippet(d["body"])
            with st.container(border=True):
                st.markdown(f"**{d['title']}** · _{d['source']}_")
                st.markdown(body)
                if cut:
                    st.caption("Continues in 📖 Guide (or the runbook download there).")
        if st.button("Clear answer", key="help_clear"):
            ss.pop("help_q", None)
            st.rerun()


{NAV[0]: gate_view, NAV[1]: checks_view, NAV[2]: setup_view, NAV[3]: history_view, NAV[4]: sql_view, NAV[5]: msl_view, NAV[6]: guide_view}[view]()
help_panel()
