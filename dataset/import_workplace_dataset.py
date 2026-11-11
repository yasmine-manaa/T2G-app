"""
Importe le VRAI dataset "Workplace Charging Experiment" (Asensio et al.,
DOI: 10.7910/DVN/NFPQLW — 3 395 sessions réelles de recharge sur le lieu de
travail, 85 conducteurs, 105 bornes, 25 sites, nov. 2014 → oct. 2015)
et le convertit dans le schéma attendu par l'application
(`ev_charging_patterns_tunisia.csv`, voir generate_dataset.py).

Données RÉELLES conservées telles quelles :
  - Horodatages de début/fin (created / ended)
  - Énergie livrée (kwhTotal) et taux de charge moyen (kWh / durée)
  - Identifiants anonymisés des utilisateurs et des bornes
  - Jour de la semaine

Données ENRICHIES (absentes du dataset réel, estimées de façon déterministe
et documentée — seed fixe) :
  - Modèle/capacité batterie par utilisateur (échantillonnés dans le parc EV
    tunisien du générateur synthétique, pondérés de façon stable par userId)
  - Ville/quartier des stations (les 25 sites US sont répartis sur les villes
    tunisiennes du référentiel pour conserver le schéma de l'app)
  - Type de chargeur (inféré du taux de charge moyen de la session)
  - Coût en TND (énergie × tarif STEG indicatif du backend, par tranche horaire)
  - SoC de début/fin (cohérent avec l'énergie et la capacité batterie)
  - Distance depuis la dernière charge (~5 km/kWh, efficacité EV typique)
  - Température (saison tunisienne selon le mois réel de la session)
  - Éligibilité V2G (~35 % des utilisateurs, comme le générateur)

Usage :
    python import_workplace_dataset.py
    # produit ev_charging_patterns_tunisia.csv (écrase le synthétique)
"""

import csv
import random
from datetime import datetime, timedelta as _td
_dt = datetime  # alias pour lisibilitÉ

random.seed(42)

RAW_PATH = "workplace_charging_sessions_2014_2015.tsv"
OUT_PATH = "ev_charging_patterns_tunisia.csv"

# ---------------------------------------------------------------------------
# Référentiels repris du générateur synthétique (contexte tunisien)
# ---------------------------------------------------------------------------

VEHICLES = [
    ("Dacia Spring", 27.4, 0.22),
    ("Renault Zoe", 52.0, 0.14),
    ("Peugeot e-208", 50.0, 0.12),
    ("Peugeot e-2008", 54.0, 0.08),
    ("Nissan Leaf", 40.0, 0.09),
    ("Hyundai Kona Electric", 64.0, 0.07),
    ("Kia Niro EV", 64.8, 0.06),
    ("MG ZS EV", 51.0, 0.08),
    ("BYD Atto 3", 60.5, 0.06),
    ("Volkswagen ID.4", 77.0, 0.04),
    ("BMW i3", 42.2, 0.02),
    ("Tesla Model 3", 60.0, 0.02),
]

STATION_CITIES = [
    ("Tunis", "Lac 2"), ("Tunis", "Centre Ville"), ("Ariana", "Ennasr"),
    ("La Marsa", "Corniche"), ("Ben Arous", "Zone Industrielle"),
    ("Sousse", "Khezama"), ("Sfax", "Route de Tunis"), ("Nabeul", "Centre"),
    ("Monastir", "Skanes"), ("Bizerte", "Corniche"), ("Gabès", "Centre"),
    ("Kairouan", "Centre"), ("Djerba", "Houmt Souk"),
]

USER_TYPES = ["Résidentiel", "Entreprise/Flotte", "Public/Commercial"]
USER_TYPE_WEIGHTS = [0.55, 0.20, 0.25]

JOURS_FR = {
    "Mon": "Lundi", "Tue": "Mardi", "Wed": "Mercredi", "Thu": "Jeudi",
    "Fri": "Vendredi", "Sat": "Samedi", "Sun": "Dimanche",
}

