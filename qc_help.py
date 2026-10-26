"""In-app Help: answers from USER_GUIDE.md, the manual fallback runbook and checks_input.json, plus the gate's own logic
(qc_core.blocks / missing_for / certify / generic_runs). Pure Python: no network, no LLM, nothing leaves the machine."""
import math
import re
from collections import Counter
from pathlib import Path

import qc_core as core

DOCS = (("USER_GUIDE.md", "User guide"), ("QC_Gate_Manual_Fallback_Runbook.md", "Manual fallback runbook"))
STOP = set("a an and are as at be but by can do does did for from how i if in into is it its me my not of on or so that the then "
           "this to was what when where which why will with you your have has get got shows show see".split())
QUICK = {"gate": "Why is my gate not Release?", "manual": "Why is a check Manual or Blocked?",
         "map": "What do I map next?", "badge": "Why is a stage not Gold?"}
INTENT = {"gate": r"\b(red|verdict|release|releas\w*|gate)\b", "manual": r"\b(manual|blocked|not run|did not run|skipped)\b",
          "badge": r"\b(gold|silver|bronze|badge|certif\w*)\b"}
SNIP = 1200
SEV = {"CRITICAL": "Critical", "HIGH": "High", "WARNING": "Warning", "INFO": "Info"}


def tokens(s):
    out = []
    for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(s).lower()):
        if t in STOP or len(t) < 2:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def sections(text, source):
    """Markdown -> one doc per #, ## or ### heading (the # one is the intro). Headings inside code fences are ignored."""
    out, title, buf, fence = [], None, [], False

    def flush():
        body = "\n".join(x for x in buf if x.strip() != "---").strip()
        if title and body:
            out.append({"title": title, "body": body, "source": source})
    for line in str(text).splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^#{1,3}\s+(.*)", line)
        if m:
            flush()
            title, buf = m.group(1).strip(), []
        elif title is not None:
            buf.append(line)
    flush()
    return out


def _need_txt(rule):
    items = [f"{core.INPUTS[s][0]} table" if r == "table" else f"{core.INPUTS[s][0]} → {core.ROLE[r]}" for s, r in core.REQ.get(rule, [])]
    if rule in core.REQ_INPUT:
        items.append(core.REQ_INPUT[rule][1])
    return "; ".join(dict.fromkeys(items))


def rule_text(r, m):
    sev, on = m.get("severity"), m.get("enabled", True) is not False
    parts = [f"Stage: {core.LBL.get(m.get('stage'), 'Any mapped source')}", f"Severity: {SEV.get(sev, sev)}",
             "Enabled" if on else "Disabled in checks_input.json (not run)"]
    if on and sev in ("CRITICAL", "HIGH"):
        parts.append("Blocks release when it fails, is Manual (not switched off) or is Blocked")
    if on and m.get("required"):
        parts.append("Required: a missing result is treated as Blocked")
    if _need_txt(r):
        parts.append(f"Needs: {_need_txt(r)}")
    return " · ".join(parts)


def _key(t):
    return t.replace("-", "")


WEIGHT = {"User guide": 1.3, "Manual fallback runbook": 0.8}  # user-facing text ranks above the runbook SQL and the rule catalog on a tie


def build_index(root, catalog):
    docs = []
    for name, label in DOCS:
        p = Path(root) / name
        if p.exists():
            docs += sections(p.read_text(encoding="utf-8"), label)
    rules = catalog.get("rules") or {}
    for r, m in rules.items():
        docs.append({"title": f"{r}: {m.get('description', '')}", "body": rule_text(r, m), "source": "Check reference (checks_input.json)", "rule": r})
    df = Counter()
    for d in docs:
        d["tf"], d["tt"] = Counter(tokens(d["title"] + " " + d["body"])), set(tokens(d["title"]))
        df.update(d["tf"].keys())
    return {"docs": docs, "idf": {t: math.log(1 + len(docs) / n) for t, n in df.items()}, "rules": _rule_keys(rules)}


