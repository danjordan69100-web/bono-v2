"""Bono v2 — Compare 2 sessions side-by-side.

Analyse 2 session IDs depuis bono.db et leurs CSV perf associés, sort un markdown comparant :
- Pace (best lap / avg lap si pipeline_timings ont l'info, sinon depuis events lap_completed)
- Latence pipeline (E2E, STT, LLM, TTS)
- Cost
- Tools usage
- Hardware (FPS, CPU/GPU si CSV présents)
- Events fired (auto-events count)

Usage :
  python compare_sessions.py --a 5 --b 8
  python compare_sessions.py --a 5 --b 8 --out comparison.md
"""
import sys
import argparse
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from analyze_session import load_session, load_perf_csv, find_perf_csv_for_session, agg, fmt_ms

ROOT = Path(__file__).parent
SESSIONS_DIR = Path(r"C:\Users\danjo\Desktop\bono_sessions")


def session_summary(sid: int) -> dict:
    """Compute aggregates for a session for comparison."""
    data = load_session(sid)
    if not data:
        return {}
    s = data["session"]
    timings = data["timings"]
    events = data["events"]
    costs = data["costs"]
    exch = data["exchanges"]

    e2e_vals = [t["total_e2e_ms"] for t in timings if t.get("total_e2e_ms")]
    stt_vals = [t["stt_ms"] for t in timings if t.get("stt_ms")]
    llm_vals = [t["llm_total_ms"] for t in timings if t.get("llm_total_ms")]
    tts_vals = [t["tts_ms"] for t in timings if t.get("tts_ms")]

    tool_counts = {}
    cache_hits = 0
    for t in timings:
        try:
            tools = json.loads(t.get("tools_used", "[]")) if isinstance(t.get("tools_used"), str) else (t.get("tools_used") or [])
            for tn in tools:
                tool_counts[tn] = tool_counts.get(tn, 0) + 1
            if t.get("cache_hit"): cache_hits += 1
        except Exception: pass

    event_counts = {}
    for e in events:
        event_counts[e["type"]] = event_counts.get(e["type"], 0) + 1

    perf_csv = find_perf_csv_for_session(s["started_at"])
    perf_rows = load_perf_csv(perf_csv) if perf_csv else []
    cpu_vals = [r["cpu_pct"] for r in perf_rows if isinstance(r.get("cpu_pct"), (int, float))]
    gpu_vals = [r["gpu_util_pct"] for r in perf_rows if isinstance(r.get("gpu_util_pct"), (int, float))]
    gpu_temp = [r["gpu_temp_c"] for r in perf_rows if isinstance(r.get("gpu_temp_c"), (int, float))]
    vram_vals = [r["vram_used_mb"] for r in perf_rows if isinstance(r.get("vram_used_mb"), (int, float))]

    return {
        "id": sid,
        "track": s.get("track", "?"),
        "car": s.get("car", "?"),
        "session_type": s.get("session_type", "?"),
        "started_at": s["started_at"],
        "duration_s": (s["ended_at"] - s["started_at"]) if s.get("ended_at") else 0,
        "n_exchanges": len(exch),
        "n_events": len(events),
        "n_timings": len(timings),
        "total_cost_usd": sum(c.get("cost_usd", 0) for c in costs),
        "e2e": agg(e2e_vals),
        "stt": agg(stt_vals),
        "llm": agg(llm_vals),
        "tts": agg(tts_vals),
        "tools": tool_counts,
        "events": event_counts,
        "cache_hit_rate": cache_hits / max(1, len(timings)),
        "cpu": agg(cpu_vals),
        "gpu_util": agg(gpu_vals),
        "gpu_temp": agg(gpu_temp),
        "vram_mb": agg(vram_vals),
        "perf_csv": str(perf_csv) if perf_csv else None,
    }


def diff(a, b, key, fmt=str):
    va, vb = a.get(key), b.get(key)
    if va is None and vb is None: return "-/-"
    if va is None: return f"-/{fmt(vb)}"
    if vb is None: return f"{fmt(va)}/-"
    return f"{fmt(va)} → {fmt(vb)}"


def diff_agg(a_agg, b_agg, field="avg"):
    if not a_agg and not b_agg: return "-/-"
    va = a_agg.get(field) if a_agg else None
    vb = b_agg.get(field) if b_agg else None
    if va is None and vb is None: return "-/-"
    if va is None: return f"-/{fmt_ms(vb)}"
    if vb is None: return f"{fmt_ms(va)}/-"
    delta_pct = ((vb - va) / va) * 100 if va > 0 else 0
    arrow = "↑" if delta_pct > 5 else ("↓" if delta_pct < -5 else "→")
    return f"{fmt_ms(va)} → {fmt_ms(vb)} ({arrow}{abs(delta_pct):.0f}%)"