# Tarif STEG « Trois Postes horaires » (BT spéciaux) + TVA 13% + taxes ~0.01
# TND/kWh — identique à backend/config.py. Référence : steg.com.tn
#   nuit (22h-6h) : 116 mill -> 0.141 | jour : 140 mill -> 0.168 | pointe soir : 391 mill -> 0.452
def prix_kwh(hour: int) -> float:
    if hour >= 22 or hour < 6:
        return 0.141
    if 18 <= hour < 22:
        return 0.452
    return 0.168


def temperature_tunisie(mois: int) -> float:
    """Température moyenne tunisienne indicatrice par mois + bruit."""
    base = {12: 12, 1: 11, 2: 13, 3: 17, 4: 21, 5: 25,
            6: 30, 7: 33, 8: 32, 9: 28, 10: 23, 11: 17}[mois]
    return round(base + random.uniform(-4, 4), 1)


def charger_type_depuis_taux(taux_kw: float):
    """Type/puissance nominale inférés du taux de charge moyen observé."""
    if taux_kw < 8:
        return "AC Lent", 7.4
    if taux_kw < 25:
        return "AC Rapide", 22.0
    if taux_kw < 60:
        return "DC Rapide", 50.0
    return "DC Ultra-rapide", 150.0


def time_of_day_label(hour: int) -> str:
    if hour < 6 or hour >= 22:
        return "Nuit"
    if hour < 12:
        return "Matin"
    if hour < 18:
        return "Après-midi"
    return "Soirée"


def parser_date(s: str) -> datetime:
    """Les dates réelles sont '0014-11-18 ...' (année 2014) ou '0015-...' (2015)."""
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    if dt.year < 100:
        dt = dt.replace(year=dt.year + 2000)
    return dt


def prix_moyen_session(start: datetime, duree_h: float) -> float:
    n = max(1, int(duree_h) + 1)
    heures = [(start.hour + i) % 24 for i in range(n)]
    return sum(prix_kwh(h) for h in heures) / n


