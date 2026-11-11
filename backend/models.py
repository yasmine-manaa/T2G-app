from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    user_code = db.Column(db.String(20), unique=True, nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    user_type = db.Column(db.String(30), default="Résidentiel")  # Résidentiel / Entreprise/Flotte / Public/Commercial
    role = db.Column(db.String(20), default="user")  # 'user' ou 'admin'
    is_active = db.Column(db.Boolean, default=True)  # False = compte suspendu
    v2g_balance_tnd = db.Column(db.Float, default=0.0)  # solde des gains V2G cumulés
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vehicles = db.relationship("Vehicle", backref="owner", lazy=True)
    sessions = db.relationship("ChargingSession", backref="user", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "user_code": self.user_code,
            "username": self.username,
            "email": self.email,
            "user_type": self.user_type,
            "role": self.role or "user",
            "is_active": bool(self.is_active),
            "v2g_balance_tnd": round(self.v2g_balance_tnd or 0, 3),
        }


class Vehicle(db.Model):
    __tablename__ = "vehicles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    model = db.Column(db.String(80), nullable=False)
    battery_capacity_kwh = db.Column(db.Float, nullable=False)
    vehicle_age_years = db.Column(db.Float, default=0)
    v2g_eligible = db.Column(db.Boolean, default=False)
    max_charge_power_kw = db.Column(db.Float, default=7.4)
    max_discharge_power_kw = db.Column(db.Float, default=5.0)
    battery_efficiency = db.Column(db.Float, default=0.92)  # rendement charge/décharge

    def to_dict(self):
        return {
            "id": self.id,
            "model": self.model,
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "vehicle_age_years": self.vehicle_age_years,
            "v2g_eligible": self.v2g_eligible,
            "max_charge_power_kw": self.max_charge_power_kw,
            "max_discharge_power_kw": self.max_discharge_power_kw,
            "battery_efficiency": self.battery_efficiency if self.battery_efficiency is not None else 0.92,
        }


class Station(db.Model):
    __tablename__ = "stations"

    id = db.Column(db.Integer, primary_key=True)
    station_code = db.Column(db.String(20), unique=True, nullable=False)
    city = db.Column(db.String(60), nullable=False)
    area = db.Column(db.String(80))
    charger_type = db.Column(db.String(30))
    charger_power_kw = db.Column(db.Float)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)

    def to_dict(self):
        return {
            "id": self.id,
            "station_code": self.station_code,
            "city": self.city,
            "area": self.area,
            "charger_type": self.charger_type,
            "charger_power_kw": self.charger_power_kw,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


class ChargingSession(db.Model):
    """Correspond à la table 'ev_charging_patterns / ev_sessions' du cahier des charges."""
    __tablename__ = "ev_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_code = db.Column(db.String(20), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey("stations.id"), nullable=False)

    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    duration_h = db.Column(db.Float)
    energy_kwh = db.Column(db.Float)
    charging_rate_kw = db.Column(db.Float)
    cost_tnd = db.Column(db.Float)
    price_per_kwh_tnd = db.Column(db.Float)

    time_of_day = db.Column(db.String(20))
    day_of_week = db.Column(db.String(20))

    soc_start_pct = db.Column(db.Integer)
    soc_end_pct = db.Column(db.Integer)
    soc_target_pct = db.Column(db.Integer)

    distance_since_last_charge_km = db.Column(db.Float)
    temperature_c = db.Column(db.Float)
    v2g_eligible = db.Column(db.Boolean, default=False)

    mode = db.Column(db.String(20), default="classique")  # classique / v1g / v2g

    vehicle = db.relationship("Vehicle")
    station = db.relationship("Station")

    def to_dict(self):
        return {
            "id": self.id,
            "session_code": self.session_code,
            "user_id": self.user_id,
            "vehicle": self.vehicle.to_dict() if self.vehicle else None,
            "station": self.station.to_dict() if self.station else None,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_h": self.duration_h,
            "energy_kwh": self.energy_kwh,
            "charging_rate_kw": self.charging_rate_kw,
            "cost_tnd": round(self.cost_tnd, 3) if self.cost_tnd is not None else None,
            "price_per_kwh_tnd": self.price_per_kwh_tnd,
            "time_of_day": self.time_of_day,
            "day_of_week": self.day_of_week,
            "soc_start_pct": self.soc_start_pct,
            "soc_end_pct": self.soc_end_pct,
            "soc_target_pct": self.soc_target_pct,
            "distance_since_last_charge_km": self.distance_since_last_charge_km,
            "temperature_c": self.temperature_c,
            "v2g_eligible": self.v2g_eligible,
            "mode": self.mode,
        }


class Booking(db.Model):
    """Réservation d'une session de recharge à venir."""
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    station_id = db.Column(db.Integer, db.ForeignKey("stations.id"), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="confirmée")  # confirmée / annulée / terminée
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vehicle = db.relationship("Vehicle")
    station = db.relationship("Station")
    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "vehicle": self.vehicle.to_dict() if self.vehicle else None,
            "station": self.station.to_dict() if self.station else None,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "status": self.status,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AuditLog(db.Model):
    """Journal d'audit des actions administrateur."""
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(60), nullable=False)  # ex: 'suspend_user', 'reset_password'
    target_type = db.Column(db.String(40))
    target_id = db.Column(db.Integer)
    details = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "admin": self.admin.username if self.admin else None,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "details": self.details,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("ev_sessions.id"), nullable=False)
    amount_tnd = db.Column(db.Float, nullable=False)
    payment_type = db.Column(db.String(20))  # 'charge_cost' ou 'v2g_gain'
    status = db.Column(db.String(20), default="confirmé")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "amount_tnd": round(self.amount_tnd, 3),
            "payment_type": self.payment_type,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class OptimizationResult(db.Model):
    __tablename__ = "optimization_results"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("ev_sessions.id"), nullable=False)
    mode = db.Column(db.String(20))  # v1g / v2g
    baseline_cost_tnd = db.Column(db.Float)
    optimized_cost_tnd = db.Column(db.Float)
    v2g_gain_tnd = db.Column(db.Float, default=0.0)
    savings_tnd = db.Column(db.Float)
    savings_pct = db.Column(db.Float)
    schedule_json = db.Column(db.Text)  # planning horaire (JSON) p(t)/d(t)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "session_id": self.session_id,
            "mode": self.mode,
            "baseline_cost_tnd": round(self.baseline_cost_tnd, 3),
            "optimized_cost_tnd": round(self.optimized_cost_tnd, 3),
            "v2g_gain_tnd": round(self.v2g_gain_tnd or 0, 3),
            "savings_tnd": round(self.savings_tnd, 3),
            "savings_pct": round(self.savings_pct, 1),
            "schedule": json.loads(self.schedule_json) if self.schedule_json else [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
