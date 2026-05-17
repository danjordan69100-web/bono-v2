"""Bono v2 — Session analyzer wrapper.

Génère un rapport markdown post-session en combinant :
- bono.db tables (sessions, exchanges, pipeline_timings, audio_metrics, events, costs)
- bono_sessions/*.csv (perf monitor output)
- presentmon.csv (FPS frametimes, si dispo)

Sortie : bono_sessions/<session_name>.report.md

Usage :
  python analyze_session.py --session-id 5
  python analyze_session.py --auto-latest    # analyse la dernière session bono.db
  python analyze_session.py --csv path.csv   # uniquement perf CSV (sans bono.db)
"""
import sys
import os
import csv
import json
import argparse
import statistics
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent
DB_PATH = ROOT / "db" / "bono.db"
SESSIONS_DIR = Path(r"C:\Users\danjo\Desktop\bono_sessions")


def fmt_ms(v):
    if v is None: return "-"
    if v < 1000: return f"{int(v)}ms"
    return f"{v/1000:.2f}s"


def load_session(session_id: int) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        s = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not s:
            return {}
        timings = [dict(r) for r in conn.execute("SELECT * FROM pipeline_timings WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()]
        audio = [dict(r) for r in conn.execute("SELECT * FROM audio_metrics WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()]
        exch = [dict(r) for r in conn.execute("SELECT * FROM exchanges WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()]
        events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()]
        # Costs across the session window
        st = s["started_at"]; en = s["ended_at"] or st + 86400
        costs = [dict(r) for r in conn.execute("SELECT * FROM costs WHERE ts BETWEEN ? AND ?", (st, en)).fetchall()]
        return {"session": dict(s), "timings": timings, "audio": audio, "exchanges": exch, "events": events, "costs": costs}
    finally:
        conn.close()


def load_latest_session_id() -> int | None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        r = conn.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def load_perf_csv(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    out = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Cast numeric strings
            for k, v in list(row.items()):
                if v in ("", "None"): row[k] = None
                else:
                    try: row[k] = float(v) if "." in v else int(v)
                    except (ValueError, TypeError): pass
            out.append(row)
    return out


def find_perf_csv_for_session(session_started_at: float) -> Path | None:
    """Find perf CSV that matches the session start time (within 5 min)."""
    if not SESSIONS_DIR.exists():
        return None
    candidates = list(SESSIONS_DIR.glob("*.csv"))
    best = None
    best_dt = float("inf")
    for c in candidates:
        try:
            ts_str = c.stem.split("_")[0] + "_" + c.stem.split("_")[1]
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            dt = abs(ts - session_started_at)
            if dt < best_dt and dt < 300:  # within 5 min
                best_dt = dt; best = c
        except Exception:
            pass
    return best


def agg(vals):
    if not vals: return None
    s = sorted(vals)
    return {"n": len(vals), "min": s[0], "max": s[-1],
            "avg": round(sum(vals)/len(vals), 1),
            "p50": s[len(s)//2],
            "p95": s[int(len(s)*0.95)] if len(s) > 1 else s[-1],
            "p99": s[int(len(s)*0.99)] if len(s) > 1 else s[-1]}


def render_report(data: dict, perf_rows: list[dict]) -> str:
    s = data.get("session", {})
    md = []
    started = datetime.fromtimestamp(s.get("started_at", 0), tz=timezone.utc).isoformat()
    ended = datetime.fromtimestamp(s["ended_at"], tz=timezone.utc).isoformat() if s.get("ended_at") else "(in progress)"
    duration_s = (s["ended_at"] - s["started_at"]) if s.get("ended_at") else 0
    md.append(f"# Bono Session Report — #{s.get('id', '?')}\n")
    md.append(f"**Track**: {s.get('track', '?')} | **Car**: {s.get('car', '?')} | **Type**: {s.get('session_type', '?')}\n")
    md.append(f"**Start**: {started} | **End**: {ended} | **Duration**: {duration_s/60:.1f} min\n")
    md.append(f"**Summary**: {s.get('summary', '(none)')}\n\n---\n")

    # Pipeline timings
    timings = data.get("timings", [])
    md.append("## Pipeline Performance\n")
    if timings:
        e2e = [t["total_e2e_ms"] for t in timings if t.get("total_e2e_ms")]
        stt = [t["stt_ms"] for t in timings if t.get("stt_ms")]
        llm = [t["llm_total_ms"] for t in timings if t.get("llm_total_ms")]
        tts = [t["tts_ms"] for t in timings if t.get("tts_ms")]
        play = [t["playback_ms"] for t in timings if t.get("playback_ms")]
        md.append("| Stage | N | min | avg | p50 | p95 | p99 | max |")
        md.append("|---|---|---|---|---|---|---|---|")
        for label, vs in [("STT", stt), ("LLM total", llm), ("TTS", tts), ("Playback", play), ("**E2E total**", e2e)]:
            a = agg(vs)
            if a:
                md.append(f"| {label} | {a['n']} | {fmt_ms(a['min'])} | {fmt_ms(a['avg'])} | {fmt_ms(a['p50'])} | {fmt_ms(a['p95'])} | {fmt_ms(a['p99'])} | {fmt_ms(a['max'])} |")
    else:
        md.append("_No pipeline timings logged for this session._\n")

    # Tools usage
    md.append("\n## Tools Usage\n")
    tool_counts = {}
    cache_hits = 0
    parallel_total = 0
    for t in timings:
        try:
            tools = json.loads(t.get("tools_used", "[]")) if isinstance(t.get("tools_used"), str) else (t.get("tools_used") or [])
            for tn in tools:
                tool_counts[tn] = tool_counts.get(tn, 0) + 1
            if t.get("cache_hit"): cache_hits += 1
            if (t.get("tools_parallel") or 0) > 1: parallel_total += 1
        except Exception: pass
    if tool_counts:
        md.append("| Tool | Calls |")
        md.append("|---|---|")
        for tn, n in sorted(tool_counts.items(), key=lambda kv: -kv[1]):
            md.append(f"| `{tn}` | {n} |")
        md.append(f"\n- Cache hits: {cache_hits}/{len(timings)} ({100*cache_hits/max(1,len(timings)):.0f}%)")
        md.append(f"- Parallel multi-tool turns: {parallel_total}")
    else:
        md.append("_No tool usage._")

    # Cost
    md.append("\n## Cost\n")
    costs = data.get("costs", [])
    if costs:
        total = sum(c.get("cost_usd", 0) for c in costs)
        by_model = {}
        for c in costs:
            by_model.setdefault(c["model"], 0)
            by_model[c["model"]] += c.get("cost_usd", 0)
        md.append(f"**Total**: ${total:.4f}\n")
        md.append("| Model | Cost |")
        md.append("|---|---|")
        for m, cs in by_model.items():
            md.append(f"| {m} | ${cs:.4f} |")
    else:
        md.append("_No costs logged._")

    # Events
    events = data.get("events", [])
    md.append(f"\n## Auto-Events ({len(events)})\n")
    if events:
        types = {}
        for e in events:
            types[e["type"]] = types.get(e["type"], 0) + 1
        md.append("| Type | Severity | Count |")
        md.append("|---|---|---|")
        for t, n in sorted(types.items(), key=lambda kv: -kv[1]):
            sev = next((e["severity"] for e in events if e["type"] == t), "?")
            md.append(f"| `{t}` | {sev} | {n} |")
    else:
        md.append("_No auto-events fired._")

    # Exchanges sample
    md.append(f"\n## PTT Exchanges ({len(data.get('exchanges', []))})\n")
    exch = data.get("exchanges", [])
    if exch:
        md.append("| Time | Transcript | Bono response | Model | Latency |")
        md.append("|---|---|---|---|---|")
        for e in exch[:15]:
            ts = datetime.fromtimestamp(e["ts"], tz=timezone.utc).strftime("%H:%M:%S")
            md.append(f"| {ts} | {(e.get('driver_msg') or '')[:60]} | {(e.get('bono_msg') or '')[:60]} | {e.get('model', '?')} | {fmt_ms(e.get('latency_ms'))} |")
        if len(exch) > 15:
            md.append(f"\n_... {len(exch)-15} more exchanges._")

    # Hardware perf (from CSV)
    if perf_rows:
        md.append("\n## Hardware Performance\n")
        cpu = [r["cpu_pct"] for r in perf_rows if isinstance(r.get("cpu_pct"), (int, float))]
        gpu = [r["gpu_util_pct"] for r in perf_rows if isinstance(r.get("gpu_util_pct"), (int, float))]
        ram = [r["ram_pct"] for r in perf_rows if isinstance(r.get("ram_pct"), (int, float))]
        temp = [r["gpu_temp_c"] for r in perf_rows if isinstance(r.get("gpu_temp_c"), (int, float))]
        vram = [r["vram_used_mb"] for r in perf_rows if isinstance(r.get("vram_used_mb"), (int, float))]
        md.append(f"_Samples: {len(perf_rows)} (~{len(perf_rows)}s @ 1Hz)_\n")
        md.append("| Metric | min | avg | p95 | max |")
        md.append("|---|---|---|---|---|")
        for label, vs in [("CPU %", cpu), ("GPU %", gpu), ("RAM %", ram), ("GPU °C", temp), ("VRAM MB", vram)]:
            a = agg(vs)
            if a:
                md.append(f"| {label} | {a['min']} | {a['avg']} | {a['p95']} | {a['max']} |")

        # API ping
        for api in ("anthropic", "deepgram", "elevenlabs", "fish_audio"):
            pings = [r[f"api_{api}_ms"] for r in perf_rows if isinstance(r.get(f"api_{api}_ms"), (int, float)) and r[f"api_{api}_ms"] > 0]
            a = agg(pings)
            if a:
                md.append(f"\n- **API {api}**: avg {a['avg']}ms (p95 {a['p95']}ms) on {a['n']} pings")
    else:
        md.append("\n## Hardware Performance\n_No perf CSV found for this session window._")

    # Recos / observations heuristiques
    md.append("\n## Observations\n")
    obs = []
    if timings:
        e2e = [t["total_e2e_ms"] for t in timings if t.get("total_e2e_ms")]
        if e2e:
            avg_e2e = sum(e2e) / len(e2e)
            if avg_e2e > 3000:
                obs.append(f"- ⚠️ E2E latency avg {avg_e2e:.0f}ms > 3s — investigate STT/LLM/TTS bottleneck")
            elif avg_e2e < 2000:
                obs.append(f"- ✅ E2E latency avg {avg_e2e:.0f}ms < 2s — good responsiveness")
    if perf_rows:
        cpu = [r["cpu_pct"] for r in perf_rows if isinstance(r.get("cpu_pct"), (int, float))]
        if cpu and max(cpu) > 90:
            obs.append(f"- ⚠️ CPU peak {max(cpu)}% — possible CPU saturation")
        temp = [r["gpu_temp_c"] for r in perf_rows if isinstance(r.get("gpu_temp_c"), (int, float))]
        if temp and max(temp) > 85:
            obs.append(f"- ⚠️ GPU temp peak {max(temp)}°C — thermal throttle risk")
    if not obs:
        obs.append("- Session metrics nominal.")
    md.extend(obs)

    md.append("\n---\n*Generated by analyze_session.py at " + datetime.now(timezone.utc).isoformat() + "*\n")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", type=int, default=None)
    ap.add_argument("--auto-latest", action="store_true")
    ap.add_argument("--out", default=None, help="Output md file path")
    args = ap.parse_args()

    sid = args.session_id
    if args.auto_latest or sid is None:
        sid = load_latest_session_id()
        if sid is None:
            print("No sessions in bono.db, abort.")
            sys.exit(1)
        print(f"[auto-latest] session_id={sid}")

    data = load_session(sid)
    if not data:
        print(f"Session {sid} not found.")
        sys.exit(1)

    # Find matching perf CSV
    perf_csv = find_perf_csv_for_session(data["session"]["started_at"])
    perf_rows = load_perf_csv(perf_csv) if perf_csv else []
    if perf_csv:
        print(f"[perf-csv] matched: {perf_csv.name} ({len(perf_rows)} samples)")
    else:
        print("[perf-csv] no matching CSV found")

    report = render_report(data, perf_rows)
    out_path = Path(args.out) if args.out else SESSIONS_DIR / f"session_{sid}_report.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"Report written : {out_path} ({len(report)} chars)")


if __name__ == "__main__":
    main()
