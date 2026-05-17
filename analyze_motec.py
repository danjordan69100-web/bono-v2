"""Bono — Analyse fichiers MoTeC ACC (.ld) via ldparser.

Usage :
  python analyze_motec.py                  # auto: last .ld
  python analyze_motec.py <path/to/.ld>    # specific file
  python analyze_motec.py --list           # list all .ld available
  python analyze_motec.py --channels       # list channels of last .ld

Installé via : git clone gotzl/ldparser → /c/dev/ldparser_lib/
Requirements : matplotlib + numpy (installed)
"""
import sys
import os
from pathlib import Path

LDPARSER_PATH = "C:/dev/ldparser_lib"
sys.path.insert(0, LDPARSER_PATH)

try:
    from ldparser import read_ldfile
except ImportError:
    print(f"[ERR] ldparser not installed. Clone : git clone https://github.com/gotzl/ldparser.git {LDPARSER_PATH}")
    sys.exit(1)

MOTEC_DIR = Path(os.path.expanduser(r"~\Documents\Assetto Corsa Competizione\MoTeC"))


def list_ld_files() -> list[Path]:
    files = sorted(MOTEC_DIR.glob("*.ld"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files


def summarize(path: Path) -> dict:
    head, chans = read_ldfile(str(path))
    return {
        "file": path.name,
        "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
        "venue": head.venue,
        "vehicleid": head.vehicleid,
        "driver": head.driver,
        "datetime": str(head.datetime),
        "n_channels": len(chans),
        "channels": [{"name": c.name, "unit": c.unit, "freq_hz": c.freq, "samples": c.data_len} for c in chans],
    }


def analyze_pace(path: Path, lap_filter: int | None = None) -> dict:
    """Quick pace analysis : lap times via LAP_BEACON channel + summary stats."""
    head, chans = read_ldfile(str(path))
    by_name = {c.name: c for c in chans}
    speed = by_name.get("SPEED")
    throttle = by_name.get("THROTTLE")
    brake = by_name.get("BRAKE")
    rpms = by_name.get("RPMS")
    gear = by_name.get("GEAR")
    summary = {
        "file": path.name,
        "venue": head.venue,
        "datetime": str(head.datetime),
        "speed_max_kmh": round(max(speed.data) * 3.6, 1) if speed else None,
        "throttle_avg_pct": round(sum(throttle.data) / len(throttle.data) * 100, 1) if throttle else None,
        "brake_avg_pct": round(sum(brake.data) / len(brake.data) * 100, 1) if brake else None,
        "rpm_max": round(max(rpms.data), 0) if rpms else None,
        "gear_max": max(gear.data) if gear else None,
        "n_samples": speed.data_len if speed else 0,
    }
    return summary


def main():
    args = sys.argv[1:]
    if "--list" in args:
        files = list_ld_files()
        print(f"Found {len(files)} .ld files in {MOTEC_DIR}:")
        for f in files[:15]:
            print(f"  [{f.stat().st_size:>10,}] {f.name}")
        return
    if "--channels" in args:
        files = list_ld_files()
        if not files:
            print("No .ld found.")
            return
        info = summarize(files[0])
        print(f"=== {info['file']} ===")
        print(f"  venue={info['venue']} datetime={info['datetime']}")
        print(f"  channels ({info['n_channels']}):")
        for c in info["channels"]:
            print(f"    {c['name']:25} {c['unit']:8} {c['freq_hz']:>4}Hz  {c['samples']:>9} samples")
        return
    if args and not args[0].startswith("--"):
        path = Path(args[0])
    else:
        files = list_ld_files()
        if not files:
            print("No .ld file in MoTeC dir.")
            return
        path = files[0]
    print(f"=== Analyzing {path.name} ===")
    pace = analyze_pace(path)
    for k, v in pace.items():
        print(f"  {k:25} {v}")


if __name__ == "__main__":
    main()
