"""
Générateur de dataset "EV Charging Patterns - Tunisie"
--------------------------------------------------------
Ce script génère un dataset RÉALISTE de sessions de recharge de véhicules
électriques, structuré comme le dataset Kaggle "EV Charging Patterns"
(colonnes identiques : User ID, Vehicle Model, Battery Capacity, SoC, etc.)
mais localisé au contexte tunisien :
  - Prix en Dinar Tunisien (TND), avec tarification heures creuses / pleines
    / pointe inspirée de la structure tarifaire STEG (tarifs indicatifs,
    à ajuster avec les vrais tarifs STEG si besoin).
  - Stations de recharge dans les principales villes tunisiennes.
  - Parc automobile représentatif du marché EV tunisien (Dacia Spring,
    Renault Zoe, Peugeot e-208/e-2008, MG ZS EV, BYD Atto 3, Tesla Model 3...).

Le dataset généré alimente directement la base de données de l'application
(table ev_charging_patterns / ev_sessions) et sert de données d'entraînement
/ simulation pour le moteur d'optimisation IA (V1G / V2G).
"""

import csv
import random
import math
from datetime import datetime, timedelta

random.seed(42)

# ---------------------------------------------------------------------------
# Référentiels "Tunisie"
# ---------------------------------------------------------------------------

