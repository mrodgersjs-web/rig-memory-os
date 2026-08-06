#!/usr/bin/env python3
"""Generate Jake's live prediction dashboard as a single HTML file."""
import json
import pathlib
import sys
import time

STATE = pathlib.Path.home() / ".rig" / "state"


def load_json_safe(path, default):
    """Load JSON from path; tolerate missing files and corrupt content."""
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
        return default


def load_data():
    report_file = sorted(STATE.glob("jake-live-predictions-*.json"))[-1]
    report = json.loads(report_file.read_text())
    import sqlite3
    conn = sqlite3.connect(str(STATE / "goal-loop-memory.db"))
    counts = dict(conn.execute("SELECT layer, COUNT(*) FROM memories GROUP BY layer").fetchall())
    conn.close()
    model_file = STATE / "predictor-transitions.json"
    model = json.loads(model_file.read_text()) if model_file.exists() else {}
    harness = load_json_safe(STATE / "jake-harness.json", {})
    bridge = load_json_safe(STATE / "prediction-bridge.json", {})
    stigmergy = load_json_safe(STATE / "stigmergy-candidates.json", [])
    link_gaps = load_json_safe(STATE / "link-gap-candidates.json", [])
    return report, counts, model, report_file.name, harness, bridge, stigmergy, link_gaps


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _fmt_prob(v):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _no_data_panel(title):
    return f"""<div class="panel">
<h2>{esc(title)}</h2>
<div class="note">no data yet</div>
</div>"""


def render_harness_panel(harness):
    if not isinstance(harness, dict):
        return _no_data_panel("Harness Interventions")
    interventions = harness.get("interventions")
    if not isinstance(interventions, list) or not interventions:
        return _no_data_panel("Harness Interventions")

    sev_order = {"blocking": 0, "warning": 1, "advisory": 2}
    sev_class_map = {"blocking": "sev-block", "warning": "sev-warn", "advisory": "sev-adv"}
    capabilities = harness.get("capabilities_loaded", 0)
    active = len(interventions)
    blocking = sum(1 for iv in interventions if isinstance(iv, dict) and iv.get("severity") == "blocking")

    rows = ""
    for iv in sorted(
        interventions,
        key=lambda x: sev_order.get(x.get("severity") if isinstance(x, dict) else None, 3),
    ):
        if not isinstance(iv, dict):
            continue
        sev = iv.get("severity", "advisory")
        sev_class = sev_class_map.get(sev, "sev-adv")
        rows += f"""
        <tr>
          <td class="pid">{esc(iv.get('id', ''))}</td>
          <td>{esc(iv.get('domain', ''))}</td>
          <td><span class="badge {sev_class}">{esc(sev).upper()}</span></td>
          <td class="counter">{esc(iv.get('detail', ''))}</td>
          <td class="counter">{esc(iv.get('intervention', ''))}</td>
        </tr>"""
    if not rows:
        return _no_data_panel("Harness Interventions")

    return f"""<div class="panel">
<h2>Harness Interventions — {capabilities} capabilities · {active} active · {blocking} blocking</h2>
<div class="note">Council-nominated interventions currently loaded into Jake's harness.</div>
<table><tr><th>ID</th><th>Domain</th><th>Severity</th><th>Detail</th><th>Intervention</th></tr>
{rows}
</table></div>"""


def render_predictions_panel(bridge, harness):
    open_preds = bridge.get("open") if isinstance(bridge, dict) else None
    if isinstance(open_preds, dict):
        open_preds = list(open_preds.values())
    if not isinstance(open_preds, list):
        open_preds = []
    open_preds = [p for p in open_preds if isinstance(p, dict)]

    signals = harness.get("signals", {}) if isinstance(harness, dict) else {}
    if not isinstance(signals, dict):
        signals = {}
    forecast_accuracy = signals.get("forecast_accuracy")
    forecast_n = signals.get("forecast_n")
    anti_calibrated = bool(signals.get("anti_calibrated"))

    if not open_preds and forecast_accuracy is None and forecast_n is None:
        return _no_data_panel("Open Predictions")

    rows = ""
    for p in open_preds:
        age = p.get("age_min", p.get("age_minutes"))
        rows += f"""
        <tr>
          <td class="counter">{esc(p.get('question', ''))}</td>
          <td class="num">{_fmt_prob(p.get('p_true'))}</td>
          <td class="num">{_fmt_prob(p.get('p_base_rate'))}</td>
          <td class="num">{esc(age) if age is not None else '—'}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="4" class="note">no open predictions</td></tr>'

    track_bits = []
    if forecast_accuracy is not None:
        try:
            track_bits.append(f"accuracy {float(forecast_accuracy):.1%}")
        except (TypeError, ValueError):
            pass
    if forecast_n is not None:
        track_bits.append(f"n={esc(forecast_n)}")
    track_line = " · ".join(track_bits) if track_bits else "no track record yet"

    badge = '<span class="badge sev-block">ANTI-CALIBRATED</span>' if anti_calibrated else ""

    return f"""<div class="panel">
