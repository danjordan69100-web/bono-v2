"""Bono — Audit doublons setups ACC (par dossier voiture/track).

Détecte les fichiers .json setup identiques DANS UN MÊME dossier voiture/track.
Exemple typique : Race.json == Practice.json == Hotlap.json (templates default ACC).

Usage :
  python audit_setup_duplicates.py                     # rapport
  python audit_setup_duplicates.py --car bmw_m4_gt3    # filtre voiture
  python audit_setup_duplicates.py --delete-defaults   # supprime _Setup.json si == autre setup (DRY RUN par défaut)
  python audit_setup_duplicates.py --delete-defaults --apply  # apply real
"""
import os, json, hashlib, sys, argparse
from pathlib import Path
from collections import defaultdict

SETUP_ROOT = Path(os.path.expanduser(r"~\Documents\Assetto Corsa Competizione\Setups"))


def setup_sig(path: Path) -> str | None:
    """Hash setup contenu sans fuel/strategy (qui varient légitimement)."""
    try:
        data = json.load(open(path, encoding="utf-8"))
        sig_obj = {
            "basicSetup": data.get("basicSetup", {}),
            "advancedSetup": data.get("advancedSetup", {}),
        }
        bs = sig_obj["basicSetup"]
        if isinstance(bs.get("strategy"), dict):
            bs["strategy"] = {k: v for k, v in bs["strategy"].items() if k != "fuel"}
        return hashlib.md5(json.dumps(sig_obj, sort_keys=True).encode()).hexdigest()[:10]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", default="", help="Filter car_id")
    ap.add_argument("--delete-defaults", action="store_true", help="Delete _Setup.json files if identical to another setup in same dir")
    ap.add_argument("--apply", action="store_true", help="Apply deletion (default: dry-run)")
    args = ap.parse_args()

    total_dirs = 0
    dirs_with_dups = 0
    n_setup_default_to_delete = 0
    deleted = []

    for car_dir in sorted(SETUP_ROOT.iterdir()):
        if not car_dir.is_dir(): continue
        if args.car and car_dir.name != args.car: continue
        for track_dir in sorted(car_dir.iterdir()):
            if not track_dir.is_dir(): continue
            files = [f for f in track_dir.glob("*.json") if f.is_file() and ".backups" not in f.parts]
            if not files: continue
            total_dirs += 1
            sig_to_files = defaultdict(list)
            for f in files:
                sig = setup_sig(f)
                if sig: sig_to_files[sig].append(f)
            groups_with_dups = {s: fs for s, fs in sig_to_files.items() if len(fs) > 1}
            if groups_with_dups:
                dirs_with_dups += 1
                print(f"\n=== {car_dir.name}/{track_dir.name} ===")
                for sig, fs in groups_with_dups.items():
                    names = [f.name for f in fs]
                    print(f"  [{sig}] {len(fs)} fichiers identiques : {', '.join(names)}")
                    if args.delete_defaults:
                        # Find _Setup.json à supprimer si présent dans le groupe
                        setup_default = next((f for f in fs if "_Setup.json" in f.name), None)
                        if setup_default:
                            other_kept = next((f for f in fs if f != setup_default), None)
                            tag = "DELETED" if args.apply else "WOULD-DELETE"
                            n_setup_default_to_delete += 1
                            if args.apply:
                                try:
                                    setup_default.unlink()
                                    deleted.append(str(setup_default.relative_to(SETUP_ROOT)))
                                except Exception as e:
                                    print(f"    ERR delete: {e}")
                            print(f"    [{tag}] {setup_default.name} (identique à {other_kept.name if other_kept else '?'})")

    print()
    print(f"Total dirs scanned       : {total_dirs}")
    print(f"Dirs with duplicates     : {dirs_with_dups}")
    if args.delete_defaults:
        print(f"_Setup.json à supprimer  : {n_setup_default_to_delete}")
        if not args.apply:
            print("\nDRY-RUN. Pour appliquer : python audit_setup_duplicates.py --delete-defaults --apply")
        else:
            print(f"Files deleted           : {len(deleted)}")


if __name__ == "__main__":
    main()
