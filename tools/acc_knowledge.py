"""Bono v3 — ACC GT3 cars + tracks knowledge base.

Source : community consensus ACC 1.9+ (Pirelli DHF), Kunos BoP, race engineering forums.
Used by tools query_car_knowledge, query_track_knowledge, query_combo_setup_hints.

CRITICAL : tout conseil setup/driving voiture-specifique DOIT consulter ces données
au lieu de génériquer. Sinon Bono = "ChatGPT racing", pas vrai ingé.
"""

# Phase C prereq brief V3 17/05 : PSI mapping formula ACC.
# Source : community ACC consensus (forum AC, Reddit r/ACCompetizione, Kunos manual).
# IMPORTANT : conversion VÉRIFIÉE valide pour ACC 1.9+ Pirelli DHF mais ASSUMPTION sur cold→hot.
# Cold→Hot delta réel dépend de : track temp, surface, stress (qualif vs race), set fresh vs used.
# Valeur 2.0 PSI = approximation moyenne piste sec 25-30°C track temp. À AJUSTER par track météo.
#
# Formule ACC setup JSON :
#   click_value (entier 0-100+ dans le JSON setup) → PSI_cold = 20.3 + click_value * 0.1
#   PSI_hot ≈ PSI_cold + COLD_TO_HOT_DELTA (default 2.0 PSI ; range observée 1.5-2.5)
#
# Exemples :
#   click=45 → cold=24.8 → hot≈26.8 (Pirelli DHF target sec piste tempérée)
#   click=50 → cold=25.3 → hot≈27.3 (chaude piste)
#   click=40 → cold=24.3 → hot≈26.3 (race start, fraîche)
ACC_PSI_BASE_COLD = 20.3
ACC_PSI_PER_CLICK = 0.1
ACC_COLD_TO_HOT_DELTA_DEFAULT = 2.0


def click_to_psi_cold(click_value: int) -> float:
    """Click ACC setup → PSI cold (à la sortie des stands, pneus froids)."""
    return round(ACC_PSI_BASE_COLD + click_value * ACC_PSI_PER_CLICK, 2)


def click_to_psi_hot(click_value: int, cold_to_hot_delta: float = ACC_COLD_TO_HOT_DELTA_DEFAULT) -> float:
    """Click ACC setup → PSI hot (en course, après warmup). Approximation."""
    return round(click_to_psi_cold(click_value) + cold_to_hot_delta, 2)


def psi_hot_target_to_click(psi_hot_target: float, cold_to_hot_delta: float = ACC_COLD_TO_HOT_DELTA_DEFAULT) -> int:
    """PSI hot target (e.g. 26.8 Pirelli DHF) → click value à mettre dans setup ACC."""
    psi_cold = psi_hot_target - cold_to_hot_delta
    click_float = (psi_cold - ACC_PSI_BASE_COLD) / ACC_PSI_PER_CLICK
    return max(0, round(click_float))


# TODO Phase C runtime validation : tester avec setup BMW M4 GT3 Monza :
#   1. Ouvrir un setup avec tyrePressure=[45,45,45,45]
#   2. Faire un out-lap + 2 push laps
#   3. Lire snap.tyre_press_fl/fr/rl/rr en hot (telemetry SHM)
#   4. Vérifier que ~26.8 PSI obs ≈ click_to_psi_hot(45) = 26.8
#   5. Si écart > 0.5 PSI, ajuster ACC_COLD_TO_HOT_DELTA_DEFAULT

