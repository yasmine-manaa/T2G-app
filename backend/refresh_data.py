"""
Actualisation des données dataset (one-shot) :
  1. Décale les sessions importées (SESS-*) pour que la dernière se situe
     ~1 mois avant aujourd'hui (comportements horaires inchangés).
  2. Recalcule price_per_kwh_tnd / cost_tnd avec les tarifs STEG réels
     (backend/config.py).
  3. Recalcule day_of_week à partir des nouvelles dates.
  4. Ré-entraîne le modèle ML de coût.

Usage : python refresh_data.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from models import db, ChargingSession
from config import Config
from optimizer import tarif_tnd_par_kwh

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def prix_moyen_session(start, end):
    """Prix moyen pondéré sur la fenêtre de charge (pas de 15 min)."""
    dt = timedelta(minutes=15)
    t, total, n = start, 0.0, 0
    while t < end:
        total += tarif_tnd_par_kwh(t.hour + t.minute / 60.0, Config)
        n += 1
        t += dt
    return total / n if n else tarif_tnd_par_kwh(start.hour, Config)


def main():
    app = create_app()
    with app.app_context():
        # Sessions dataset (SESS-*) : décalage temporel
        dataset_sessions = ChargingSession.query.filter(
            ChargingSession.session_code.like("SESS-%")
        ).all()
        # Toutes les sessions : recalcul des coûts aux tarifs STEG actuels
        all_sessions = ChargingSession.query.all()

        if dataset_sessions:
            max_end = max(s.end_time for s in dataset_sessions)
            target = datetime.utcnow() - timedelta(days=30)
            shift = target - max_end
            print(f"{len(dataset_sessions)} sessions dataset | décalage = {shift.days} jours")

        for s in all_sessions:
            if s.session_code.startswith("SESS-") and dataset_sessions:
                s.start_time = s.start_time + shift
                s.end_time = s.end_time + shift
                s.day_of_week = JOURS_FR[s.start_time.weekday()]
            prix = prix_moyen_session(s.start_time, s.end_time)
            s.price_per_kwh_tnd = round(prix, 3)
            s.cost_tnd = round(s.energy_kwh * prix, 3)
        db.session.commit()
        print(f"Coûts recalculés pour {len(all_sessions)} sessions (tarifs STEG).")

        # Ré-entraînement ML
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
            print("Modèle ML ré-entrainé.")

        # Vérification
        if dataset_sessions:
            s0 = dataset_sessions[0]
            slast = max(dataset_sessions, key=lambda x: x.end_time)
            print(f"Première session : {s0.start_time} | Dernière : {slast.end_time}")


if __name__ == "__main__":
    main()
