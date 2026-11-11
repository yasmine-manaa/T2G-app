import os
import csv
import io
import time
import functools
import jwt
import numpy as np
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

from config import Config, BASE_DIR
from models import db, User, Vehicle, Station, ChargingSession, Payment, OptimizationResult, Booking, AuditLog
from optimizer import optimiser_session
from prediction_engine import cost_predictor, time_predictor, rag_system, demand_predictor
from chatbot import T2GAssistant, CO2_KG_PER_KWH


def _parse_booking_dt(value):
    """Parse 'YYYY-MM-DD HH:MM' ou ISO datetime ; retourne un datetime naïf ou None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    # CORS restreint aux origines locales de dev (tout port localhost/127.0.0.1)
    CORS(app, origins=[r"https?://(localhost|127\.0\.0\.1)(:\d+)?"])
    db.init_app(app)

    with app.app_context():
        os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
        db.create_all()
        # Migrations légères sur bases existantes
        try:
            from sqlalchemy import inspect, text
            cols = [c["name"] for c in inspect(db.engine).get_columns("users")]
            for col, ddl in [
                ("role", "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'"),
                ("is_active", "ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1"),
            ]:
                if col not in cols:
                    db.session.execute(text(ddl))
            scols = [c["name"] for c in inspect(db.engine).get_columns("stations")]
            for col in ("latitude", "longitude"):
                if col not in scols:
                    db.session.execute(text(f"ALTER TABLE stations ADD COLUMN {col} FLOAT"))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # ---------------------------------------------------------------
    # Auth helpers (JWT léger)
    # ---------------------------------------------------------------
    def generate_token(user: User) -> str:
        payload = {
            "user_id": user.id,
            "username": user.username,
            "exp": datetime.now(timezone.utc) + Config.JWT_EXP_DELTA,
        }
        return jwt.encode(payload, Config.SECRET_KEY, algorithm="HS256")

    def token_required(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return jsonify({"error": "Token manquant"}), 401
            token = auth_header.split(" ", 1)[1]
            try:
                payload = jwt.decode(token, Config.SECRET_KEY, algorithms=["HS256"])
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Token expiré"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Token invalide"}), 401
            user = db.session.get(User, int(payload["user_id"]))
            if not user:
                return jsonify({"error": "Utilisateur introuvable"}), 401
            if not user.is_active:
                return jsonify({"error": "Compte suspendu. Contactez un administrateur."}), 403
            request.current_user = user
            return f(*args, **kwargs)
        return wrapper

    def admin_required(f):
        @functools.wraps(f)
        @token_required
        def wrapper(*args, **kwargs):
            if (request.current_user.role or "user") != "admin":
                return jsonify({"error": "Accès réservé aux administrateurs"}), 403
            return f(*args, **kwargs)
        return wrapper

    # ---------------------------------------------------------------
    # Rate limiting simple en mémoire (anti brute-force)
    # ---------------------------------------------------------------
    _rate_buckets = defaultdict(lambda: deque())

    def rate_limit(max_requests: int, window_sec: int):
        def decorator(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                key = request.remote_addr or "unknown"
                now = time.time()
                bucket = _rate_buckets[key]
                while bucket and now - bucket[0] > window_sec:
                    bucket.popleft()
                if len(bucket) >= max_requests:
                    return jsonify({"error": "Trop de requêtes, réessayez dans une minute."}), 429
                bucket.append(now)
                return f(*args, **kwargs)
            return wrapper
        return decorator

    def valid_password(pw: str) -> bool:
        return isinstance(pw, str) and len(pw) >= 8 and any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw)

    def audit(admin: User, action: str, target_type=None, target_id=None, details=None):
        db.session.add(AuditLog(
            admin_id=admin.id, action=action,
            target_type=target_type, target_id=target_id, details=details,
        ))

    # ---------------------------------------------------------------
    # Auth
    # ---------------------------------------------------------------
    @app.post("/api/auth/register")
    @rate_limit(10, 60)
    def register():
        data = request.get_json(force=True)
        for field in ("username", "email", "password"):
            if not data.get(field):
                return jsonify({"error": f"Champ requis manquant : {field}"}), 400
        if not valid_password(data["password"]):
            return jsonify({"error": "Mot de passe trop faible : 8 caractères min., lettres et chiffres requis."}), 400
        if User.query.filter_by(username=data["username"]).first():
            return jsonify({"error": "Nom d'utilisateur déjà pris"}), 409
        if User.query.filter_by(email=data["email"]).first():
            return jsonify({"error": "Email déjà utilisé"}), 409

        last = User.query.order_by(User.id.desc()).first()
        next_num = (last.id if last else 0) + 1
        user = User(
            user_code=f"USR-{next_num:04d}",
            username=data["username"],
            email=data["email"],
            user_type=data.get("user_type", "Résidentiel"),
        )
        user.set_password(data["password"])
        db.session.add(user)
        db.session.commit()

        # véhicule par défaut à la création (modifiable ensuite)
        vehicle = Vehicle(
            user_id=user.id,
            model=data.get("vehicle_model", "Dacia Spring"),
            battery_capacity_kwh=float(data.get("battery_capacity_kwh", 27.4)),
            v2g_eligible=bool(data.get("v2g_eligible", False)),
            max_charge_power_kw=float(data.get("max_charge_power_kw", 7.4)),
            max_discharge_power_kw=float(data.get("max_discharge_power_kw", 5.0)),
        )
        db.session.add(vehicle)
        db.session.commit()

        token = generate_token(user)
        return jsonify({"token": token, "user": user.to_dict()}), 201

    @app.post("/api/auth/login")
    @rate_limit(10, 60)
    def login():
        data = request.get_json(force=True)
        user = User.query.filter_by(username=data.get("username", "")).first()
        if not user or not user.check_password(data.get("password", "")):
            return jsonify({"error": "Identifiants invalides"}), 401
        if not user.is_active:
            return jsonify({"error": "Compte suspendu. Contactez un administrateur."}), 403
        token = generate_token(user)
        return jsonify({"token": token, "user": user.to_dict()})

    @app.get("/api/auth/me")
    @token_required
    def me():
        return jsonify(request.current_user.to_dict())

    @app.put("/api/auth/me")
    @token_required
    def update_profile():
        data = request.get_json(force=True)
        user = request.current_user
        
        if "email" in data:
            # Vérifier que l'email n'est pas déjà utilisé
            existing = User.query.filter_by(email=data["email"]).first()
            if existing and existing.id != user.id:
                return jsonify({"error": "Email déjà utilisé"}), 409
            user.email = data["email"]
        
        if "username" in data:
            existing = User.query.filter_by(username=data["username"]).first()
            if existing and existing.id != user.id:
                return jsonify({"error": "Nom d'utilisateur déjà pris"}), 409
            user.username = data["username"]
        
        if "user_type" in data:
            user.user_type = data["user_type"]
        
        db.session.commit()
        return jsonify(user.to_dict())

    @app.post("/api/auth/change-password")
    @token_required
    def change_password():
        data = request.get_json(force=True)
        user = request.current_user
        
        if not user.check_password(data.get("current_password", "")):
            return jsonify({"error": "Mot de passe actuel incorrect"}), 401
        
        if not valid_password(data.get("new_password", "")):
            return jsonify({"error": "Le nouveau mot de passe doit contenir au moins 8 caractères (lettres + chiffres)."}), 400
        
        user.set_password(data["new_password"])
        db.session.commit()
        return jsonify({"message": "Mot de passe modifié avec succès"})

    # ---------------------------------------------------------------
    # Sessions de recharge
    # ---------------------------------------------------------------
    @app.get("/api/sessions")
    @token_required
    def list_sessions():
        is_admin = (request.current_user.role or "user") == "admin"
        # L'admin voit toutes les sessions ; un utilisateur lambda ne voit que les siennes
        q = ChargingSession.query if is_admin else ChargingSession.query.filter_by(user_id=request.current_user.id)
        city = request.args.get("city")
        mode = request.args.get("mode")
        user_id = request.args.get("user_id")
        if user_id and is_admin:
            q = q.filter_by(user_id=int(user_id))
        elif not is_admin:
            q = q.filter_by(user_id=request.current_user.id)
        if city:
            q = q.join(Station).filter(Station.city == city)
        if mode:
            q = q.filter(ChargingSession.mode == mode)

        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 25)), 200)
        pagination = q.order_by(ChargingSession.start_time.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        return jsonify({
            "items": [s.to_dict() for s in pagination.items],
            "total": pagination.total,
            "page": page,
            "pages": pagination.pages,
        })

    @app.get("/api/sessions/<int:session_id>")
    @token_required
    def get_session(session_id):
        is_admin = (request.current_user.role or "user") == "admin"
        q = ChargingSession.query.filter_by(id=session_id)
        if not is_admin:
            q = q.filter_by(user_id=request.current_user.id)
        return jsonify(q.first_or_404().to_dict())

    # ---------------------------------------------------------------
    # Optimisation IA V1G / V2G
    # ---------------------------------------------------------------
    @app.post("/api/optimize/<int:session_id>")
    @token_required
    def optimize(session_id):
        is_admin = (request.current_user.role or "user") == "admin"
        q = ChargingSession.query.filter_by(id=session_id)
        if not is_admin:
            q = q.filter_by(user_id=request.current_user.id)
        s = q.first_or_404()
        vehicle = s.vehicle
        mode = request.get_json(silent=True) or {}
        v2g_enabled = bool(mode.get("v2g_enabled", vehicle.v2g_eligible))
        
        # Paramètres avancés d'optimisation
        health_weight = float(mode.get("health_weight", 0.01))
        max_cycles = int(mode.get("max_cycles", 3))

        result = optimiser_session(
            soc_start_pct=s.soc_start_pct,
            soc_target_pct=s.soc_target_pct,
            battery_capacity_kwh=vehicle.battery_capacity_kwh,
            max_charge_kw=vehicle.max_charge_power_kw,
            max_discharge_kw=vehicle.max_discharge_power_kw,
            efficiency=vehicle.battery_efficiency,
            start_dt=s.start_time,
            end_dt=s.end_time,
            v2g_enabled=v2g_enabled,
            cfg=Config,
            health_weight=health_weight,
            max_cycles_per_session=max_cycles,
        )

        import json
        from sqlalchemy import func

        # Éviter le double-comptage du solde V2G lors de ré-optimisations
        previous_gain = (
            db.session.query(func.coalesce(func.sum(OptimizationResult.v2g_gain_tnd), 0.0))
            .filter_by(session_id=s.id)
            .scalar()
        ) or 0.0

        opt = OptimizationResult(
            session_id=s.id,
            mode="v2g" if v2g_enabled else "v1g",
            baseline_cost_tnd=result["baseline_cost_tnd"],
            optimized_cost_tnd=result["optimized_cost_tnd"],
            v2g_gain_tnd=result["v2g_gain_tnd"],
            savings_tnd=result["savings_tnd"],
            savings_pct=result["savings_pct"],
            schedule_json=json.dumps(result["schedule"]),
        )
        db.session.add(opt)

        s.mode = opt.mode
        if s.user:
            # Remplace la contribution V2G de cette session plutôt que d'empiler
            new_gain = result["v2g_gain_tnd"] if v2g_enabled else 0.0
            s.user.v2g_balance_tnd = (s.user.v2g_balance_tnd or 0) - previous_gain + new_gain
        db.session.commit()

        # Enrichir la réponse avec les nouvelles métriques
        response = opt.to_dict()
        response.update({
            "battery_health_cost_tnd": result.get("battery_health_cost_tnd", 0),
            "optimized_net_cost_tnd": result.get("optimized_net_cost_tnd", result["optimized_cost_tnd"]),
            "cycles_used": result.get("cycles_used", 0),
            "final_soc_pct": result.get("final_soc_pct", s.soc_end_pct),
            "savings_tnd": result["savings_tnd"],
            "savings_pct": result["savings_pct"],
            "baseline_cost_tnd": result["baseline_cost_tnd"],
        })
        
        return jsonify(response)

    # ---------------------------------------------------------------
    # Paiements
    # ---------------------------------------------------------------
    @app.get("/api/sessions/<int:session_id>/payments")
    @token_required
    def session_payments(session_id):
        ChargingSession.query.filter_by(id=session_id, user_id=request.current_user.id).first_or_404()
        payments = Payment.query.filter_by(session_id=session_id).all()
        return jsonify([p.to_dict() for p in payments])

    # ---------------------------------------------------------------
    # Référentiels
    # ---------------------------------------------------------------
    @app.get("/api/stations")
    def stations():
        return jsonify([s.to_dict() for s in Station.query.all()])

    @app.get("/api/vehicles")
    @token_required
    def vehicles():
        user_id = request.current_user.id
        q = Vehicle.query.filter_by(user_id=user_id)
        return jsonify([v.to_dict() for v in q.all()])

    @app.post("/api/vehicles")
    @token_required
    def create_vehicle():
        data = request.get_json(force=True)
        vehicle = Vehicle(
            user_id=request.current_user.id,
            model=data.get("model", "Dacia Spring"),
            battery_capacity_kwh=float(data.get("battery_capacity_kwh", 27.4)),
            vehicle_age_years=float(data.get("vehicle_age_years", 0)),
            v2g_eligible=bool(data.get("v2g_eligible", False)),
            max_charge_power_kw=float(data.get("max_charge_power_kw", 7.4)),
            max_discharge_power_kw=float(data.get("max_discharge_power_kw", 5.0)),
            battery_efficiency=float(data.get("battery_efficiency", 0.92)),
        )
        db.session.add(vehicle)
        db.session.commit()
        return jsonify(vehicle.to_dict()), 201

    @app.put("/api/vehicles/<int:vehicle_id>")
    @token_required
    def update_vehicle(vehicle_id):
        vehicle = Vehicle.query.filter_by(id=vehicle_id, user_id=request.current_user.id).first_or_404()
        data = request.get_json(force=True)
        
        if "model" in data:
            vehicle.model = data["model"]
        if "battery_capacity_kwh" in data:
            vehicle.battery_capacity_kwh = float(data["battery_capacity_kwh"])
        if "vehicle_age_years" in data:
            vehicle.vehicle_age_years = float(data["vehicle_age_years"])
        if "v2g_eligible" in data:
            vehicle.v2g_eligible = bool(data["v2g_eligible"])
        if "max_charge_power_kw" in data:
            vehicle.max_charge_power_kw = float(data["max_charge_power_kw"])
        if "max_discharge_power_kw" in data:
            vehicle.max_discharge_power_kw = float(data["max_discharge_power_kw"])
        if "battery_efficiency" in data:
            vehicle.battery_efficiency = float(data["battery_efficiency"])
        
        db.session.commit()
        return jsonify(vehicle.to_dict())

    @app.delete("/api/vehicles/<int:vehicle_id>")
    @token_required
    def delete_vehicle(vehicle_id):
        vehicle = Vehicle.query.filter_by(id=vehicle_id, user_id=request.current_user.id).first_or_404()
        db.session.delete(vehicle)
        db.session.commit()
        return jsonify({"message": "Véhicule supprimé"}), 200

    # ---------------------------------------------------------------
    # Statistiques Dashboard
    # ---------------------------------------------------------------
    @app.get("/api/stats/dashboard")
    @token_required
    def dashboard_stats():
        from sqlalchemy import func

        # Statistiques personnelles (affichées en priorité sur le dashboard)
        user_total_sessions = ChargingSession.query.filter_by(user_id=request.current_user.id).count()
        user_total_energy = db.session.query(func.sum(ChargingSession.energy_kwh)).filter_by(user_id=request.current_user.id).scalar() or 0
        user_total_cost = db.session.query(func.sum(ChargingSession.cost_tnd)).filter_by(user_id=request.current_user.id).scalar() or 0
        user_v2g_gain = db.session.query(func.sum(OptimizationResult.v2g_gain_tnd)).join(ChargingSession).filter(ChargingSession.user_id == request.current_user.id).scalar() or 0
        user_avg_soc_start = db.session.query(func.avg(ChargingSession.soc_start_pct)).filter_by(user_id=request.current_user.id).scalar() or 0
        user_avg_soc_end = db.session.query(func.avg(ChargingSession.soc_end_pct)).filter_by(user_id=request.current_user.id).scalar() or 0

        # Agrégats globaux (réseau / dataset) pour contexte
        total_sessions = db.session.query(func.count(ChargingSession.id)).scalar() or 0
        total_energy = db.session.query(func.sum(ChargingSession.energy_kwh)).scalar() or 0
        total_cost = db.session.query(func.sum(ChargingSession.cost_tnd)).scalar() or 0
        total_v2g_gain = db.session.query(func.sum(OptimizationResult.v2g_gain_tnd)).scalar() or 0

        # L'admin n'a pas de sessions propres : on lui affiche les agrégats globaux
        is_admin = (request.current_user.role or "user") == "admin"
        if is_admin:
            user_total_sessions = total_sessions
            user_total_energy = total_energy
            user_total_cost = total_cost
            user_v2g_gain = total_v2g_gain
            user_avg_soc_start = db.session.query(func.avg(ChargingSession.soc_start_pct)).scalar() or 0
            user_avg_soc_end = db.session.query(func.avg(ChargingSession.soc_end_pct)).scalar() or 0

        hour_expr = (
            func.strftime("%H", ChargingSession.start_time)
            if "sqlite" in Config.SQLALCHEMY_DATABASE_URI
            else func.extract("hour", ChargingSession.start_time)
        )
        city_q = (
            db.session.query(Station.city, func.count(ChargingSession.id), func.sum(ChargingSession.cost_tnd))
            .join(ChargingSession, ChargingSession.station_id == Station.id)
        )
        mode_q = db.session.query(ChargingSession.mode, func.count(ChargingSession.id))
        hour_q = db.session.query(hour_expr, func.count(ChargingSession.id))
        if not is_admin:
            city_q = city_q.filter(ChargingSession.user_id == request.current_user.id)
            mode_q = mode_q.filter(ChargingSession.user_id == request.current_user.id)
            hour_q = hour_q.filter(ChargingSession.user_id == request.current_user.id)
        by_city = city_q.group_by(Station.city).all()
        by_mode = mode_q.group_by(ChargingSession.mode).all()
        by_hour = hour_q.group_by(hour_expr).all()

        return jsonify({
            # KPIs principaux = données de l'utilisateur connecté
            "total_sessions": user_total_sessions,
            "total_energy_kwh": round(user_total_energy, 1),
            "total_cost_tnd": round(user_total_cost, 2),
            "total_v2g_gain_tnd": round(user_v2g_gain, 2),
            "avg_soc_start_pct": round(user_avg_soc_start or 0, 1),
            "avg_soc_end_pct": round(user_avg_soc_end or 0, 1),
            "user_stats": {
                "total_sessions": user_total_sessions,
                "total_energy_kwh": round(user_total_energy, 1),
                "total_cost_tnd": round(user_total_cost, 2),
                "v2g_gain_tnd": round(user_v2g_gain, 2),
                "v2g_balance_tnd": round(request.current_user.v2g_balance_tnd or 0, 3),
            },
            "global_stats": {
                "total_sessions": total_sessions,
                "total_energy_kwh": round(total_energy, 1),
                "total_cost_tnd": round(total_cost, 2),
                "total_v2g_gain_tnd": round(total_v2g_gain, 2),
            },
            "by_city": [{"city": c, "sessions": n, "cost_tnd": round(cost or 0, 2)} for c, n, cost in by_city],
            "by_mode": [{"mode": m, "sessions": n} for m, n in by_mode],
            "by_hour": sorted(
                [{"hour": int(float(h)), "sessions": n} for h, n in by_hour if h is not None],
                key=lambda x: x["hour"],
            ),
            # Impact environnemental estimé (EV vs thermique + énergie V2G restituée)
            "environment": {
                "co2_avoided_kg": round(user_total_energy * CO2_KG_PER_KWH, 1),
                "co2_factor_kg_per_kwh": CO2_KG_PER_KWH,
                "trees_equivalent": round(user_total_energy * CO2_KG_PER_KWH / 21, 1),
            },
            "currency": Config.CURRENCY,
        })

    @app.get("/api/stats/analytics")
    @token_required
    def analytics_stats():
        from sqlalchemy import func
        import calendar

        # L'admin voit les statistiques globales (toutes les sessions)
        is_admin = (request.current_user.role or "user") == "admin"
        user_filter = None if is_admin else request.current_user.id

        # Statistiques mensuelles
        month_expr = (
            func.strftime("%Y-%m", ChargingSession.start_time)
            if "sqlite" in Config.SQLALCHEMY_DATABASE_URI
            else func.date_trunc("month", ChargingSession.start_time)
        )
        
        monthly_stats = (
            db.session.query(
                month_expr,
                func.count(ChargingSession.id),
                func.sum(ChargingSession.energy_kwh),
                func.sum(ChargingSession.cost_tnd),
                func.avg(ChargingSession.soc_start_pct),
                func.avg(ChargingSession.soc_end_pct)
            )
            .group_by(month_expr)
            .order_by(month_expr)
            .limit(12)
        )
        if user_filter is not None:
            monthly_stats = monthly_stats.filter_by(user_id=user_filter).all()
        else:
            monthly_stats = monthly_stats.all()

        # Top véhicules par consommation
        vehicle_stats = (
            db.session.query(
                Vehicle.model,
                func.count(ChargingSession.id),
                func.sum(ChargingSession.energy_kwh),
                func.avg(ChargingSession.charging_rate_kw)
            )
            .join(ChargingSession, Vehicle.id == ChargingSession.vehicle_id)
            .group_by(Vehicle.id, Vehicle.model)
        )
        if user_filter is not None:
            vehicle_stats = vehicle_stats.filter(ChargingSession.user_id == user_filter).all()
        else:
            vehicle_stats = vehicle_stats.all()

        # Distribution par type de tarif
        tariff_q = ChargingSession.query
        if user_filter is not None:
            tariff_q = tariff_q.filter_by(user_id=user_filter)
        tariff_distribution = []
        for session in tariff_q.all():
            hour = session.start_time.hour
            if hour >= 22 or hour < 6:
                tariff_type = "Heures creuses"
            elif 18 <= hour < 22:
                tariff_type = "Heures de pointe"
            else:
                tariff_type = "Heures pleines"
            tariff_distribution.append({
                "session_id": session.id,
                "tariff_type": tariff_type,
                "cost_tnd": session.cost_tnd,
                "energy_kwh": session.energy_kwh
            })

        return jsonify({
            "monthly_stats": [
                {
                    "month": str(m[0]),
                    "sessions": m[1],
                    "energy_kwh": round(float(m[2] or 0), 1),
                    "cost_tnd": round(float(m[3] or 0), 2),
                    "co2_kg": round(float(m[2] or 0) * CO2_KG_PER_KWH, 1),
                    "avg_soc_start": round(float(m[4] or 0), 1),
                    "avg_soc_end": round(float(m[5] or 0), 1),
                }
                for m in monthly_stats
            ],
            "vehicle_stats": [
                {
                    "model": v[0],
                    "sessions": v[1],
                    "total_energy_kwh": round(float(v[2] or 0), 1),
                    "avg_charging_rate": round(float(v[3] or 0), 2),
                }
                for v in vehicle_stats
            ],
            "tariff_distribution": tariff_distribution[-50:],  # Dernières 50 sessions
        })

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "service": "T2G API", "currency": Config.CURRENCY})

    # ---------------------------------------------------------------
    # Chatbot + Agent T2G (RAG personnalisé)
    # ---------------------------------------------------------------
    @app.post("/api/chat")
    @rate_limit(30, 60)
    @token_required
    def chat():
        data = request.get_json(force=True, silent=True) or {}
        message = data.get("message", "")
        history = data.get("history") or []
        assistant = T2GAssistant(db.session)
        return jsonify(assistant.reply(message, request.current_user, history))

    # ---------------------------------------------------------------
    # Administration (réservé au rôle 'admin')
    # ---------------------------------------------------------------
    @app.get("/api/admin/stats")
    @admin_required
    def admin_stats():
        from sqlalchemy import func

        users_count = db.session.query(func.count(User.id)).scalar() or 0
        admin_count = db.session.query(func.count(User.id)).filter_by(role="admin").scalar() or 0
        sessions_count = db.session.query(func.count(ChargingSession.id)).scalar() or 0
        stations_count = db.session.query(func.count(Station.id)).scalar() or 0
        vehicles_count = db.session.query(func.count(Vehicle.id)).scalar() or 0
        total_energy = db.session.query(func.sum(ChargingSession.energy_kwh)).scalar() or 0
        total_cost = db.session.query(func.sum(ChargingSession.cost_tnd)).scalar() or 0
        total_v2g_gain = db.session.query(func.sum(OptimizationResult.v2g_gain_tnd)).scalar() or 0

        by_city = (
            db.session.query(Station.city, func.count(ChargingSession.id),
                             func.sum(ChargingSession.energy_kwh), func.sum(ChargingSession.cost_tnd))
            .join(ChargingSession, ChargingSession.station_id == Station.id)
            .group_by(Station.city)
            .all()
        )
        by_mode = (
            db.session.query(ChargingSession.mode, func.count(ChargingSession.id))
            .group_by(ChargingSession.mode).all()
        )
        recent_users = (
            User.query.order_by(User.created_at.desc()).limit(10).all()
        )

        return jsonify({
            "users": {"total": users_count, "admins": admin_count},
            "sessions": {"total": sessions_count},
            "stations": {"total": stations_count},
            "vehicles": {"total": vehicles_count},
            "energy_kwh": round(total_energy, 1),
            "cost_tnd": round(total_cost, 2),
            "v2g_gain_tnd": round(total_v2g_gain, 2),
            "co2_avoided_kg": round(total_energy * CO2_KG_PER_KWH, 1),
            "by_city": [
                {"city": c, "sessions": n, "energy_kwh": round(e or 0, 1), "cost_tnd": round(cost or 0, 2)}
                for c, n, e, cost in by_city
            ],
            "by_mode": [{"mode": m, "sessions": n} for m, n in by_mode],
            "recent_users": [u.to_dict() for u in recent_users],
        })

    @app.get("/api/admin/users")
    @admin_required
    def admin_users():
        from sqlalchemy import func

        rows = (
            db.session.query(
                User.id, User.user_code, User.username, User.email, User.user_type,
                User.role, User.v2g_balance_tnd, User.created_at, User.is_active,
                func.count(ChargingSession.id),
                func.coalesce(func.sum(ChargingSession.energy_kwh), 0),
                func.coalesce(func.sum(ChargingSession.cost_tnd), 0),
            )
            .outerjoin(ChargingSession, ChargingSession.user_id == User.id)
            .group_by(User.id)
            .order_by(User.id)
            .all()
        )
        return jsonify([
            {
                "id": r[0], "user_code": r[1], "username": r[2], "email": r[3],
                "user_type": r[4], "role": r[5] or "user",
                "v2g_balance_tnd": round(r[6] or 0, 3),
                "created_at": r[7].isoformat() if r[7] else None,
                "is_active": bool(r[8]),
                "sessions": r[9],
                "energy_kwh": round(float(r[10] or 0), 1),
                "cost_tnd": round(float(r[11] or 0), 2),
            }
            for r in rows
        ])

    @app.post("/api/admin/users/<int:user_id>/suspend")
    @admin_required
    def admin_suspend_user(user_id):
        target = db.session.get(User, user_id)
        if not target:
            return jsonify({"error": "Utilisateur introuvable"}), 404
        if target.id == request.current_user.id:
            return jsonify({"error": "Vous ne pouvez pas suspendre votre propre compte"}), 400
        target.is_active = False
        audit(request.current_user, "suspend_user", "user", target.id, target.username)
        db.session.commit()
        return jsonify({"message": f"Compte {target.username} suspendu", "user": target.to_dict()})

    @app.post("/api/admin/users/<int:user_id>/activate")
    @admin_required
    def admin_activate_user(user_id):
        target = db.session.get(User, user_id)
        if not target:
            return jsonify({"error": "Utilisateur introuvable"}), 404
        target.is_active = True
        audit(request.current_user, "activate_user", "user", target.id, target.username)
        db.session.commit()
        return jsonify({"message": f"Compte {target.username} réactivé", "user": target.to_dict()})

    @app.post("/api/admin/users/<int:user_id>/reset-password")
    @admin_required
    def admin_reset_password(user_id):
        target = db.session.get(User, user_id)
        if not target:
            return jsonify({"error": "Utilisateur introuvable"}), 404
        data = request.get_json(force=True, silent=True) or {}
        new_pw = data.get("password", "")
        if not valid_password(new_pw):
            return jsonify({"error": "Mot de passe trop faible : 8 caractères min., lettres et chiffres."}), 400
        target.set_password(new_pw)
        audit(request.current_user, "reset_password", "user", target.id, target.username)
        db.session.commit()
        return jsonify({"message": f"Mot de passe de {target.username} réinitialisé"})

    @app.get("/api/admin/audit")
    @admin_required
    def admin_audit():
        logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(100).all()
        return jsonify([l.to_dict() for l in logs])

    @app.get("/api/admin/v2g-study")
    @admin_required
    def admin_v2g_study():
        """Étude chiffrée du potentiel V1G/V2G sur tout le dataset.

        - V1G : estimation analytique (charge aux créneaux les moins chers)
          sur TOUTES les sessions dataset.
        - V2G : optimisation LP réelle sur un échantillon (`sample` sessions,
          défaut 60) — peut prendre ~30 s.
        """
        from optimizer import optimiser_session, tarif_tnd_par_kwh
        import random

        sample_size = max(10, min(int(request.args.get("sample", 60)), 300))
        sessions = ChargingSession.query.filter(
            ChargingSession.session_code.like("SESS-%")
        ).all()
        if not sessions:
            return jsonify({"error": "Aucune session dataset"}), 404

        random.seed(42)
        v2g_sample = random.sample(sessions, min(sample_size, len(sessions)))
        v2g_ids = {s.id for s in v2g_sample}

        v1g = {"sessions": 0, "energy_kwh": 0.0, "baseline_tnd": 0.0, "optimized_tnd": 0.0}
        v2g = {"sessions": 0, "baseline_tnd": 0.0, "net_tnd": 0.0,
               "gain_tnd": 0.0, "saved_tnd": 0.0}

        for s in sessions:
            v = s.vehicle
            if not v:
                continue
            cap = v.battery_capacity_kwh
            eff = v.battery_efficiency or 0.9
            maxc = min(v.max_charge_power_kw or 7.4,
                       s.station.charger_power_kw if s.station else 22.0)
            maxd = v.max_discharge_power_kw or 0.0
            soc_start_kwh = s.soc_start_pct / 100.0 * cap
            soc_target_kwh = s.soc_target_pct / 100.0 * cap
            remaining = max(0.0, soc_target_kwh - soc_start_kwh)
            grid_needed = remaining / max(eff, 0.5)
            dt = timedelta(minutes=15)

            # Fenêtre temporelle : bornée à la fenêtre de session
            t = s.start_time
            end = s.end_time
            # Prix par pas
            steps = []
            while t < end:
                steps.append((t, tarif_tnd_par_kwh(t.hour + t.minute / 60.0, Config)))
                t += dt
            if not steps:
                continue

            # V1G : charger le besoin aux créneaux les moins chers
            need = grid_needed
            cheap = sorted(range(len(steps)), key=lambda i: steps[i][1])
            cost_v1g = 0.0
            for i in cheap:
                if need <= 0:
                    break
                e = min(maxc * 0.25, need)
                cost_v1g += e * steps[i][1]
                need -= e

            # Classique : charge ASAP au début de la fenêtre
            cost_base = 0.0
            need = grid_needed
            for _, p in steps:
                if need <= 0:
                    break
                e = min(maxc * 0.25, need)
                cost_base += e * p
                need -= e

            v1g["sessions"] += 1
            v1g["energy_kwh"] += s.energy_kwh
            v1g["baseline_tnd"] += cost_base
            v1g["optimized_tnd"] += cost_v1g

            if s.id in v2g_ids:
                r = optimiser_session(
                    s.soc_start_pct, s.soc_target_pct, cap,
                    maxc, maxd, eff, s.start_time, s.end_time,
                    bool(v.v2g_eligible), Config,
                )
                v2g["sessions"] += 1
                v2g["baseline_tnd"] += r["baseline_cost_tnd"]
                v2g["net_tnd"] += r["optimized_net_cost_tnd"]
                v2g["gain_tnd"] += r["v2g_gain_tnd"]
                v2g["saved_tnd"] += r["savings_tnd"]

        def rnd(d, nd=2):
            return {k: (round(v, nd) if isinstance(v, float) else v) for k, v in d.items()}

        v1g_savings = v1g["baseline_tnd"] - v1g["optimized_tnd"]
        v1g_pct = (v1g_savings / v1g["baseline_tnd"] * 100) if v1g["baseline_tnd"] else 0
        v2g_savings = v2g["baseline_tnd"] - v2g["net_tnd"]
        v2g_pct = (v2g_savings / v2g["baseline_tnd"] * 100) if v2g["baseline_tnd"] else 0

        return jsonify({
            "dataset_sessions": len(sessions),
            "v1g": {**rnd(v1g), "savings_tnd": round(v1g_savings, 2),
                    "savings_pct": round(v1g_pct, 1)},
            "v2g_sample": {**rnd(v2g), "savings_tnd": round(v2g_savings, 2),
                           "savings_pct": round(v2g_pct, 1),
                           "sample_size": v2g["sessions"]},
            "note": "V1G : estimation analytique sur tout le dataset. "
                    "V2G : optimisation LP réelle sur un échantillon. "
                    "NB : les sessions dataset sont courtes (1-5 h, majoritairement "
                    "en journée dans le tarif « jour » STEG) → faible flexibilité "
                    "tarifaire. Le potentiel V2G réel apparaît sur les longues "
                    "durées de stationnement (sessions démo de nuit).",
        })

    # ---------------------------------------------------------------
    # Réservations de sessions
    # ---------------------------------------------------------------
    @app.get("/api/bookings")
    @token_required
    def list_bookings():
        q = Booking.query
        if (request.current_user.role or "user") != "admin":
            q = q.filter_by(user_id=request.current_user.id)
        status = request.args.get("status")
        if status:
            q = q.filter_by(status=status)
        items = q.order_by(Booking.start_time).all()
        return jsonify([b.to_dict() for b in items])

    @app.post("/api/bookings")
    @token_required
    def create_booking():
        data = request.get_json(force=True, silent=True) or {}
        try:
            start = _parse_booking_dt(data.get("start_time"))
            end = _parse_booking_dt(data.get("end_time"))
        except Exception:
            return jsonify({"error": "Format de date invalide (attendu YYYY-MM-DD HH:MM)"}), 400
        if not start or not end or end <= start:
            return jsonify({"error": "Heures invalides : la fin doit être après le début"}), 400

        station = db.session.get(Station, int(data.get("station_id") or 0))
        vehicle = db.session.get(Vehicle, int(data.get("vehicle_id") or 0))
        if not station or not vehicle or vehicle.user_id != request.current_user.id:
            return jsonify({"error": "Station ou véhicule invalide"}), 400

        booking = Booking(
            user_id=request.current_user.id,
            vehicle_id=vehicle.id,
            station_id=station.id,
            start_time=start,
            end_time=end,
            notes=(data.get("notes") or "")[:255] or None,
        )
        db.session.add(booking)
        db.session.commit()
        return jsonify(booking.to_dict()), 201

    @app.put("/api/bookings/<int:booking_id>")
    @token_required
    def update_booking(booking_id):
        is_admin = (request.current_user.role or "user") == "admin"
        q = Booking.query.filter_by(id=booking_id)
        if not is_admin:
            q = q.filter_by(user_id=request.current_user.id)
        booking = q.first_or_404()
        data = request.get_json(force=True, silent=True) or {}
        if "status" in data:
            if data["status"] not in ("confirmée", "annulée", "terminée"):
                return jsonify({"error": "Statut invalide"}), 400
            booking.status = data["status"]
        if "notes" in data:
            booking.notes = (data["notes"] or "")[:255] or None
        db.session.commit()
        return jsonify(booking.to_dict())

    @app.delete("/api/bookings/<int:booking_id>")
    @token_required
    def delete_booking(booking_id):
        is_admin = (request.current_user.role or "user") == "admin"
        q = Booking.query.filter_by(id=booking_id)
        if not is_admin:
            q = q.filter_by(user_id=request.current_user.id)
        booking = q.first_or_404()
        db.session.delete(booking)
        db.session.commit()
        return jsonify({"message": "Réservation supprimée"})

    # ---------------------------------------------------------------
    # Export CSV des sessions
    # ---------------------------------------------------------------
    @app.get("/api/export/sessions.csv")
    @token_required
    def export_sessions_csv():
        is_admin = (request.current_user.role or "user") == "admin"
        q = ChargingSession.query
        if not is_admin:
            q = q.filter_by(user_id=request.current_user.id)
        sessions = q.order_by(ChargingSession.start_time).all()

        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "session_code", "user", "vehicule", "ville", "station",
            "debut", "fin", "duree_h", "energie_kwh", "cout_tnd",
            "soc_debut", "soc_fin", "mode",
        ])
        for s in sessions:
            writer.writerow([
                s.session_code,
                s.user.username if s.user else "",
                s.vehicle.model if s.vehicle else "",
                s.station.city if s.station else "",
                s.station.station_code if s.station else "",
                s.start_time.strftime("%Y-%m-%d %H:%M") if s.start_time else "",
                s.end_time.strftime("%Y-%m-%d %H:%M") if s.end_time else "",
                s.duration_h, s.energy_kwh, s.cost_tnd,
                s.soc_start_pct, s.soc_end_pct, s.mode,
            ])
        out.seek(0)
        filename = "t2g_sessions_admin.csv" if is_admin else f"t2g_sessions_{request.current_user.username}.csv"
        return Response(
            out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # ---------------------------------------------------------------
    # Prédictions ML et IA
    # ---------------------------------------------------------------
    @app.post("/api/predict/cost")
    @token_required
    def predict_charging_cost():
        """Prédit le coût de recharge pour une session donnée"""
        data = request.get_json(force=True)
        
        try:
            # Gérer différents formats de date
            start_time_str = data.get('start_time', datetime.now().isoformat())
            if isinstance(start_time_str, str):
                if 'T' in start_time_str:
                    start_time = datetime.fromisoformat(start_time_str)
                else:
                    start_time = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
            else:
                start_time = start_time_str
            
            prediction = cost_predictor.predict_cost(
                battery_capacity=float(data.get('battery_capacity', 50)),
                soc_start=int(data.get('soc_start', 20)),
                soc_target=int(data.get('soc_target', 90)),
                charging_power=float(data.get('charging_power', 7.4)),
                start_time=start_time,
                temperature=float(data.get('temperature', 25)),
                vehicle_age=float(data.get('vehicle_age', 2)),
                distance=float(data.get('distance', 100))
            )
            return jsonify(prediction)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/predict/optimal-time")
    @token_required
    def predict_optimal_charging_time():
        """Prédit les meilleurs moments pour charger"""
        data = request.get_json(force=True)
        
        try:
            optimal_times = time_predictor.predict_optimal_time(
                current_soc=int(data.get('current_soc', 20)),
                target_soc=int(data.get('target_soc', 90)),
                battery_capacity=float(data.get('battery_capacity', 50)),
                max_power=float(data.get('max_power', 7.4)),
                flexibility_hours=int(data.get('flexibility_hours', 24))
            )
            return jsonify({"optimal_times": optimal_times})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.get("/api/advice/personalized")
    @token_required
    def get_personalized_advice():
        """Génère des conseils personnalisés basés sur l'historique utilisateur"""
        try:
            user_sessions = ChargingSession.query.filter_by(user_id=request.current_user.id).all()
            session_history = [s.to_dict() for s in user_sessions]

            user_data = request.current_user.to_dict()
            vehicles = Vehicle.query.filter_by(user_id=request.current_user.id).all()
            if vehicles:
                user_data["vehicle_age_years"] = max(v.vehicle_age_years or 0 for v in vehicles)
                user_data["v2g_eligible"] = any(bool(v.v2g_eligible) for v in vehicles)
            else:
                user_data["vehicle_age_years"] = 0
                user_data["v2g_eligible"] = False

            advice = rag_system.get_personalized_advice(user_data, session_history)

            return jsonify({
                "advice": advice,
                "user_profile": {
                    "total_sessions": len(session_history),
                    "avg_cost": round(float(np.mean([s.get("cost_tnd") or 0 for s in session_history])), 2) if session_history else 0,
                    "v2g_usage": sum(1 for s in session_history if s.get("mode") == "v2g"),
                    "v2g_eligible": user_data["v2g_eligible"],
                    "vehicle_age_years": user_data["vehicle_age_years"],
                    "ml_trained": bool(getattr(cost_predictor, "is_trained", False)),
                }
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/predict/station-demand")
    @token_required
    def predict_station_demand():
        """Prédit la demande pour une station donnée"""
        data = request.get_json(force=True)
        
        try:
            station_id = int(data.get('station_id'))
            hour = int(data.get('hour', datetime.now().hour))
            day_of_week = int(data.get('day_of_week', datetime.now().weekday()))
            
            # Récupérer les données historiques
            historical_data = [s.to_dict() for s in ChargingSession.query.filter_by(station_id=station_id).all()]
            
            prediction = demand_predictor.predict_station_demand(
                station_id, hour, day_of_week, historical_data
            )
            
            return jsonify(prediction)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/ml/train")
    @token_required
    def train_ml_model():
        """Entraîne le modèle ML sur les données historiques"""
        try:
            # Récupérer toutes les sessions pour l'entraînement
            all_sessions = ChargingSession.query.all()
            sessions_data = []
            
            for session in all_sessions:
                session_dict = session.to_dict()
                # Ajouter les informations véhicule
                if session.vehicle:
                    session_dict['battery_capacity_kwh'] = session.vehicle.battery_capacity_kwh
                    session_dict['vehicle_age_years'] = session.vehicle.vehicle_age_years
                sessions_data.append(session_dict)
            
            # Entraîner le modèle
            success = cost_predictor.train(sessions_data)
            
            if success:
                return jsonify({
                    "message": "Modèle entraîné avec succès",
                    "training_samples": len(sessions_data),
                    "model_status": "trained"
                })
            else:
                return jsonify({
                    "message": "Pas assez de données pour l'entraînement",
                    "training_samples": len(sessions_data),
                    "model_status": "using_fallback"
                }), 400
                
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
