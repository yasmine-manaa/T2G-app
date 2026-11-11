"""
Charge le dataset "ev_charging_patterns_tunisia.csv" dans la base de données
(users, vehicles, stations, ev_sessions, payments).

Usage :
    python seed.py
"""
import os
import csv
import random
from datetime import datetime, timedelta

from app import create_app
from models import db, User, Vehicle, Station, ChargingSession, Payment, OptimizationResult


def _find_csv_path():
    """Cherche le CSV à plusieurs emplacements possibles (local ou conteneur Docker)."""
    candidates = [
        os.environ.get("DATASET_PATH"),
        os.path.join(os.path.dirname(__file__), "..", "dataset", "ev_charging_patterns_tunisia.csv"),
        "/dataset/ev_charging_patterns_tunisia.csv",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return os.path.join(os.path.dirname(__file__), "..", "dataset", "ev_charging_patterns_tunisia.csv")


CSV_PATH = _find_csv_path()

CHARGER_POWER_MAP = {
    "AC Lent": 7.4, "AC Rapide": 22.0, "DC Rapide": 50.0, "DC Ultra-rapide": 150.0,
}

# Coordonnées approximatives des villes tunisiennes (pour la carte des stations)
CITY_COORDS = {
    "Tunis": (36.8065, 10.1815), "Ariana": (36.8819, 10.1930),
    "La Marsa": (36.8760, 10.3220), "Ben Arous": (36.7532, 10.2280),
    "Sousse": (35.8256, 10.6369), "Sfax": (34.7406, 10.7603),
    "Nabeul": (36.4561, 10.7357), "Monastir": (35.7839, 10.8260),
    "Bizerte": (37.2744, 9.8739), "Gabès": (33.8815, 10.0982),
    "Kairouan": (35.6781, 10.0963), "Djerba": (33.9229, 10.8486),
}

DEMO_VEHICLES = [
    {"model": "Renault Zoe", "battery_capacity_kwh": 52.0, "vehicle_age_years": 1.5,
     "v2g_eligible": True, "max_charge_power_kw": 22.0, "max_discharge_power_kw": 10.0},
    {"model": "Peugeot e-208", "battery_capacity_kwh": 50.0, "vehicle_age_years": 2.0,
     "v2g_eligible": True, "max_charge_power_kw": 7.4, "max_discharge_power_kw": 5.0},
    {"model": "Dacia Spring", "battery_capacity_kwh": 27.4, "vehicle_age_years": 0.5,
     "v2g_eligible": False, "max_charge_power_kw": 7.4, "max_discharge_power_kw": 0.0},
]

# Fenêtres qui croisent plusieurs créneaux tarifaires (pointe → creuses)
# pour que coût classique ≠ coût optimisé.
OVERNIGHT_WINDOWS = [
    (18, 8),   # 18h → 02h
    (19, 10),  # 19h → 05h
    (17, 12),  # 17h → 05h
    (20, 9),   # 20h → 05h
    (21, 8),   # 21h → 05h
    (16, 14),  # 16h → 06h
]


def get_or_create_station(cache, code, city, area, charger_type, power):
    if code in cache:
        return cache[code]
    station = Station.query.filter_by(station_code=code).first()
    if not station:
        lat, lon = CITY_COORDS.get(city, (None, None))
        station = Station(
            station_code=code, city=city, area=area,
            charger_type=charger_type, charger_power_kw=power,
            latitude=lat, longitude=lon,
        )
        db.session.add(station)
        db.session.flush()
    elif station.latitude is None and city in CITY_COORDS:
        station.latitude, station.longitude = CITY_COORDS[city]
    cache[code] = station
    return station


def get_or_create_user(cache, user_code, user_type):
    if user_code in cache:
        return cache[user_code]
    user = User.query.filter_by(user_code=user_code).first()
    if not user:
        user = User(
            user_code=user_code,
            username=user_code.lower(),
            email=f"{user_code.lower()}@t2g-demo.tn",
            user_type=user_type,
        )
        user.set_password("demo1234")
        db.session.add(user)
        db.session.flush()
    cache[user_code] = user
    return user


def get_or_create_vehicle(cache, user, model, capacity, age, v2g_eligible, charger_power):
    key = (user.id, model)
    if key in cache:
        return cache[key]
    vehicle = Vehicle.query.filter_by(user_id=user.id, model=model).first()
    if not vehicle:
        vehicle = Vehicle(
            user_id=user.id,
            model=model,
            battery_capacity_kwh=capacity,
            vehicle_age_years=age,
            v2g_eligible=v2g_eligible,
            max_charge_power_kw=min(charger_power, 22.0),
            max_discharge_power_kw=min(charger_power, 10.0) if v2g_eligible else 0.0,
        )
        db.session.add(vehicle)
        db.session.flush()
    cache[key] = vehicle
    return vehicle


def _price_for_hour(hour):
    from config import Config
    if hour >= 22 or hour < 6:
        return Config.TARIFF_OFFPEAK
    if 18 <= hour < 22:
        return Config.TARIFF_PEAK
    return Config.TARIFF_NORMAL


def _time_of_day_label(hour):
    if 18 <= hour < 22:
        return "Soirée"
    if hour >= 22 or hour < 6:
        return "Nuit"
    return "Jour"


def ensure_demo_vehicles(demo):
    vehicles = Vehicle.query.filter_by(user_id=demo.id).all()
    if vehicles:
        return vehicles
    for v_data in DEMO_VEHICLES:
        vehicle = Vehicle(user_id=demo.id, **v_data)
        db.session.add(vehicle)
        db.session.flush()
        vehicles.append(vehicle)
    return vehicles


def ensure_demo_station():
    station = Station.query.first()
    if station:
        return station
    station = Station(
        station_code="STN-DEMO-01",
        city="Tunis",
        area="Lac",
        charger_type="AC Rapide",
        charger_power_kw=22.0,
    )
    db.session.add(station)
    db.session.flush()
    return station


def create_demo_sessions(demo, n=24, replace_if_empty_or_classique_only=True):
    """Crée des sessions démo avec mix de modes et fenêtres multi-tarifs."""
    existing = ChargingSession.query.filter_by(user_id=demo.id).all()
    modes_present = {s.mode for s in existing}

    needs_seed = (
        len(existing) < 10
        or (replace_if_empty_or_classique_only and modes_present <= {"classique"} and len(existing) > 0)
    )
    # Si déjà un mix de modes et assez de sessions, ne rien faire
    if existing and not needs_seed:
        return 0
    if existing and modes_present <= {"classique"}:
        # Remplacer les sessions demo purement classiques pour débloquer les filtres
        for s in existing:
            OptimizationResult.query.filter_by(session_id=s.id).delete()
            Payment.query.filter_by(session_id=s.id).delete()
            db.session.delete(s)
        db.session.flush()

    vehicles = ensure_demo_vehicles(demo)
    # Utiliser plusieurs stations dans différentes villes pour les filtres
    demo_cities = ["Tunis", "La Marsa", "Sfax", "Sousse", "Nabeul", "Bizerte"]
    demo_stations = []
    for city in demo_cities:
        station = Station.query.filter_by(city=city).first()
        if station:
            demo_stations.append(station)
    if not demo_stations:
        station = ensure_demo_station()
        demo_stations = [station]
    
    modes = (["classique"] * 8) + (["v1g"] * 8) + (["v2g"] * 8)
    random.shuffle(modes)

    created = 0
    for i in range(n):
        vehicle = vehicles[i % len(vehicles)]
        mode = modes[i % len(modes)]
        # Sessions V2G uniquement sur véhicules éligibles
        if mode == "v2g" and not vehicle.v2g_eligible:
            mode = "v1g"
        
        station = demo_stations[i % len(demo_stations)]

        days_ago = random.randint(1, 90)
        arrive_hour, duration_h = OVERNIGHT_WINDOWS[i % len(OVERNIGHT_WINDOWS)]
        # Variante courte en journée pour diversité (1 session sur 4)
        if i % 4 == 3:
            arrive_hour = random.choice([8, 10, 12, 14])
            duration_h = random.uniform(2.0, 4.0)

        start_time = (datetime.now() - timedelta(days=days_ago)).replace(
            hour=arrive_hour, minute=random.choice([0, 15, 30]), second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=duration_h)

        # Énergie réaliste bornée par capacité batterie
        energy_kwh = round(min(
            vehicle.max_charge_power_kw * min(duration_h, 4.0) * 0.55,
            vehicle.battery_capacity_kwh * 0.7,
        ), 1)
        hour = start_time.hour
        price_per_kwh = _price_for_hour(hour)
        # Coût "classique" (référence) ; pour v1g/v2g on simule une économie
        cost_classique = round(energy_kwh * price_per_kwh, 3)
        if mode == "v1g":
            cost_tnd = round(cost_classique * random.uniform(0.55, 0.75), 3)
        elif mode == "v2g":
            cost_tnd = round(cost_classique * random.uniform(0.25, 0.50), 3)
        else:
            cost_tnd = cost_classique

        session = ChargingSession(
            session_code=f"DEMO-{i + 1:04d}",
            user_id=demo.id,
            vehicle_id=vehicle.id,
            station_id=station.id,
            start_time=start_time,
            end_time=end_time,
            duration_h=round(duration_h, 2),
            energy_kwh=energy_kwh,
            charging_rate_kw=round(energy_kwh / max(duration_h, 0.25), 2),
            cost_tnd=cost_tnd,
            price_per_kwh_tnd=price_per_kwh,
            time_of_day=_time_of_day_label(hour),
            day_of_week=start_time.strftime("%A"),
            soc_start_pct=random.randint(15, 40),
            soc_end_pct=random.randint(75, 95),
            soc_target_pct=90,
            distance_since_last_charge_km=random.uniform(50, 200),
            temperature_c=random.uniform(15, 35),
            v2g_eligible=vehicle.v2g_eligible,
            mode=mode,
        )
        db.session.add(session)
        db.session.flush()
        db.session.add(Payment(
            session_id=session.id,
            amount_tnd=cost_tnd,
            payment_type="charge_cost",
            status="confirmé",
        ))
        created += 1

    db.session.commit()
    return created


def ensure_demo_user():
    demo = User.query.filter_by(username="demo").first()
    if not demo:
        last = User.query.order_by(User.id.desc()).first()
        demo = User(
            user_code=f"USR-{(last.id if last else 0) + 1:04d}",
            username="demo",
            email="demo@t2g-demo.tn",
            user_type="Résidentiel",
        )
        demo.set_password("demo1234")
        db.session.add(demo)
        db.session.flush()
        ensure_demo_vehicles(demo)
        db.session.commit()
        print("Compte de démo créé -> identifiant: demo / mot de passe: demo1234")
    else:
        ensure_demo_vehicles(demo)
        db.session.commit()
    return demo


def ensure_admin_user():
    """Compte administrateur : admin / admin1234 (rôle 'admin')."""
    admin = User.query.filter_by(username="admin").first()
    if not admin:
        admin = User(
            user_code="USR-ADMIN",
            username="admin",
            email="admin@t2g.tn",
            user_type="Public/Commercial",
            role="admin",
        )
        admin.set_password("admin1234")
        db.session.add(admin)
        db.session.commit()
        print("Compte administrateur créé -> identifiant: admin / mot de passe: admin1234")
    elif (admin.role or "user") != "admin":
        admin.role = "admin"
        db.session.commit()
    return admin


def ensure_station_coords():
    """Backfill latitude/longitude pour les stations existantes (carte)."""
    updated = 0
    for station in Station.query.filter(Station.latitude.is_(None)).all():
        if station.city in CITY_COORDS:
            station.latitude, station.longitude = CITY_COORDS[station.city]
            updated += 1
    if updated:
        db.session.commit()
        print(f"Coordonnées ajoutées à {updated} stations.")


def run_seed():
    app = create_app()
    with app.app_context():
        demo = User.query.filter_by(username="demo").first()
        demo_session_count = (
            ChargingSession.query.filter_by(user_id=demo.id).count() if demo else 0
        )
        csv_already_loaded = User.query.count() > 0 and not (
            demo and demo_session_count < 10
        )

        # Toujours garantir le compte démo + sessions exploitables
        if User.query.count() == 0:
            if not os.path.exists(CSV_PATH):
                raise SystemExit(
                    f"Dataset introuvable : {CSV_PATH}\n"
                    f"Lancez d'abord : python dataset/generate_dataset.py"
                )

            station_cache, user_cache, vehicle_cache = {}, {}, {}
            count = 0
            with open(CSV_PATH, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    station = get_or_create_station(
                        station_cache,
                        row["charging_station_id"], row["station_city"], row["station_area"],
                        row["charger_type"], float(row["charger_power_kw"]),
                    )
                    user = get_or_create_user(user_cache, row["user_id"], row["user_type"])
                    v2g_eligible = row["v2g_eligible"].strip().lower() == "true"
                    vehicle = get_or_create_vehicle(
                        vehicle_cache, user, row["vehicle_model"],
                        float(row["battery_capacity_kwh"]), float(row["vehicle_age_years"]),
                        v2g_eligible, float(row["charger_power_kw"]),
                    )

                    session = ChargingSession(
                        session_code=row["session_id"],
                        user_id=user.id,
                        vehicle_id=vehicle.id,
                        station_id=station.id,
                        start_time=datetime.strptime(row["charging_start_time"], "%Y-%m-%d %H:%M:%S"),
                        end_time=datetime.strptime(row["charging_end_time"], "%Y-%m-%d %H:%M:%S"),
                        duration_h=float(row["charging_duration_h"]),
                        energy_kwh=float(row["energy_consumed_kwh"]),
                        charging_rate_kw=float(row["charging_rate_kw"]),
                        cost_tnd=float(row["charging_cost_tnd"]),
                        price_per_kwh_tnd=float(row["price_per_kwh_tnd"]),
                        time_of_day=row["time_of_day"],
                        day_of_week=row["day_of_week"],
                        soc_start_pct=int(row["soc_start_pct"]),
                        soc_end_pct=int(row["soc_end_pct"]),
                        soc_target_pct=int(row["soc_target_pct"]),
                        distance_since_last_charge_km=float(row["distance_since_last_charge_km"]),
                        temperature_c=float(row["temperature_c"]),
                        v2g_eligible=v2g_eligible,
                        mode="classique",
                    )
                    db.session.add(session)
                    db.session.flush()
                    db.session.add(Payment(
                        session_id=session.id,
                        amount_tnd=session.cost_tnd,
                        payment_type="charge_cost",
                        status="confirmé",
                    ))
                    count += 1
                    if count % 500 == 0:
                        db.session.commit()
                        print(f"  {count} sessions importées...")

            db.session.commit()
            print(f"Import terminé : {count} sessions, {len(user_cache)} utilisateurs, "
                  f"{len(vehicle_cache)} véhicules, {len(station_cache)} stations.")
        elif csv_already_loaded:
            print("Dataset déjà présent. Mise à jour du compte démo uniquement…")

        demo = ensure_demo_user()
        n = create_demo_sessions(demo)
        if n:
            print(f"{n} sessions de test ajoutées pour l'utilisateur démo (modes classique/v1g/v2g).")
        else:
            print("Sessions démo déjà à jour.")

        ensure_admin_user()
        ensure_station_coords()

        # Entraînement ML si assez de données
        try:
            from prediction_engine import cost_predictor
            all_sessions = ChargingSession.query.limit(2000).all()
            sessions_data = []
            for session in all_sessions:
                d = session.to_dict()
                if session.vehicle:
                    d["battery_capacity_kwh"] = session.vehicle.battery_capacity_kwh
                    d["vehicle_age_years"] = session.vehicle.vehicle_age_years
                sessions_data.append(d)
            if cost_predictor.train(sessions_data):
                print("Modèle ML de coût entraîné au seed.")
        except Exception as e:
            print(f"Entraînement ML ignoré: {e}")


if __name__ == "__main__":
    run_seed()