<h2>Open Predictions {badge}</h2>
<div class="note">Track record: {esc(track_line)}</div>
<table><tr><th>Question</th><th>P(true)</th><th>P(base rate)</th><th>Age (min)</th></tr>
{rows}
</table></div>"""


def render_discovery_panel(stigmergy, link_gaps):
    stig_list = [s for s in stigmergy if isinstance(s, dict)] if isinstance(stigmergy, list) else []
    gap_list = [g for g in link_gaps if isinstance(g, dict)] if isinstance(link_gaps, list) else []

    if not stig_list and not gap_list:
        return _no_data_panel("Discovery — Stigmergy & Link Gaps")

    stig_sorted = sorted(stig_list, key=lambda s: -(s.get("survival_streak") or 0))[:10]
    stig_rows = ""
    for s in stig_sorted:
        sleeper_badge = '<span class="badge sev-warn">SLEEPER</span>' if s.get("sleeper") else ""
        stig_rows += f"""
        <tr>
          <td class="counter">{esc(s.get('edge', ''))}</td>
          <td class="num">{esc(s.get('survival_streak', 0))}</td>
          <td>{sleeper_badge}</td>
        </tr>"""
    if not stig_rows:
        stig_rows = '<tr><td colspan="3" class="note">no data yet</td></tr>'

    gap_sorted = sorted(gap_list, key=lambda g: -(g.get("cooccurrence") or 0))[:10]
    gap_rows = ""
    for g in gap_sorted:
        n_examples = len(g.get("gap_examples") or [])
        gap_rows += f"""
        <tr>
          <td class="proj">{esc(g.get('tag_a', ''))}</td>
          <td class="proj">{esc(g.get('tag_b', ''))}</td>
          <td class="num">{esc(g.get('cooccurrence', 0))}</td>
          <td class="num">{n_examples}</td>
        </tr>"""
    if not gap_rows:
        gap_rows = '<tr><td colspan="4" class="note">no data yet</td></tr>'

    return f"""<div class="panel">
