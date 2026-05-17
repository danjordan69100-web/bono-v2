"""Bono — Auto-fix setups WET (pluie) ACC.

Problème détecté 17/05 :
- Tous les _Wet.json sont des copies des _Race.json
- PSI hot estimé = 27.3 alors que target Pirelli DHF wet = 24.3
- BB pas adapté wet (target = nominal - 1.5%)

Fix :
- Réduit tyrePressure de N clicks pour atteindre -2.3 PSI cold→hot wet
- Ajuste brakeBias pour cibler bb_target_wet
- Backup chaque fichier modifié dans .backups/

Usage :
  python fix_wet_setups.py                     # dry-run, liste les modifs
  python fix_wet_setups.py --apply             # applique les modifs (avec backups)
  python fix_wet_setups.py --car bmw_m4_gt3    # filtre par voiture
"""
import os, json, shutil, time, sys, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tools.acc_knowledge import (
    get_car_knowledge, get_session_tyre_target_psi, get_session_bb_target_pct,
    psi_hot_target_to_click, ACC_COLD_TO_HOT_DELTA_DEFAULT,
)

SETUP_ROOT = Path(os.path.expanduser(r"~\Documents\Assetto Corsa Competizione\Setups"))


def fix_wet_setup(path: Path, car: str, apply: bool = False) -> dict:
    """Lit le wet setup et calcule les corrections nécessaires."""
    car_kb = get_car_knowledge(car) or {}
    if not car_kb:
        return {"error": f"no car_kb for {car}", "path": str(path)}
    data = json.load(open(path, encoding="utf-8"))
    bs = data.get("basicSetup", {})
    adv = data.get("advancedSetup", {})
    # 1. Tyre pressures
    tyres = bs.get("tyres", {})
    tp_current = list(tyres.get("tyrePressure") or [50, 50, 50, 50])
    target_psi = get_session_tyre_target_psi(car_kb, "RACE", is_wet=True)
    target_clicks = {
        "FL": psi_hot_target_to_click(target_psi.get("FL", 24.5)),
        "FR": psi_hot_target_to_click(target_psi.get("FR", 24.5)),
        "RL": psi_hot_target_to_click(target_psi.get("RL", 24.5)),
        "RR": psi_hot_target_to_click(target_psi.get("RR", 24.5)),
    }
    new_tp = [target_clicks["FL"], target_clicks["FR"], target_clicks["RL"], target_clicks["RR"]]
    # 2. Brake bias
    mech = adv.get("mechanicalBalance", {})
    bb_raw = mech.get("brakeBias")
    bb_target_wet = get_session_bb_target_pct(car_kb, None, "RACE", is_wet=True)
    new_bb_raw = None
    if bb_raw is not None and bb_target_wet is not None:
        # bb_raw = (BB_pct - 50) * 10
        new_bb_raw = int(round((bb_target_wet - 50) * 10))
    report = {
        "path": str(path.relative_to(SETUP_ROOT)),
        "car": car,
        "tyre_pressure_current": tp_current,
        "tyre_pressure_new": new_tp,
        "bb_raw_current": bb_raw,
        "bb_raw_new": new_bb_raw,
        "bb_pct_current": round(50 + bb_raw / 10.0, 1) if bb_raw is not None else None,
        "bb_pct_new": bb_target_wet,
    }
    if apply:
        # Backup
        backup_dir = path.parent / ".backups"
        backup_dir.mkdir(exist_ok=True)
        bk_name = path.name.replace(".json", f".{int(time.time())}.wetfix.bak.json")
        shutil.copy2(path, backup_dir / bk_name)
        # Apply
        if tyres:
            tyres["tyrePressure"] = new_tp
            bs["tyres"] = tyres
            data["basicSetup"] = bs
        if new_bb_raw is not None:
            mech["brakeBias"] = new_bb_raw
            adv["mechanicalBalance"] = mech
            data["advancedSetup"] = adv
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        report["applied"] = True
        report["backup"] = str(backup_dir / bk_name)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Apply modifs (default: dry-run)")
    ap.add_argument("--car", default="", help="Filter by car_id (e.g. bmw_m4_gt3)")
    args = ap.parse_args()
    cars_dirs = sorted(SETUP_ROOT.iterdir())
    if args.car:
        cars_dirs = [d for d in cars_dirs if d.name == args.car]
    n_modified = 0
    n_total = 0
    print(f"{'MODE':6} {'CAR':25} {'TRACK':22} {'BB now → new':15} {'PSI FL/FR/RL/RR now → new'}")
    print("-" * 130)
    for car_dir in cars_dirs:
        if not car_dir.is_dir(): continue
        car = car_dir.name
        for track_dir in sorted(car_dir.iterdir()):
            if not track_dir.is_dir(): continue
            wet_files = list(track_dir.glob("*_Wet.json"))
            for wf in wet_files:
                n_total += 1
                report = fix_wet_setup(wf, car, apply=args.apply)
                if "error" in report:
                    print(f"  ERR  {car:25} {track_dir.name:22} {report['error'][:80]}")
                    continue
                bb_str = f"{report['bb_pct_current']:.1f} → {report['bb_pct_new']:.1f}"
                tp_str = f"{report['tyre_pressure_current']} → {report['tyre_pressure_new']}"
                changed = (report['tyre_pressure_current'] != report['tyre_pressure_new']
                           or report['bb_raw_current'] != report['bb_raw_new'])
                tag = "FIXED" if (args.apply and changed) else ("MODIF" if changed else "OK")
                if changed: n_modified += 1
                print(f"  {tag:6} {car:25} {track_dir.name:22} {bb_str:15} {tp_str[:60]}")
    print()
    print(f"Total wet setups scanned : {n_total}")
    print(f"Setups needing fix       : {n_modified}")
    if not args.apply:
        print(f"\nDRY-RUN. Pour appliquer : python fix_wet_setups.py --apply")


if __name__ == "__main__":
    main()