VEHICLES = [
    # (modèle, capacité batterie kWh, part de marché approx.)
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

STATIONS = [
    # (id, ville, quartier)
    ("STN-TUN-01", "Tunis", "Lac 2"),
    ("STN-TUN-02", "Tunis", "Centre Ville"),
    ("STN-TUN-03", "Ariana", "Ennasr"),
    ("STN-TUN-04", "La Marsa", "Corniche"),
    ("STN-BEN-01", "Ben Arous", "Zone Industrielle"),
    ("STN-SOU-01", "Sousse", "Khezama"),
    ("STN-SFA-01", "Sfax", "Route de Tunis"),
    ("STN-NAB-01", "Nabeul", "Centre"),
    ("STN-MON-01", "Monastir", "Skanes"),
    ("STN-BIZ-01", "Bizerte", "Corniche"),
    ("STN-GAB-01", "Gabès", "Centre"),
    ("STN-KAI-01", "Kairouan", "Centre"),
    ("STN-DJE-01", "Djerba", "Houmt Souk"),
]

CHARGER_TYPES = [
    ("AC Lent", 7.4, 0.55),
    ("AC Rapide", 22.0, 0.25),
    ("DC Rapide", 50.0, 0.15),
    ("DC Ultra-rapide", 150.0, 0.05),
]

USER_TYPES = ["Résidentiel", "Entreprise/Flotte", "Public/Commercial"]

# Tarification STEG « Trois Postes horaires » (TND / kWh) — identique à
# backend/config.py : nuit 0.141 | pointe soir 0.452 | jour 0.168
def prix_kwh(hour: int) -> float:
    if 22 <= hour or hour < 6:          # nuit / heures creuses
        return 0.141
    if 18 <= hour < 22:                 # pointe soir
        return 0.452
    return 0.168                        # jour / heures pleines


def choisir_pondere(items):
    """items: list of (value, ..., weight) -> retourne 'value' pondéré."""
    weights = [it[-1] for it in items]
    return random.choices(items, weights=weights, k=1)[0]


def generer_dataset(n_users=220, n_sessions=3200, seed_start=datetime(2026, 3, 1)):
    rows = []
    users = [f"USR-{i:04d}" for i in range(1, n_users + 1)]
    # on associe un véhicule fixe par utilisateur (comme dans la vraie vie)
    user_vehicle = {u: choisir_pondere(VEHICLES) for u in users}
    user_type_map = {u: random.choices(USER_TYPES, weights=[0.55, 0.20, 0.25])[0] for u in users}
    user_age_map = {u: round(random.uniform(0.2, 6.0), 1) for u in users}

    session_id = 1
    for _ in range(n_sessions):
        user = random.choice(users)
        model, capacity, _ = user_vehicle[user]
        station_id, city, quartier = random.choice(STATIONS)
        charger_name, charger_power, _ = choisir_pondere(CHARGER_TYPES)
        user_type = user_type_map[user]

        # date aléatoire sur ~7 mois
        day_offset = random.randint(0, 210)
        # heures d'arrivée réalistes : pics domicile (19-23h) et travail (7-9h)
        heure_arrivee = random.choices(
            population=list(range(24)),
            weights=[3,2,1,1,1,1,2,6,5,3,2,2,2,2,2,2,3,4,7,9,8,6,5,4],
            k=1
        )[0]
        start_dt = seed_start + timedelta(days=day_offset, hours=heure_arrivee,
                                           minutes=random.choice([0, 15, 30, 45]))

        soc_start = max(5, min(95, int(random.gauss(38, 18))))
        soc_target = random.choice([80, 85, 90, 100, 100, 90])
        soc_end = min(100, max(soc_start + 5, int(random.gauss(soc_target, 6))))

        energie_necessaire = capacity * (soc_end - soc_start) / 100.0
        energie_necessaire = max(0.5, energie_necessaire)

        duree_h = energie_necessaire / max(charger_power * 0.9, 1.0)
        duree_h = max(0.25, min(duree_h, 10.0))
        end_dt = start_dt + timedelta(hours=duree_h)

        charging_rate = round(energie_necessaire / duree_h, 2)

        # coût moyen pondéré selon les heures traversées par la session
        heures_traversees = [ (start_dt + timedelta(hours=h)).hour for h in range(int(math.ceil(duree_h)) + 1) ]
        prix_moyen = sum(prix_kwh(h) for h in heures_traversees) / len(heures_traversees)
        cout_tnd = round(energie_necessaire * prix_moyen, 3)

        distance_depuis_derniere_charge = round(random.uniform(15, 320), 1)
        temperature = round(random.uniform(9, 39), 1)  # climat tunisien

        time_of_day = (
            "Nuit" if start_dt.hour < 6 or start_dt.hour >= 22 else
            "Matin" if start_dt.hour < 12 else
            "Après-midi" if start_dt.hour < 18 else "Soirée"
        )
        jours_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

        rows.append({
            "session_id": f"SESS-{session_id:06d}",
            "user_id": user,
            "user_type": user_type,
            "vehicle_model": model,
            "battery_capacity_kwh": capacity,
            "vehicle_age_years": user_age_map[user],
            "charging_station_id": station_id,
            "station_city": city,
            "station_area": quartier,
            "charger_type": charger_name,
            "charger_power_kw": charger_power,
            "charging_start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "charging_end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "charging_duration_h": round(duree_h, 2),
            "energy_consumed_kwh": round(energie_necessaire, 2),
            "charging_rate_kw": charging_rate,
            "charging_cost_tnd": cout_tnd,
            "price_per_kwh_tnd": round(prix_moyen, 3),
            "time_of_day": time_of_day,
            "day_of_week": jours_fr[start_dt.weekday()],
            "soc_start_pct": soc_start,
            "soc_end_pct": soc_end,
            "soc_target_pct": soc_target,
            "distance_since_last_charge_km": distance_depuis_derniere_charge,
            "temperature_c": temperature,
            "v2g_eligible": random.random() < 0.35,  # véhicule/borne compatible V2G
        })
        session_id += 1

    return rows


FIELDNAMES = [
    "session_id", "user_id", "user_type", "vehicle_model", "battery_capacity_kwh",
    "vehicle_age_years", "charging_station_id", "station_city", "station_area",
    "charger_type", "charger_power_kw", "charging_start_time", "charging_end_time",
    "charging_duration_h", "energy_consumed_kwh", "charging_rate_kw",
    "charging_cost_tnd", "price_per_kwh_tnd", "time_of_day", "day_of_week",
    "soc_start_pct", "soc_end_pct", "soc_target_pct",
    "distance_since_last_charge_km", "temperature_c", "v2g_eligible",
]

if __name__ == "__main__":
    data = generer_dataset()
    out_path = "ev_charging_patterns_tunisia.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(data)
    print(f"{len(data)} sessions générées -> {out_path}")