<h2>Discovery — Stigmergy & Link Gaps</h2>
<div class="note">Surviving stigmergic trails, plus tag pairs that co-occur often but never cite each other.</div>
<div class="cols">
<div>
<h3 style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--dim)">Top stigmergy edges</h3>
<table><tr><th>Edge</th><th>Streak</th><th></th></tr>
{stig_rows}
</table>
</div>
<div>
<h3 style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--dim)">Top link gaps</h3>
<table><tr><th>Tag A</th><th>Tag B</th><th>Co-occ</th><th>Gaps</th></tr>
{gap_rows}
</table>
</div>
</div>
</div>"""


def main():
    report, mem_counts, model, report_name, harness, bridge, stigmergy, link_gaps = load_data()
    preds = report["predictions"]
    sessions = report["sessions"]
    histo = report.get("phase_histogram", {})

    # dedupe predictions for the headline view
    seen = set()
    uniq_preds = []
    for p in preds:
        k = (p["for_project"], p["current_phase"], p["predicted_next_phase"])
        if k not in seen:
            seen.add(k)
            uniq_preds.append(p)

    max_prob = max((p["probability"] for p in uniq_preds), default=1)
    flagged = [s for s in sessions if s["pushbacks"]]
    total_transitions = sum(sum(r["next"].values()) for r in model.get("transitions", []))

    pred_rows = ""
    for p in sorted(uniq_preds, key=lambda x: -x["probability"]):
        pct = int(p["probability"] * 100)
        heat = min(1.0, p["probability"] / max_prob) if max_prob else 0
        color = f"hsl({int(150 * heat)}, 70%, 55%)"
        pred_rows += f"""
        <tr>
          <td class="proj">{esc(p['for_project'])}</td>
          <td><span class="phase">{esc(p['current_phase'])}</span> <span class="arrow">→</span> <span class="phase pred">{esc(p['predicted_next_phase'])}</span></td>
          <td class="probcell">
            <div class="barwrap"><div class="bar" style="width:{pct}%;background:{color}"></div><span class="probt">{p['probability']:.2f}</span></div>
          </td>
          <td class="pid">{esc(p['prediction_id'][:8])}</td>
        </tr>"""

    pb_rows = ""
    for s in flagged:
        for pb in s["pushbacks"]:
            sev_class = {"blocking": "sev-block", "warning": "sev-warn", "advisory": "sev-adv"}.get(pb["escalation"], "sev-adv")
            pb_rows += f"""
        <tr>
          <td class="proj">{esc(s['project'])}</td>
          <td class="num">{s['files_touched']}</td>
          <td class="num">{s['test_runs']}</td>
          <td><span class="badge {sev_class}">{esc(pb['escalation']).upper()}</span></td>
          <td class="pattern">{esc(pb['pattern'])}</td>
          <td class="counter">{esc(pb['counter'])}</td>
        </tr>"""

    histo_bars = ""
    max_h = max(histo.values(), default=1)
    for phase, count in sorted(histo.items(), key=lambda x: -x[1]):
        w = int(100 * count / max_h)
        histo_bars += f"""
        <div class="hrow"><span class="hlabel">{esc(phase)}</span>
        <div class="hbarwrap"><div class="hbar" style="width:{w}%"></div></div>
        <span class="hcount">{count}</span></div>"""

    mem_rows = "".join(
        f"<tr><td>{esc(k)}</td><td class='num'>{v:,}</td></tr>"
        for k, v in sorted(mem_counts.items(), key=lambda x: -x[1])
    )

    clean_sessions = [s for s in sessions if not s["pushbacks"]]

    harness_panel = render_harness_panel(harness)
    predictions_panel = render_predictions_panel(bridge, harness)
    discovery_panel = render_discovery_panel(stigmergy, link_gaps)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jake — Live Predictions & Patterns</title>
<style>
  :root {{ --bg:#0b0e17; --panel:#121627; --edge:#1e2542; --text:#e6ebf5; --dim:#8b93a7; --accent:#7c6cf0; --green:#34d399; --amber:#fbbf24; --red:#f87171; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font:14px/1.5 -apple-system, "SF Pro Text", sans-serif; padding:32px; }}
  h1 {{ font-size:26px; font-weight:700; letter-spacing:-0.02em; }}
  h1 .jake {{ color:var(--accent); }}
  .sub {{ color:var(--dim); margin:6px 0 28px; font-size:13px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:28px; }}
  .stat {{ background:var(--panel); border:1px solid var(--edge); border-radius:12px; padding:16px 18px; }}
  .stat .v {{ font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .stat .k {{ color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:0.08em; margin-top:2px; }}
  .panel {{ background:var(--panel); border:1px solid var(--edge); border-radius:14px; padding:20px 22px; margin-bottom:24px; }}
  .panel h2 {{ font-size:15px; font-weight:600; margin-bottom:4px; }}
  .panel .note {{ color:var(--dim); font-size:12px; margin-bottom:14px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:0.07em; padding:6px 10px; border-bottom:1px solid var(--edge); }}
  td {{ padding:9px 10px; border-bottom:1px solid rgba(30,37,66,0.5); vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  .proj {{ font-weight:500; max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .phase {{ background:rgba(124,108,240,0.14); color:#b7aefc; padding:2px 9px; border-radius:6px; font-size:12px; font-weight:600; }}
  .phase.pred {{ background:rgba(52,211,153,0.13); color:var(--green); }}
  .arrow {{ color:var(--dim); margin:0 4px; }}
  .barwrap {{ position:relative; width:180px; height:20px; background:rgba(255,255,255,0.05); border-radius:6px; overflow:hidden; }}
  .bar {{ height:100%; border-radius:6px; opacity:0.85; }}
  .probt {{ position:absolute; right:7px; top:1px; font-size:11px; font-weight:700; color:#fff; text-shadow:0 1px 2px rgba(0,0,0,0.6); }}
  .pid {{ color:var(--dim); font-family:ui-monospace,monospace; font-size:11px; }}
  .badge {{ font-size:10px; font-weight:700; padding:3px 8px; border-radius:5px; letter-spacing:0.05em; }}
  .sev-block {{ background:rgba(248,113,113,0.16); color:var(--red); }}
  .sev-warn {{ background:rgba(251,191,36,0.14); color:var(--amber); }}
  .sev-adv {{ background:rgba(139,147,167,0.14); color:var(--dim); }}
  .pattern {{ font-weight:600; font-size:12px; }}
  .counter {{ color:var(--dim); font-size:12px; max-width:340px; }}
  .num {{ font-variant-numeric:tabular-nums; text-align:right; }}
  .hrow {{ display:flex; align-items:center; gap:12px; margin-bottom:7px; }}
  .hlabel {{ width:80px; text-align:right; color:var(--dim); font-size:12px; font-weight:600; }}
  .hbarwrap {{ flex:1; height:16px; background:rgba(255,255,255,0.04); border-radius:5px; overflow:hidden; }}
  .hbar {{ height:100%; background:linear-gradient(90deg,#7c6cf0,#4f46e5); border-radius:5px; }}
  .hcount {{ width:46px; font-size:12px; font-variant-numeric:tabular-nums; }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
  @media (max-width:900px) {{ .cols {{ grid-template-columns:1fr; }} }}
  .falsifiable {{ background:rgba(52,211,153,0.07); border:1px solid rgba(52,211,153,0.25); border-radius:10px; padding:12px 16px; font-size:12px; color:#a7e8cd; margin-bottom:24px; }}
  code {{ font-family:ui-monospace,monospace; font-size:11px; background:rgba(255,255,255,0.06); padding:1px 5px; border-radius:4px; }}
</style></head><body>

<h1><span class="jake">Jake</span> — Live Predictions & Patterns</h1>
<div class="sub">Report <code>{esc(report_name)}</code> · generated {esc(report['generated_at'])} · model: {total_transitions:,} learned transitions (persisted)</div>

<div class="grid">
  <div class="stat"><div class="v">{report['active_sessions']}</div><div class="k">Active sessions (6h)</div></div>
  <div class="stat"><div class="v">{len(uniq_preds)}</div><div class="k">Unique predictions</div></div>
  <div class="stat"><div class="v" style="color:var(--red)">{len(flagged)}</div><div class="k">Sessions flagged</div></div>
  <div class="stat"><div class="v" style="color:var(--green)">{len(clean_sessions)}</div><div class="k">Clean sessions</div></div>
  <div class="stat"><div class="v">{total_transitions:,}</div><div class="k">Learned transitions</div></div>
  <div class="stat"><div class="v">{sum(mem_counts.values()):,}</div><div class="k">Total memories</div></div>
</div>

<div class="falsifiable">
  <b>Falsifiable by construction:</b> every prediction carries an ID + 1-hour expiry and resolves against what the session actually does next via <code>resolve_prediction</code>. Probabilities are Laplace-smoothed Markov over 30,687 recorded phase transitions — Jake gets graded, he doesn't get to vibe.
</div>

{harness_panel}

{predictions_panel}

{discovery_panel}

<div class="panel">
<h2>Next-Phase Predictions — active sessions</h2>
<div class="note">Given the session's current phase, what Jake predicts happens next (learned from your full history).</div>
<table><tr><th>Project</th><th>Transition</th><th>Probability</th><th>Prediction ID</th></tr>
{pred_rows}
</table></div>

<div class="panel">
<h2>Pattern Pushbacks — {len(flagged)} of {len(sessions)} sessions flagged</h2>
<div class="note">Path-cluster scope-creep detection: flags sessions whose edits drift into topically unrelated directories.</div>
<table><tr><th>Project</th><th>Files</th><th>Tests</th><th>Escalation</th><th>Pattern</th><th>Jake's counter</th></tr>
{pb_rows}
</table></div>

<div class="cols">
<div class="panel">
<h2>Fleet Phase Histogram (6h)</h2>
<div class="note">What the active coding fleet is actually doing. read:edit imbalance is the systemic signal.</div>
{histo_bars}
</div>

<div class="panel">
<h2>Memory Store</h2>
<div class="note">All sources ingested — chats (episodic), Obsidian (semantic), runtime layers.</div>
<table><tr><th>Layer</th><th style="text-align:right">Memories</th></tr>
{mem_rows}
</table></div>
</div>

</body></html>"""

    out = STATE / "jake-dashboard.html"
    out.write_text(html)
    print(out)


if __name__ == "__main__":
    main()