def render_comparison(a: dict, b: dict) -> str:
    md = []
    md.append(f"# Bono Sessions Comparison\n")
    md.append(f"**A** : Session #{a.get('id', '?')} {a.get('track', '?')} {a.get('car', '?')}")
    md.append(f"**B** : Session #{b.get('id', '?')} {b.get('track', '?')} {b.get('car', '?')}")
    md.append(f"\n---\n")

    md.append("## Overview\n")
    md.append("| Metric | A | B |")
    md.append("|---|---|---|")
    md.append(f"| Duration (min) | {a.get('duration_s',0)/60:.1f} | {b.get('duration_s',0)/60:.1f} |")
    md.append(f"| Exchanges | {a.get('n_exchanges',0)} | {b.get('n_exchanges',0)} |")
    md.append(f"| Events fired | {a.get('n_events',0)} | {b.get('n_events',0)} |")
    md.append(f"| Pipeline samples | {a.get('n_timings',0)} | {b.get('n_timings',0)} |")
    md.append(f"| Total cost USD | ${a.get('total_cost_usd',0):.4f} | ${b.get('total_cost_usd',0):.4f} |")
    md.append(f"| Cache hit rate | {a.get('cache_hit_rate',0)*100:.0f}% | {b.get('cache_hit_rate',0)*100:.0f}% |")

    md.append("\n## Pipeline Latency (avg → Δ%)\n")
    md.append("| Stage | A→B avg | A→B p95 |")
    md.append("|---|---|---|")
    for stage in ("stt", "llm", "tts", "e2e"):
        md.append(f"| {stage.upper()} | {diff_agg(a.get(stage), b.get(stage), 'avg')} | {diff_agg(a.get(stage), b.get(stage), 'p95')} |")

    md.append("\n## Tools Usage\n")
    all_tools = set(a.get("tools", {}).keys()) | set(b.get("tools", {}).keys())
    if all_tools:
        md.append("| Tool | A | B |")
        md.append("|---|---|---|")
        for t in sorted(all_tools):
            md.append(f"| `{t}` | {a.get('tools', {}).get(t, 0)} | {b.get('tools', {}).get(t, 0)} |")
    else:
        md.append("_No tool usage in either session._")

    md.append("\n## Events Fired\n")
    all_events = set(a.get("events", {}).keys()) | set(b.get("events", {}).keys())
    if all_events:
        md.append("| Event | A | B |")
        md.append("|---|---|---|")
        for e in sorted(all_events):
            md.append(f"| `{e}` | {a.get('events', {}).get(e, 0)} | {b.get('events', {}).get(e, 0)} |")

    md.append("\n## Hardware\n")
    md.append("| Metric | A avg | B avg | Δ |")
    md.append("|---|---|---|---|")
    for key, label in [("cpu", "CPU %"), ("gpu_util", "GPU %"), ("gpu_temp", "GPU °C"), ("vram_mb", "VRAM MB")]:
        va = a.get(key, {}).get("avg") if a.get(key) else None
        vb = b.get(key, {}).get("avg") if b.get(key) else None
        delta = f"{vb - va:+.1f}" if (va is not None and vb is not None) else "-"
        md.append(f"| {label} | {va if va is not None else '-'} | {vb if vb is not None else '-'} | {delta} |")

    md.append("\n## Verdict\n")
    a_e2e = (a.get("e2e") or {}).get("avg")
    b_e2e = (b.get("e2e") or {}).get("avg")
    if a_e2e and b_e2e:
        if b_e2e < a_e2e * 0.95:
            md.append(f"- ✅ Session B avg E2E latency improved by {((a_e2e-b_e2e)/a_e2e)*100:.0f}%")
        elif b_e2e > a_e2e * 1.05:
            md.append(f"- ⚠️ Session B avg E2E latency degraded by {((b_e2e-a_e2e)/a_e2e)*100:.0f}%")
        else:
            md.append("- Session latency stable between A and B")
    a_cost = a.get("total_cost_usd", 0); b_cost = b.get("total_cost_usd", 0)
    if a_cost and b_cost:
        if b_cost > a_cost * 1.3:
            md.append(f"- ⚠️ Cost increased {((b_cost-a_cost)/a_cost)*100:.0f}% between A and B")

    md.append(f"\n---\n*Generated {datetime.now(timezone.utc).isoformat()}*\n")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=int, required=True, help="Session A id")
    ap.add_argument("--b", type=int, required=True, help="Session B id")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sa = session_summary(args.a)
    sb = session_summary(args.b)
    if not sa: print(f"Session {args.a} missing"); sys.exit(1)
    if not sb: print(f"Session {args.b} missing"); sys.exit(1)

    md = render_comparison(sa, sb)
    out_path = Path(args.out) if args.out else SESSIONS_DIR / f"compare_{args.a}_vs_{args.b}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Comparison written : {out_path} ({len(md)} chars)")


if __name__ == "__main__":
    main()