# ============================================================
# CARS — ACC GT3 (2026 season car list)
# ============================================================
# Each car :
#  - engine : front / mid / rear, V8/V10/V6/V12, turbo/aspirated, displacement
#  - weight_dist : front/rear bias (approx %)
#  - bb_range : optimal brake bias % range (driver style dependent)
#  - tc1_typical / tc2_typical : community common values
#  - abs_typical : community common
#  - tyre_pressure_target_hot_psi : Pirelli DHF window per axle
#  - arb_front_pref / arb_rear_pref : 0-16 clicks ACC scale
#  - ride_height_pref_mm : low/medium/high characterization
#  - strengths : list of strengths
#  - weaknesses : list of weaknesses
#  - notes : car-specific engineering notes
CARS = {
    "ferrari_488_gt3_evo": {
        "display_name": "Ferrari 488 GT3 Evo",
        "engine": "mid_V8_3.9L_twin_turbo",
        "weight_dist_front_pct": 43,
        "bb_range_pct": [56, 59],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [2, 4],
        "tyre_pressure_target_hot_psi": {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [4, 8],
        "arb_rear_pref": [3, 6],
        "ride_height_pref": "medium-low",
        "strengths": ["traction sortie virage", "balance neutre Pirelli DHF", "freinage tardif"],
        "weaknesses": ["rear management sous fortes accélérations", "compromis aero/mécanique"],
        "notes": "Voiture polyvalente. BB sweet spot 57. Si Dan se plaint rotation : ARB rear -1 ou front_camber -2 click. Si rear instable : preload +1.",
    },
    "ferrari_296_gt3": {
        "display_name": "Ferrari 296 GT3",
        "engine": "mid_V6_3.0L_twin_turbo_hybrid",
        "weight_dist_front_pct": 42,
        "bb_range_pct": [56, 58],
        "tc1_typical": [3, 5],
        "tc2_typical": [3, 5],
        "abs_typical": [2, 4],
        "tyre_pressure_target_hot_psi": {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [4, 7],
        "arb_rear_pref": [3, 5],
        "ride_height_pref": "low",
        "strengths": ["downforce élevé", "rotation virage moyen", "consommation pneu équilibrée"],
        "weaknesses": ["sensible aero (ride height)", "turbo lag moins prononcé que 488"],
        "notes": "Successeur 488 EVO. Plus moderne. Préfère un peu plus de DF que la 488.",
    },
    "bmw_m4_gt3": {
        "display_name": "BMW M4 GT3",
        "engine": "front_inline_6_3.0L_twin_turbo",
        "weight_dist_front_pct": 51,
        "bb_range_pct": [54, 57],
        "tc1_typical": [4, 7],
        "tc2_typical": [4, 6],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [2, 5],
        "arb_rear_pref": [4, 8],
        "ride_height_pref": "medium",
        "strengths": ["stabilité haute vitesse", "freinage puissant", "endurance"],
        "weaknesses": ["inertie rotation virages lents", "front-limited en mid-corner", "compromis dans virages serrés"],
        "notes": "Front-engine = heavy front. Trail braking aide rotation. ARB front mou aide tourner. Si understeer : ARB front -1 ou front_toe +1.",
    },
    "porsche_992_gt3_r": {
        "display_name": "Porsche 911 GT3 R (992)",
        "engine": "rear_flat_6_4.0L_aspirated",
        "weight_dist_front_pct": 39,
        "bb_range_pct": [58, 62],
        "tc1_typical": [4, 7],
        "tc2_typical": [4, 6],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 27.0, "RR": 27.0},
        "arb_front_pref": [3, 7],
        "arb_rear_pref": [5, 10],
        "ride_height_pref": "medium-high",
        "strengths": ["traction sortie de virage (moteur arrière)", "freinage stable", "agilité"],
        "weaknesses": ["snap-oversteer risque sur levage gaz", "transition rapide nécessite finesse"],
        "notes": "Rear-engined = mass derrière essieu = oversteer naturel sur lift. JAMAIS lever gaz brutal mi-virage. BB plus élevé que mid-engined.",
    },
    "porsche_991_ii_gt3_r": {
        "display_name": "Porsche 911 GT3 R (991.II)",
        "engine": "rear_flat_6_4.0L_aspirated",
        "weight_dist_front_pct": 38,
        "bb_range_pct": [58, 62],
        "tc1_typical": [4, 7],
        "tc2_typical": [4, 6],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 27.0, "RR": 27.0},
        "arb_front_pref": [3, 7],
        "arb_rear_pref": [5, 10],
        "ride_height_pref": "medium-high",
        "strengths": ["traction", "freinage stable"],
        "weaknesses": ["snap-oversteer", "ancienne génération vs 992"],
        "notes": "Génération précédente. Caractère similaire 992 mais légèrement moins DF.",
    },
    "mclaren_720s_gt3_evo": {
        "display_name": "McLaren 720S GT3 Evo",
        "engine": "mid_V8_4.0L_twin_turbo",
        "weight_dist_front_pct": 42,
        "bb_range_pct": [55, 58],
        "tc1_typical": [3, 5],
        "tc2_typical": [3, 5],
        "abs_typical": [2, 4],
        "tyre_pressure_target_hot_psi": {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [5, 9],
        "arb_rear_pref": [4, 7],
        "ride_height_pref": "low",
        "strengths": ["downforce le plus élevé GT3 (effet de sol)", "stable haute vitesse", "rotation T7+ Spa"],
        "weaknesses": ["turbo lag prononcé sur out-lap", "sensibilité aero ride height"],
        "notes": "DF queen. Préfère tracks rapides (Spa, Silverstone, Paul Ricard). BB plus bas car DF aide stop. Si turbo lag dérange : ecuMap +1.",
    },
    "audi_r8_lms_evo_ii": {
        "display_name": "Audi R8 LMS Evo II",
        "engine": "mid_V10_5.2L_aspirated",
        "weight_dist_front_pct": 44,
        "bb_range_pct": [56, 59],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [2, 4],
        "tyre_pressure_target_hot_psi": {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [4, 8],
        "arb_rear_pref": [4, 7],
        "ride_height_pref": "medium",
        "strengths": ["équilibre", "moteur aspiré linéaire (pas de turbo lag)", "BoP-friendly"],
        "weaknesses": ["puissance pure légèrement inférieure aux turbos en sortie virage"],
        "notes": "Voiture facile à piloter. Bonne base pour débutants. V10 aspiré = throttle linéaire. Idéal endurance.",
    },
    "mercedes_amg_gt3_evo": {
        "display_name": "Mercedes AMG GT3 Evo",
        "engine": "front_V8_6.2L_aspirated",
        "weight_dist_front_pct": 52,
        "bb_range_pct": [54, 57],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [3, 6],
        "arb_rear_pref": [4, 8],
        "ride_height_pref": "medium-high",
        "strengths": ["puissance V8 aspiré linéaire", "stabilité haute vitesse", "agréable pour endurance"],
        "weaknesses": ["heavy front (front-engine V8)", "rotation virages lents", "consommation pneu avant"],
        "notes": "Front-engined V8 aspiré. Aime ride height un peu plus haut pour rotation. Surveille front_camber et toe (consommation FL/FR avant).",
    },
    "lamborghini_huracan_gt3_evo": {
        "display_name": "Lamborghini Huracan GT3 Evo",
        "engine": "mid_V10_5.2L_aspirated",
        "weight_dist_front_pct": 42,
        "bb_range_pct": [56, 58],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [2, 4],
        "tyre_pressure_target_hot_psi": {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [5, 9],
        "arb_rear_pref": [3, 6],
        "ride_height_pref": "low",
        "strengths": ["downforce élevé", "rotation virage moyen", "agressivité turn-in"],
        "weaknesses": ["sensible bumps (rigid)", "moins endurance friendly"],
        "notes": "Sœur Audi R8 mais châssis plus rigide. Aime tracks lisses (Paul Ricard, Spa). Évite Brands/Imola (bumpy).",
    },
    "lamborghini_huracan_gt3_evo2": {
        "display_name": "Lamborghini Huracan GT3 Evo 2",
        "engine": "mid_V10_5.2L_aspirated",
        "weight_dist_front_pct": 42,
        "bb_range_pct": [56, 58],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [2, 4],
        "tyre_pressure_target_hot_psi": {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [5, 9],
        "arb_rear_pref": [3, 6],
        "ride_height_pref": "low",
        "strengths": ["évolution Evo : meilleur DF arrière", "rotation améliorée"],
        "weaknesses": ["même profil que Evo I sur bumps"],
        "notes": "Évolution 2024. Caractère similaire Evo I, légère amélioration DF rear.",
    },
    "aston_martin_vantage_gt3_amr": {
        "display_name": "Aston Martin Vantage GT3 AMR",
        "engine": "front_V8_4.0L_twin_turbo",
        "weight_dist_front_pct": 50,
        "bb_range_pct": [55, 58],
        "tc1_typical": [4, 7],
        "tc2_typical": [4, 6],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [3, 7],
        "arb_rear_pref": [4, 8],
        "ride_height_pref": "medium",
        "strengths": ["stabilité high speed", "agressivité turn-in", "puissance bas régime"],
        "weaknesses": ["heavy front", "consommation pneu avant"],
        "notes": "Aston AMR (Aston Martin Racing). Sœur philosophique BMW M4 GT3 (front-engine V8 turbo). Setup similaire approche.",
    },
    "honda_nsx_gt3_evo": {
        "display_name": "Honda NSX GT3 Evo",
        "engine": "mid_V6_3.5L_twin_turbo",
        "weight_dist_front_pct": 42,
        "bb_range_pct": [56, 58],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [2, 4],
        "tyre_pressure_target_hot_psi": {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [4, 8],
        "arb_rear_pref": [3, 7],
        "ride_height_pref": "medium",
        "strengths": ["rotation neutre", "puissance turbo V6", "polyvalente"],
        "weaknesses": ["consommation pneu en mode aggressive driving"],
        "notes": "Voiture peu populaire mais compétitive. Caractère similaire Audi R8 mais turbo. Bonne base setup.",
    },
    "lexus_rc_f_gt3": {
        "display_name": "Lexus RC F GT3",
        "engine": "front_V8_5.4L_aspirated",
        "weight_dist_front_pct": 52,
        "bb_range_pct": [54, 57],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [3, 6],
        "arb_rear_pref": [4, 8],
        "ride_height_pref": "medium",
        "strengths": ["fiabilité", "linéarité V8 aspiré", "stable freinage"],
        "weaknesses": ["heavy front", "DF inférieur aux mid-engined"],
        "notes": "Front-engined V8 aspiré 5.4L. Setup philosophie similaire Mercedes AMG.",
    },
    "nissan_gtr_nismo_gt3": {
        "display_name": "Nissan GT-R NISMO GT3",
        "engine": "front_V6_3.8L_twin_turbo",
        "weight_dist_front_pct": 53,
        "bb_range_pct": [54, 57],
        "tc1_typical": [4, 7],
        "tc2_typical": [4, 6],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [3, 6],
        "arb_rear_pref": [4, 8],
        "ride_height_pref": "medium",
        "strengths": ["puissance bas régime", "stable", "BoP-friendly"],
        "weaknesses": ["lourd front", "compromis en virages serrés"],
        "notes": "Heavy front car. AWD-derivé (RWD en GT3) mais châssis biaisé front.",
    },
    "bentley_continental_gt3": {
        "display_name": "Bentley Continental GT3",
        "engine": "front_V8_4.0L_twin_turbo",
        "weight_dist_front_pct": 54,
        "bb_range_pct": [54, 57],
        "tc1_typical": [4, 7],
        "tc2_typical": [4, 6],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [3, 6],
        "arb_rear_pref": [4, 8],
        "ride_height_pref": "medium",
        "strengths": ["torque énorme bas régime", "stable haute vitesse"],
        "weaknesses": ["voiture la plus lourde GT3", "compromis virages lents"],
        "notes": "Heavy car. Aime endurance, stable. Gestion pneu front cruciale.",
    },
    "emil_frey_jaguar_g3_evo": {
        "display_name": "Emil Frey Jaguar G3 Evo",
        "engine": "front_V8_5.0L_aspirated",
        "weight_dist_front_pct": 51,
        "bb_range_pct": [54, 57],
        "tc1_typical": [3, 6],
        "tc2_typical": [3, 5],
        "abs_typical": [3, 5],
        "tyre_pressure_target_hot_psi": {"FL": 26.6, "FR": 26.6, "RL": 26.8, "RR": 26.8},
        "arb_front_pref": [3, 6],
        "arb_rear_pref": [4, 8],
        "ride_height_pref": "medium",
        "strengths": ["agréable", "linéarité aspiré"],
        "weaknesses": ["voiture rare en BoP", "moins compétitive en championnats officiels"],
        "notes": "Niche car. Caractère Mercedes AMG-like.",
    },
}


# ============================================================
# TRACKS — ACC circuits 2026
# ============================================================
# Each track :
#  - length_km, n_turns
#  - downforce_pref : low / medium / high
#  - brake_events : main heavy braking corners
#  - kerb_usage : aggressive / moderate / careful
#  - tyre_stress_zone : where tyres suffer most
#  - pit_loss_s : (verbatim from ACC_PIT_LOSS_S in racing_tools)
#  - weather_volatility : low / medium / high
#  - notes
TRACKS = {
    "spa": {
        "display_name": "Circuit de Spa-Francorchamps",
        "length_km": 7.004,
        "n_turns": 19,
        "downforce_pref": "medium",
        "brake_events": ["T1 (La Source)", "T7 (Les Combes)", "T13 (Bus Stop chicane)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Pouhon (T10-T11) long high-speed left, rear-left heat buildup",
        "pit_loss_s": 26.0,
        "weather_volatility": "high",
        "notes": "Track Bono iconique (Mercedes F1 base). Eau Rouge sensible aero ride. Bus Stop attaque kerb mid. Rain volatil — toujours surveiller forecast.",
    },
    "monza": {
        "display_name": "Autodromo Nazionale Monza",
        "length_km": 5.793,
        "n_turns": 11,
        "downforce_pref": "low",
        "brake_events": ["T1 (Variante del Rettifilo, heavy)", "T4 (Variante della Roggia)", "T8 (Variante Ascari)"],
        "kerb_usage": "aggressive",
        "tyre_stress_zone": "Lesmo I-II (T5-T6) front-loaded long fast right combo",
        "pit_loss_s": 22.0,
        "weather_volatility": "medium",
        "notes": "Low DF (4-6 ACC wing) pour vitesse droite. T1 brake lock-up risque pneu froid. Attack kerbs Variantes maximum.",
    },
    "imola": {
        "display_name": "Autodromo Enzo e Dino Ferrari",
        "length_km": 4.909,
        "n_turns": 19,
        "downforce_pref": "high",
        "brake_events": ["T2 (Tamburello-1)", "T7 (Tosa)", "T14 (Variante Alta)", "T17 (Rivazza-1)"],
        "kerb_usage": "aggressive",
        "tyre_stress_zone": "Acque Minerali (T11-T12) long compression",
        "pit_loss_s": 25.0,
        "weather_volatility": "medium",
        "notes": "High DF (8-10 wing). Bumpy + kerbs attack max. Tamburello compression critical for ride height.",
    },
    "brands_hatch": {
        "display_name": "Brands Hatch GP Circuit",
        "length_km": 3.916,
        "n_turns": 10,
        "downforce_pref": "high",
        "brake_events": ["T1 (Paddock Hill Bend)", "T4 (Druids)", "T7 (Surtees)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Paddock Hill Bend (T1) high-g drop",
        "pit_loss_s": 21.0,
        "weather_volatility": "high",
        "notes": "Track technique anglais. Paddock Hill Bend = drop massive g-load + compression. ARB front aide rotation lent. Souvent pluie UK.",
    },
    "zolder": {
        "display_name": "Circuit Zolder",
        "length_km": 4.011,
        "n_turns": 10,
        "downforce_pref": "medium-high",
        "brake_events": ["T1 (Sterrenwacht)", "T5 (Lucien Bianchi)", "T10 (Terlaemen)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Lucien Bianchi (T5-T6) ess longue",
        "pit_loss_s": 20.0,
        "weather_volatility": "medium",
        "notes": "Belge. Compact. ARB front pour rotation. Pit lane courte = pit loss minimal.",
    },
    "nurburgring": {
        "display_name": "Nurburgring GP-Strecke",
        "length_km": 5.148,
        "n_turns": 16,
        "downforce_pref": "medium",
        "brake_events": ["T1 (Castrol-S)", "T8 (Dunlop Kehre)", "T13 (NGK-Schikane)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Schumacher S (T11-T12)",
        "pit_loss_s": 24.0,
        "weather_volatility": "high",
        "notes": "German GP layout. Climat volatile (Eifel mountain). Mesure pneu importante.",
    },
    "silverstone": {
        "display_name": "Silverstone Circuit",
        "length_km": 5.891,
        "n_turns": 18,
        "downforce_pref": "medium",
        "brake_events": ["T3 (Village)", "T6 (Brooklands)", "T16 (Stowe)"],
        "kerb_usage": "aggressive",
        "tyre_stress_zone": "Copse (T9-T10) sustained right load",
        "pit_loss_s": 23.0,
        "weather_volatility": "high",
        "notes": "British home of F1. Maggots-Becketts-Chapel = roller-coaster, ARB balance critical. Copse pneu droit critique.",
    },
    "paul_ricard": {
        "display_name": "Paul Ricard Circuit",
        "length_km": 5.842,
        "n_turns": 15,
        "downforce_pref": "medium",
        "brake_events": ["T8 (Signes)", "T10 (Beausset)", "T15 (Bendor)"],
        "kerb_usage": "careful",
        "tyre_stress_zone": "Long droite Mistral = pneus arrière chargés",
        "pit_loss_s": 24.0,
        "weather_volatility": "low",
        "notes": "Track lisse (aucun bumps). Mistral straight = test DF/drag balance. McLaren 720S très rapide ici. Run-offs en peinture = limites strictes.",
    },
    "barcelona": {
        "display_name": "Circuit de Barcelona-Catalunya",
        "length_km": 4.655,
        "n_turns": 16,
        "downforce_pref": "high",
        "brake_events": ["T1 (Elf)", "T10 (La Caixa)", "T14 (Europcar)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "T3 (Renault) sustained right",
        "pit_loss_s": 22.0,
        "weather_volatility": "low",
        "notes": "Test track F1 traditionnel. Front-limited. Camber + ARB front cruciaux. T3 = test high speed corner.",
    },
    "misano": {
        "display_name": "Misano World Circuit",
        "length_km": 4.226,
        "n_turns": 16,
        "downforce_pref": "high",
        "brake_events": ["T2 (Variante del Parco)", "T8 (Tramonto)", "T11 (Quercia)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Quercia (T11) long right",
        "pit_loss_s": 21.0,
        "weather_volatility": "low",
        "notes": "Track italien. DF élevé. Kerbs modérés. Slow corners-heavy = TC haut.",
    },
    "suzuka": {
        "display_name": "Suzuka International Racing Course",
        "length_km": 5.807,
        "n_turns": 18,
        "downforce_pref": "high",
        "brake_events": ["T1 (1st corner)", "T11 (Hairpin)", "T15 (Casio Triangle)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Esses S1 (T2-T7) high speed combo",
        "pit_loss_s": 23.0,
        "weather_volatility": "medium",
        "notes": "Figure-8 mythique. Esses = test rotation rapide. 130R = balls courage.",
    },
    "mount_panorama": {
        "display_name": "Mount Panorama (Bathurst)",
        "length_km": 6.213,
        "n_turns": 23,
        "downforce_pref": "medium-high",
        "brake_events": ["T2 (Hell Corner)", "T14 (The Chase)", "T22 (Murray's)"],
        "kerb_usage": "careful",
        "tyre_stress_zone": "Skyline-Esses (T11-T15) descent",
        "pit_loss_s": 30.0,
        "weather_volatility": "medium",
        "notes": "Track légendaire Australian. Mountain section technique. Pit loss élevé. Aero stability critique sur descent.",
    },
    "kyalami": {
        "display_name": "Kyalami Grand Prix Circuit",
        "length_km": 4.522,
        "n_turns": 16,
        "downforce_pref": "medium",
        "brake_events": ["T1 (Crowthorne)", "T8 (Mineshaft)", "T15 (Cheetah)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Sunset (T13) downhill right",
        "pit_loss_s": 21.0,
        "weather_volatility": "low",
        "notes": "Altitude 1.5km = puissance moteur réduite. DF efficacement utile.",
    },
    "laguna_seca": {
        "display_name": "WeatherTech Raceway Laguna Seca",
        "length_km": 3.602,
        "n_turns": 11,
        "downforce_pref": "medium-high",
        "brake_events": ["T2 (Andretti Hairpin)", "T8 (Corkscrew)", "T11 (Sunset)"],
        "kerb_usage": "aggressive",
        "tyre_stress_zone": "Corkscrew (T8) compression blind drop",
        "pit_loss_s": 21.0,
        "weather_volatility": "low",
        "notes": "Iconic Corkscrew. Court mais technique. Kerbs attack T1-T2.",
    },
    "cota": {
        "display_name": "Circuit of the Americas",
        "length_km": 5.513,
        "n_turns": 20,
        "downforce_pref": "medium",
        "brake_events": ["T1 (uphill hairpin)", "T12 (heavy brake)", "T19 (chicane)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Esses (T3-T6) high speed combo",
        "pit_loss_s": 27.0,
        "weather_volatility": "low",
        "notes": "F1 US GP layout. T1 hill iconic. Esses très chargés. ARB balanced.",
    },
    "donington": {
        "display_name": "Donington Park",
        "length_km": 4.020,
        "n_turns": 13,
        "downforce_pref": "high",
        "brake_events": ["T1 (Redgate)", "T6 (Old Hairpin)", "T12 (Roberts)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Craner Curves (T2-T3) descent right",
        "pit_loss_s": 20.0,
        "weather_volatility": "medium",
        "notes": "British classic. Craner Curves = test confiance high-speed downhill. Pit court.",
    },
    "oulton_park": {
        "display_name": "Oulton Park Circuit",
        "length_km": 4.330,
        "n_turns": 17,
        "downforce_pref": "high",
        "brake_events": ["T1 (Old Hall)", "T7 (Cascades)", "T16 (Lodge)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Cascades (T7-T8) blind compression",
        "pit_loss_s": 19.0,
        "weather_volatility": "high",
        "notes": "British track raw. Cascades = test ride height + DF. Pit super court.",
    },
    "snetterton": {
        "display_name": "Snetterton Circuit",
        "length_km": 4.779,
        "n_turns": 13,
        "downforce_pref": "medium",
        "brake_events": ["T3 (Palmer)", "T6 (Nelson)", "T11 (Brundle)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "Coram-Murrays (T8-T9)",
        "pit_loss_s": 20.0,
        "weather_volatility": "medium",
        "notes": "British circuit moderne. Mix high-speed et tight. Bonne base setup test.",
    },
    "hungaroring": {
        "display_name": "Hungaroring",
        "length_km": 4.381,
        "n_turns": 14,
        "downforce_pref": "high",
        "brake_events": ["T1 (heavy)", "T4 (tight)", "T12 (last)"],
        "kerb_usage": "moderate",
        "tyre_stress_zone": "T4 (slow apex) front loaded",
        "pit_loss_s": 21.0,
        "weather_volatility": "medium",
        "notes": "Hot Hungarian summer = tyre management. Track technique slow corners.",
    },
}


# ============================================================
# COMBO HINTS — Car + Track specific recommendations (V3.P : étendu 35+)
# ============================================================
COMBO_HINTS = {
    # === FERRARI 488 GT3 EVO ===
    ("ferrari_488_gt3_evo", "monza"): {
        "wing_target": 6, "bb_target": 57, "tc1_target": 4,
        "advice": "Monza low DF. Wing 6, BB 57. T1 brake lock-up : pression front 26.6 pour warmer plus vite. Attack kerbs Variantes.",
    },
    ("ferrari_488_gt3_evo", "spa"): {
        "wing_target": 8, "bb_target": 58, "tc1_target": 5,
        "advice": "Spa medium DF. Wing 8. Eau Rouge = ride height légèrement haut (anti-scrape compression). BB 58 long brake T1.",
    },
    ("ferrari_488_gt3_evo", "imola"): {
        "wing_target": 10, "bb_target": 58, "tc1_target": 5,
        "advice": "Imola high DF kerb attack. Wing 10. Bumpy = ride height medium. BB 58 freinages multiples.",
    },
    ("ferrari_488_gt3_evo", "brands_hatch"): {
        "wing_target": 11, "bb_target": 57, "arb_front_target": 4,
        "advice": "Brands tight + high DF. Wing 11. ARB front 4 pour Druids rotation. Paddock Hill ride height attention.",
    },
    ("ferrari_488_gt3_evo", "nurburgring"): {
        "wing_target": 8, "bb_target": 58,
        "advice": "Nürburgring GP medium DF. Wing 8. Weather volatil = surveille forecast.",
    },
    ("ferrari_488_gt3_evo", "silverstone"): {
        "wing_target": 8, "bb_target": 57,
        "advice": "Silverstone medium DF wing 8. Copse pneu droit chargé. ARB balanced 5-5 base.",
    },
    ("ferrari_488_gt3_evo", "paul_ricard"): {
        "wing_target": 6, "bb_target": 56,
        "advice": "Paul Ricard Mistral straight = wing 6 low pour vitesse. Track lisse = ride height low OK.",
    },
    # === BMW M4 GT3 ===
    ("bmw_m4_gt3", "brands_hatch"): {
        "wing_target": 10, "bb_target": 55, "arb_front_target": 3,
        "advice": "Brands tight + BMW lourd front. ARB front 3 (mou) pour rotation Druids. BB 55.",
    },
    ("bmw_m4_gt3", "monza"): {
        "wing_target": 5, "bb_target": 55, "tc1_target": 5,
        "advice": "Monza low DF. BMW front-heavy = ARB front mou (3-4). BB 55 (pas trop bias avant heavy car). T1 brake stable.",
    },
    ("bmw_m4_gt3", "spa"): {
        "wing_target": 7, "bb_target": 55,
        "advice": "Spa medium. BMW high speed stable mais lent rotation. ARB front 3. Eau Rouge confidence.",
    },
    ("bmw_m4_gt3", "imola"): {
        "wing_target": 10, "bb_target": 55, "arb_front_target": 3,
        "advice": "Imola tight + bumpy + BMW heavy front. ARB front 3 absolu. Ride height medium-high anti-bump.",
    },
    ("bmw_m4_gt3", "nurburgring"): {
        "wing_target": 8, "bb_target": 55,
        "advice": "Nürburgring GP. BMW stable. ARB front mou.",
    },
    ("bmw_m4_gt3", "silverstone"): {
        "wing_target": 8, "bb_target": 56,
        "advice": "Silverstone high speed. BMW excellent ici. Copse charge front.",
    },
    # === PORSCHE 992 GT3 R ===
    ("porsche_992_gt3_r", "monza"): {
        "wing_target": 5, "bb_target": 60, "tc1_target": 5,
        "advice": "Porsche rear-engine traction max. Monza Variantes lift = ATTENTION snap-oversteer. TC1 5. Wing 5. BB 60 (rear-engine peut bias front élevé).",
    },
    ("porsche_992_gt3_r", "spa"): {
        "wing_target": 7, "bb_target": 60,
        "advice": "Spa long brake T1. Porsche stable freinage. Wing 7 medium. Eau Rouge confidence (Porsche aime compression).",
    },
    ("porsche_992_gt3_r", "imola"): {
        "wing_target": 10, "bb_target": 60,
        "advice": "Imola tight = Porsche agile. Wing 10. ATTENTION lift-off Tamburello = snap risk.",
    },
    ("porsche_992_gt3_r", "brands_hatch"): {
        "wing_target": 11, "bb_target": 60,
        "advice": "Brands tight + Porsche agile = excellent combo. Wing 11. Druids out = traction Porsche shine.",
    },
    ("porsche_992_gt3_r", "laguna_seca"): {
        "wing_target": 10, "bb_target": 60,
        "advice": "Laguna tight + technique. Porsche shine. Corkscrew = compression, ride height medium.",
    },
    # === McLAREN 720S GT3 EVO ===
    ("mclaren_720s_gt3_evo", "spa"): {
        "wing_target": 7, "bb_target": 56,
        "advice": "McLaren DF queen + Spa medium = wing 7. Kemmel vitesse max. Watch turbo lag sortie T1 La Source.",
    },
    ("mclaren_720s_gt3_evo", "monza"): {
        "wing_target": 4, "bb_target": 56,
        "advice": "Monza low DF + McLaren DF = wing 4 minimum. Vitesse droites primordiale.",
    },
    ("mclaren_720s_gt3_evo", "silverstone"): {
        "wing_target": 7, "bb_target": 57,
        "advice": "Silverstone high speed = McLaren paradis. Wing 7. Maggots-Becketts shine.",
    },
    ("mclaren_720s_gt3_evo", "paul_ricard"): {
        "wing_target": 6, "bb_target": 56,
        "advice": "Paul Ricard track ideal McLaren (lisse + Mistral straight). Wing 6. Préfère gros DF arrière 9-10.",
    },
    ("mclaren_720s_gt3_evo", "imola"): {
        "wing_target": 9, "bb_target": 56,
        "advice": "Imola high DF McLaren. Wing 9. Turbo lag = ecuMap +1.",
    },
    # === MERCEDES AMG GT3 EVO ===
    ("mercedes_amg_gt3_evo", "imola"): {
        "wing_target": 9, "bb_target": 55,
        "advice": "Imola bumpy + Mercedes heavy front. Ride height medium-high (anti-scrape Tamburello). BB 55.",
    },
    ("mercedes_amg_gt3_evo", "spa"): {
        "wing_target": 7, "bb_target": 55,
        "advice": "Spa Mercedes V8 aspiré linéaire = confortable. Wing 7. ARB rear 6-7 anti-yaw.",
    },
    ("mercedes_amg_gt3_evo", "brands_hatch"): {
        "wing_target": 10, "bb_target": 55, "arb_front_target": 3,
        "advice": "Brands tight + Mercedes lourd front. ARB front 3 absolu. Ride height medium-high pour Paddock Hill.",
    },
    ("mercedes_amg_gt3_evo", "nurburgring"): {
        "wing_target": 8, "bb_target": 55,
        "advice": "Nürburgring + Mercedes endurance friendly. ARB balanced.",
    },
    # === AUDI R8 LMS EVO II ===
    ("audi_r8_lms_evo_ii", "spa"): {
        "wing_target": 7, "bb_target": 57,
        "advice": "Audi R8 polyvalente, idéale Spa. Wing 7. V10 aspiré linéaire = confidence en sortie virage.",
    },
    ("audi_r8_lms_evo_ii", "monza"): {
        "wing_target": 5, "bb_target": 57,
        "advice": "Monza Audi équilibrée. Wing 5. ARB balanced. Excellent BoP-friendly base.",
    },
    ("audi_r8_lms_evo_ii", "imola"): {
        "wing_target": 9, "bb_target": 57,
        "advice": "Imola Audi. Wing 9. V10 aspiré aide kerb attack (pas de turbo lag).",
    },
    ("audi_r8_lms_evo_ii", "barcelona"): {
        "wing_target": 9, "bb_target": 57,
        "advice": "Barcelona high DF. Audi stable. T3 Renault long right = camber front bien réglé.",
    },
    # === LAMBO HURACAN GT3 EVO ===
    ("lamborghini_huracan_gt3_evo", "paul_ricard"): {
        "wing_target": 7, "bb_target": 57,
        "advice": "Paul Ricard lisse = Lambo idéal. Wing 7. Aime tracks lisses.",
    },
    ("lamborghini_huracan_gt3_evo", "spa"): {
        "wing_target": 8, "bb_target": 57,
        "advice": "Spa Lambo. Wing 8. Châssis rigide = Eau Rouge confidence.",
    },
    ("lamborghini_huracan_gt3_evo", "monza"): {
        "wing_target": 5, "bb_target": 57,
        "advice": "Monza Lambo. Wing 5. Variantes attack kerbs sans peur.",
    },
    # === ASTON MARTIN VANTAGE AMR ===
    ("aston_martin_vantage_gt3_amr", "spa"): {
        "wing_target": 8, "bb_target": 56,
        "advice": "Aston Spa medium DF. Wing 8. Heavy front = ARB front mou.",
    },
    ("aston_martin_vantage_gt3_amr", "silverstone"): {
        "wing_target": 8, "bb_target": 56,
        "advice": "Silverstone high speed + Aston front-engine. ARB balanced.",
    },
    # === FERRARI 296 GT3 ===
    ("ferrari_296_gt3", "spa"): {
        "wing_target": 8, "bb_target": 57,
        "advice": "Ferrari 296 successeur 488. Spa wing 8 similaire 488 mais préfère légèrement plus DF.",
    },
    ("ferrari_296_gt3", "monza"): {
        "wing_target": 6, "bb_target": 57,
        "advice": "Monza Ferrari 296. Wing 6. Plus moderne aero que 488 = sensible ride height.",
    },
    ("ferrari_296_gt3", "imola"): {
        "wing_target": 10, "bb_target": 57,
        "advice": "Imola high DF Ferrari 296. Wing 10. Hybrid V6 sound.",
    },
    # === HONDA NSX GT3 EVO ===
    ("honda_nsx_gt3_evo", "suzuka"): {
        "wing_target": 9, "bb_target": 57,
        "advice": "Honda à Suzuka = home race. Wing 9. Esses NSX excellent rotation.",
    },
    ("honda_nsx_gt3_evo", "spa"): {
        "wing_target": 8, "bb_target": 57,
        "advice": "NSX Spa polyvalente. Wing 8.",
    },
}


def get_car_knowledge(car_id: str) -> dict | None:
    """Returns car knowledge dict, or None if unknown."""
    car_id = (car_id or "").strip().lower()
    return CARS.get(car_id)


def get_track_knowledge(track_id: str) -> dict | None:
    """Returns track knowledge dict, or None if unknown."""
    track_id = (track_id or "").strip().lower().replace("-", "_")
    return TRACKS.get(track_id)


def get_combo_hint(car_id: str, track_id: str) -> dict | None:
    """Returns combo car+track setup hint if available."""
    car_id = (car_id or "").strip().lower()
    track_id = (track_id or "").strip().lower().replace("-", "_")
    return COMBO_HINTS.get((car_id, track_id))


def all_known_cars() -> list[str]:
    return sorted(CARS.keys())


def all_known_tracks() -> list[str]:
    return sorted(TRACKS.keys())