def _rule_keys(rules):
    """Exact lower-case ID first (PRJ001 vs PRJ-001 differ only by the dash); the dash-free form only when it is unambiguous."""
    loose = Counter(_key(r.lower()) for r in rules)
    out = {_key(r.lower()): r for r in rules if loose[_key(r.lower())] == 1}
    out.update({r.lower(): r for r in rules})
    return out


def rule_ids(idx, q):
    keys = idx["rules"]
    return list(dict.fromkeys(keys.get(t) or keys[_key(t)] for t in tokens(q) if t in keys or _key(t) in keys))


def search(idx, q, k=3):
    qt, ids = set(tokens(q)), {r.lower() for r in rule_ids(idx, q)}
    sc = []
    for d in idx["docs"]:
        s = sum(idx["idf"].get(t, 0) * (1 + math.log(d["tf"][t])) * (3 if t in d["tt"] else 1) for t in qt if d["tf"].get(t))
        s *= WEIGHT.get(d["source"], 1)
        if d.get("rule") and d["rule"].lower() in ids:
            s += 100
        if s > 0:
            sc.append((s, d))
    sc.sort(key=lambda x: -x[0])
    return [d for _, d in sc[:k]]


def snippet(body, n=SNIP):
    """First ~n characters, cut at a paragraph break, with any open code fence closed."""
    if len(body) <= n:
        return body, False
    cut = body.rfind("\n\n", 0, n)
    s = body[:cut if cut > n // 3 else n].rstrip()
    if s.count("```") % 2:
        s += "\n```"
    return s, True


# ---------------- answers from the current gate and setup ----------------
def _first(c):
    return str(c.get("message") or "").splitlines()[0][:200] if c.get("message") else ""


def _why(c):
    sev = SEV.get(c.get("severity"), c.get("severity"))
    return {"BLOCKED": "Blocked (could not run)", "MANUAL_REQUIRED": f"Manual at {sev} (not evaluated)"}.get(c["status"], f"Failed at {sev}")


def _enabled(catalog, r):
    return ((catalog.get("rules") or {}).get(r) or {}).get("enabled", True) is not False


def gate_answer(ctx):
    x = ctx.get("x")
    if not x:
        return ["Nothing on the gate yet. Run live checks from **🧪 Live input setup**, or load an IS `qc_report_*.json` under **📥 Load results**."]
    J = x["J"]
    verdict = (("Do not release" if x["fail"] else "Release with caveats" if x["warn"] else "Release") if J
               else ("Fix inputs before processing" if x["fail"] else "Inputs ready to process"))
    out = [f"Verdict: **{verdict}**."]
    if x["need_del"]:
        out.append("- The IS report has no Delivery stage checks, so the wave cannot release.")
    if x["blocking"]:
        out.append(f"{len(x['blocking'])} blocking check(s):")
        out += [f"- **{c.get('rule')}** · {core.LBL.get(c['stage'], c['stage'])} · {_why(c)}: {_first(c)}" for c in x["blocking"][:12]]
        if len(x["blocking"]) > 12:
            out.append(f"- … and {len(x['blocking']) - 12} more")
    cav = [c for c in x["checks"] if c["status"] != "PASS" and not core.blocks(c)]
    if cav:
        out.append(f"{len(cav)} Warning/Info check(s) are not Pass: they do not block, put them in the release notes.")
    if x["counts"]["suspect"]:
        out.append(f"{x['counts']['suspect']} suspect pass(es): a pass with a negative %. Confirm the check tests absolute deviation.")
    if not J:
        out.append("No IS report loaded: this verdict covers the live inputs only.")
    if x["fail"] or cav:
        out.append("Next: open **🔎 Checks**, switch on **Only blockers**, select a row for the sample failing rows. "
                   "Check upstream first: MDM mapping, projections and universe, SA, then POS.")
    return out


def manual_answer(ctx):
    x, cfg, cat = ctx.get("x"), ctx.get("cfg") or {}, ctx.get("catalog") or {}
    out = []
    if x:
        mb = [c for c in x["checks"] if c["status"] in ("MANUAL_REQUIRED", "BLOCKED")]
        if mb:
            out.append(f"In the run on the gate, {len(mb)} check(s) are Manual or Blocked:")
            out += [f"- **{c.get('rule')}** · {_why(c)}{' ⛔' if core.blocks(c) else ''}: {_first(c)}" for c in mb[:15]]
            if len(mb) > 15:
                out.append(f"- … and {len(mb) - 15} more (filter Status in **🔎 Checks**)")
            if any(c["status"] == "BLOCKED" for c in mb):
                out.append("Blocked = the SQL or driver failed (message has the error) or a required rule produced no result. "
                           "Check table name, access to that server, and column types.")
        else:
            out.append("No Manual or Blocked checks in the run on the gate.")
    off = [r for r in cfg.get("rules_off") or [] if _enabled(cat, r)]
    miss = {r: core.missing_for(r, cfg) for r in core.REQ if _enabled(cat, r) and r not in off and r not in core.not_applicable(cfg)}
    miss = {r: m for r, m in miss.items() if m}
    if miss:
        out.append(f"With the current setup, {len(miss)} spec check(s) would run as Manual:")
        out += [f"- **{r}** ({SEV.get(cat['rules'][r].get('severity'), '')}): set {'; '.join(m)}" for r, m in list(miss.items())[:15]]
        if len(miss) > 15:
            out.append(f"- … and {len(miss) - 15} more (see **Check readiness** in Live input setup)")
    elif cfg:
        out.append("With the current setup every enabled spec check has what it needs.")
    if off:
        out.append(f"Switched off for this run (shown as Manual, not blocking, put in the release notes): {', '.join(off)}. Tick **Run** in Check readiness to switch them back on.")
    out.append("A Manual check at Critical or High blocks release, unless you switched it off in Check readiness. Switched-off and Warning/Info Manual checks go in the release notes.")
    return out


def map_answer(ctx):
    cfg, cat = ctx.get("cfg") or {}, ctx.get("catalog") or {}
    out, step = [], 0

    def add(s):
        nonlocal step
        step += 1
        out.append(f"{step}. {s}")
    if not ctx.get("connected"):
        add("Connect to SQL Server in the sidebar (**🔌 SQL Server**).")
    if not str(cfg.get("report_name") or "").strip():
        add("Enter the **Report name** in Live input setup.")
    if not any((o or {}).get("table") for o in (cfg.get("sources") or {}).values()):
        add("Enter at least one source **Table** (Shelf audit first), then click **Load columns · all sources**.")
    off = set(cfg.get("rules_off") or [])
    need = {}
    na = core.not_applicable(cfg)
    for r in core.REQ:
        if not _enabled(cat, r) or r in off or r in na:
            continue
        for m in core.missing_for(r, cfg):
            need.setdefault(m, []).append(r)
    order = sorted(need.items(), key=lambda kv: (min(core.SEV_O.get(cat["rules"][r].get("severity"), 9) for r in kv[1]), -len(kv[1]), kv[0]))
    for item, rs in order[:12]:
        blk = any(cat["rules"][r].get("severity") in ("CRITICAL", "HIGH") for r in rs)
        add(f"Set **{item}**{' ⛔' if blk else ''} → used by {', '.join(rs)}")
    if len(order) > 12:
        out.append(f"… and {len(order) - 12} more. See the Needs line on each source tab.")
    if need:
        out.append("⛔ = a Critical or High check stays Manual and blocks release until this is set. "
                   "Only exact column-name matches are filled by Load columns: confirm every mapping.")
    else:
        add("Everything the enabled spec checks need is mapped. Click **💾 Save setup**, then **▶ Run live checks**.")
    return out


def badge_answer(ctx):
    x, prods = ctx.get("x"), ctx.get("products") or {}
    if not x:
        return ["Nothing on the gate yet, so no stage badges. Badges: Bronze = nothing blocks; Silver = Bronze + every check evaluated "
                "+ owner and SLO entered; Gold = Silver + every check passes and no suspect pass."]
    out = []
    for s in core.STAGES:
        cs = [c for c in x["checks"] if c["stage"] == s]
        if not cs:
            continue
        p = prods.get(s) or {}
        tier = core.certify(cs, p)
        if tier == "Gold":
            out.append(f"- **{core.LBL[s]}**: 🥇 Gold.")
            continue
        why = []
        if tier is None:
            why.append(f"{sum(core.blocks(c) for c in cs)} blocking check(s)")
        if tier in (None, "Bronze"):
            mb = sum(c["status"] in ("MANUAL_REQUIRED", "BLOCKED") for c in cs)
            if mb:
                why.append(f"{mb} Manual or Blocked")
            if not p.get("owner") or not p.get("slo"):
                why.append("owner and SLO not both entered (📦 Data products on the Gate)")
        fails, sus = sum(c["status"] == "FAIL" for c in cs), sum(core.suspect(c) for c in cs)
        if fails:
            why.append(f"{fails} failing")
        if sus:
            why.append(f"{sus} suspect pass(es)")
        out.append(f"- **{core.LBL[s]}**: {tier or 'Not certified'} → {'; '.join(why)}")
    out.append("Badges are informational: the release decision only uses the release rule.")
    return out


def rule_answer(ctx, r):
    x, cfg, cat = ctx.get("x"), ctx.get("cfg") or {}, ctx.get("catalog") or {}
    m = (cat.get("rules") or {}).get(r) or {}
    out = [f"**{r}**: {m.get('description', '')}", f"- {rule_text(r, m)}"]
    if not _enabled(cat, r):
        return out
    got = [c for c in (x or {}).get("checks", []) if c.get("rule") == r]
    if got:
        out += [f"- On the gate: {core.LBL.get(c['stage'], c['stage'])} · {c['status'].replace('_REQUIRED', '').title()}"
                f"{' ⛔' if core.blocks(c) else ''}: {_first(c)}" for c in got[:6]]
    if r in (cfg.get("rules_off") or []):
        out.append("- Switched off for this run in Check readiness (shows as Manual, does not block).")
    elif r in core.not_applicable(cfg):
        out.append(f"- Current setup: not applicable ({core.not_applicable(cfg)[r]}). Shows as Manual, does not block, goes in the release notes.")
    elif r in core.REQ:
        miss = core.missing_for(r, cfg)
        out.append("- Current setup: ready to run." if not miss else f"- Current setup: would run as Manual. Set {'; '.join(miss)}.")
    elif r in core.GEN:
        runs = core.generic_runs(r, cfg)
        out.append("- Current setup: will run on " + "; ".join(f"{core.LBL[s]}: {', '.join(c)}" for s, c in runs.items())
                   if runs else "- Current setup: no source mapped for it, so it is skipped (never Manual).")
    return out


QUICK_FN = {"gate": gate_answer, "manual": manual_answer, "map": map_answer, "badge": badge_answer}


def answer(idx, kind, q, ctx):
    """kind: a QUICK key, or 'ask' for free text. Returns {'lines': [...markdown], 'hits': [docs]}."""
    if kind in QUICK_FN:
        return {"lines": QUICK_FN[kind](ctx), "hits": []}
    q = str(q or "").strip()
    if not q:
        return {"lines": ["Type a question, or use one of the buttons above."], "hits": []}
    lines = []
    for r in rule_ids(idx, q)[:3]:
        lines += rule_answer(ctx, r)
    low = q.lower()
    for k, pat in INTENT.items():
        if not lines and re.search(pat, low):
            lines += QUICK_FN[k](ctx)
    hits = search(idx, q)
    if not lines and not hits:
        lines = ["No match in the guide. Try a rule ID (e.g. SA007), a status (Manual, Blocked), or a tab name (SQL, MSL). "
                 "See also **📖 Guide → 9. Troubleshooting**."]
    return {"lines": lines, "hits": hits}