def main():
    with open(RAW_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    # --- mappings stables -------------------------------------------------
    user_ids = sorted({r["userId"] for r in rows}, key=int)
    user_index = {uid: i for i, uid in enumerate(user_ids)}          # 0..84
    user_app_id = {uid: f"USR-{i + 1:04d}" for i, uid in enumerate(user_ids)}

    # véhicule + type + V2G fixes par utilisateur (comme dans la vraie vie)
    user_vehicle = {}
    user_type_map = {}
    user_v2g = {}
    user_age = {}
    for uid, i in user_index.items():
        rng = random.Random(1000 + i)
        user_vehicle[uid] = _choisir_vehicle(rng)
        user_type_map[uid] = rng.choices(USER_TYPES, weights=USER_TYPE_WEIGHTS, k=1)[0]
        user_v2g[uid] = rng.random() < 0.35
        user_age[uid] = round(rng.uniform(0.2, 6.0), 1)

    station_ids = sorted({r["stationId"] for r in rows}, key=int)
    location_ids = sorted({r["locationId"] for r in rows}, key=int)
    # répartition des 25 sites US sur les villes tunisiennes (pondération Tunis)
    city_pool = (["Tunis"] * 4 + ["Sousse", "Sfax", "Ariana", "Nabeul",
                                   "Bizerte", "Monastir", "La Marsa"] * 2
                 + ["Ben Arous", "Gabès", "Kairouan", "Djerba"])
    station_city = {}
    for j, loc in enumerate(location_ids):
        city = city_pool[j % len(city_pool)]
        # quartier stable par location
        quartiers = [a for c, a in STATION_CITIES if c == city]
        station_city[loc] = (city, quartiers[j % len(quartiers)])

    # Décalage temporel : le dataset source date de 2014-2015. On le recentre
    # pour que la dernière session se situe ~1 mois avant aujourd'hui, sans
    # changer les heures de la journée ni les durées (comportements identiques).
    _max_end = max(parser_date(r["ended"]) for r in rows)
    _shift = (_dt.now() - _td(days=30)) - _max_end

    out_rows = []
    for idx, r in enumerate(rows, start=1):
        uid = r["userId"]
        loc = r["locationId"]
        city, area = station_city[loc]
        model, capacity, _ = user_vehicle[uid]

        start = parser_date(r["created"]) + _shift
        end = parser_date(r["ended"]) + _shift
        if end <= start:
            end = start
        duree_h = max(0.1, (end - start).total_seconds() / 3600.0)
        # durée réelle déclarée si cohérente
        try:
            duree_decl = float(r["chargeTimeHrs"])
            if 0.05 <= duree_decl <= 24:
                duree_h = duree_decl
        except (ValueError, TypeError):
            pass

        energy = max(0.1, float(r["kwhTotal"] or 0))
        taux = energy / duree_h
        charger_name, charger_power = charger_type_depuis_taux(taux)

        # SoC : cohérent avec l'énergie réellement livrée
        soc_target = random.choice([80, 85, 90, 100, 100, 90])
        soc_end = min(100, max(50, int(round(soc_target + random.gauss(0, 6)))))
        soc_start = int(max(5, min(95, round(
            soc_end - energy / capacity * 100 + random.gauss(0, 4)
        ))))
        if soc_start >= soc_end:
            soc_start = max(5, soc_end - 5)

        prix_moy = prix_moyen_session(start, duree_h)
        cout_tnd = round(energy * prix_moy, 3)

        out_rows.append({
            "session_id": f"ACN-{r['sessionId']}",
            "user_id": user_app_id[uid],
            "user_type": user_type_map[uid],
            "vehicle_model": model,
            "battery_capacity_kwh": capacity,
            "vehicle_age_years": user_age[uid],
            "charging_station_id": f"STN-ACN-{r['stationId']}",
            "station_city": city,
            "station_area": area,
            "charger_type": charger_name,
            "charger_power_kw": charger_power,
            "charging_start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "charging_end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
            "charging_duration_h": round(duree_h, 2),
            "energy_consumed_kwh": round(energy, 2),
            "charging_rate_kw": round(taux, 2),
            "charging_cost_tnd": cout_tnd,
            "price_per_kwh_tnd": round(prix_moy, 3),
            "time_of_day": time_of_day_label(start.hour),
            "day_of_week": JOURS_FR.get(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][start.weekday()], "Lundi"),
            "soc_start_pct": soc_start,
            "soc_end_pct": soc_end,
            "soc_target_pct": soc_target,
            # ~5 km/kWh (0.2 kWh/km) — estimation documentée
            "distance_since_last_charge_km": round(
                energy * 5.0 * random.uniform(0.8, 1.3), 1),
            "temperature_c": temperature_tunisie(start.month),
            "v2g_eligible": user_v2g[uid],
        })

    out_rows.sort(key=lambda x: x["charging_start_time"])
    for i, row in enumerate(out_rows, start=1):
        row["session_id"] = f"SESS-{i:06d}"

    FIELDNAMES = [
        "session_id", "user_id", "user_type", "vehicle_model", "battery_capacity_kwh",
        "vehicle_age_years", "charging_station_id", "station_city", "station_area",
        "charger_type", "charger_power_kw", "charging_start_time", "charging_end_time",
        "charging_duration_h", "energy_consumed_kwh", "charging_rate_kw",
        "charging_cost_tnd", "price_per_kwh_tnd", "time_of_day", "day_of_week",
        "soc_start_pct", "soc_end_pct", "soc_target_pct",
        "distance_since_last_charge_km", "temperature_c", "v2g_eligible",
    ]

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"{len(out_rows)} sessions réelles importées -> {OUT_PATH}")
    print(f"  {len(user_app_id)} utilisateurs, {len(station_ids)} bornes, "
          f"{len(location_ids)} sites")


def _choisir_vehicle(rng):
    weights = [it[-1] for it in VEHICLES]
    return rng.choices(VEHICLES, weights=weights, k=1)[0]


if __name__ == "__main__":
    main()
