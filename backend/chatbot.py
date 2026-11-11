"""
Chatbot + Agent T2G
===================
Assistant conversationnel personnalisé (RAG) capable de :
  - répondre aux questions (tarifs, V2G, batterie, sessions, conseils)
  - agir en "agent" : exécuter de vraies actions (prédire un coût,
    trouver le meilleur moment, optimiser une session réelle)

Le contexte injecté au RAG est personnel : profil utilisateur, véhicules,
historique de sessions, soldes V2G.
"""

import json
import re
from datetime import datetime

from config import Config
from models import ChargingSession, Vehicle, OptimizationResult, User
from optimizer import optimiser_session
from prediction_engine import cost_predictor, time_predictor, rag_system


TARIFS = {
    "heures_creuses": {"label": "Heures creuses (nuit STEG)", "hours": "22h–6h", "tnd_kwh": 0.141},
    "heures_pleines": {"label": "Heures pleines (jour STEG)", "hours": "6h–18h", "tnd_kwh": 0.168},
    "pointe": {"label": "Heures de pointe (soir STEG)", "hours": "18h–22h", "tnd_kwh": 0.452},
}
V2G_SELL_RATIO = 0.85  # aligné sur Config.V2G_SELL_RATIO

# Facteur d'émission moyen du réseau tunisien (charbon/gnc) — ordre de grandeur
# pédagogique pour estimer le CO2 évité par la recharge EV vs thermique.
CO2_KG_PER_KWH = 0.45


def _norm(text: str) -> str:
    return (text or "").lower()


class T2GAssistant:
    """Orchestre intents + tools pour le chatbot et l'agent."""

    def __init__(self, db_session):
        self.db = db_session

    # ------------------------------------------------------------------
    # Contexte utilisateur (RAG personnalisé)
    # ------------------------------------------------------------------
    def _context(self, user: User) -> dict:
        vehicles = Vehicle.query.filter_by(user_id=user.id).all()
        sessions = ChargingSession.query.filter_by(user_id=user.id).all()
        costs = [s.cost_tnd or 0 for s in sessions]
        energies = [s.energy_kwh or 0 for s in sessions]
        v2g_count = sum(1 for s in sessions if s.mode == "v2g")
        v1g_count = sum(1 for s in sessions if s.mode == "v1g")
        v2g_eligible = any(v.v2g_eligible for v in vehicles)

        # énergie restituée au réseau (V2G) via les plannings optimisés
        v2g_energy = 0.0
        schedules = (
            self.db.query(OptimizationResult.schedule_json)
            .join(ChargingSession, OptimizationResult.session_id == ChargingSession.id)
            .filter(ChargingSession.user_id == user.id, OptimizationResult.mode == "v2g")
            .all()
        )
        for (sched_json,) in schedules:
            try:
                sched = json.loads(sched_json) if sched_json else []
                v2g_energy += sum(float(p.get("discharge_kw") or 0) for p in sched)
            except Exception:
                continue

        return {
            "user": user.to_dict(),
            "vehicles": [v.to_dict() for v in vehicles],
            "vehicle_count": len(vehicles),
            "v2g_eligible": v2g_eligible,
            "total_sessions": len(sessions),
            "total_energy_kwh": round(sum(energies), 1),
            "total_cost_tnd": round(sum(costs), 2),
            "avg_cost_tnd": round(sum(costs) / len(costs), 2) if costs else 0,
            "v1g_count": v1g_count,
            "v2g_count": v2g_count,
            "v2g_balance_tnd": round(user.v2g_balance_tnd or 0, 3),
            "v2g_energy_returned_kwh": round(v2g_energy, 1),
            "co2_avoided_kg": round((sum(energies) + v2g_energy) * CO2_KG_PER_KWH, 1),
            "primary_vehicle": vehicles[0].to_dict() if vehicles else None,
        }

    # ------------------------------------------------------------------
    # Intents
    # ------------------------------------------------------------------
    def _detect_intent(self, msg: str) -> str:
        m = _norm(msg)
        if re.search(r"\b(salut|bonjour|bonsoir|hello|coucou|ça va|ca va)\b", m):
            return "greeting"
        if re.search(r"(optimis|optimi|programme ma charge|décale|decale|reporte)", m):
            return "optimize_action"
        if re.search(r"(meilleur moment|quand charger|horaire optimal|meilleurs horaires|quand dois-je)", m):
            return "optimal_time"
        if re.search(r"(prédit|estime|combien ça coûte|combien ca coute|coût de (ma |une )?(recharge|charge)|prix d'une recharge)", m):
            return "predict_cost"
        if re.search(r"(tarif|prix|kwh|heure creuse|heure pleine|pointe|combien coûte le)", m):
            return "tariffs"
        if re.search(r"(v2g|revendre|revente|injecter|gain|revenu|solde)", m):
            return "v2g"
        if re.search(r"(conseil|recommande|recommandation|advice|astuce|comment.*économiser|comment.*economiser|réduire|reduire)", m):
            return "advice"
        if re.search(r"(historique|statistique|stats|combien.*session|total|bilan|résumé|resume|combien ai-je)", m):
            return "stats"
        if re.search(r"(batterie|soc|autonomie|charge.*%|niveau)", m):
            return "battery"
        if re.search(r"(co2|co₂|carbone|environnement|écolog|ecolog|émission|emission)", m):
            return "environment"
        if re.search(r"(qui es-tu|que peux-tu|help|aide|fonctionnalité|fonctionnalite)", m):
            return "help"
        return "fallback"

    # ------------------------------------------------------------------
    # Réponse principale
    # ------------------------------------------------------------------
    def reply(self, message: str, user: User, history: list = None) -> dict:
        msg = (message or "").strip()
        if not msg:
            return {"reply": "Posez-moi une question sur vos recharges, les tarifs ou le V2G !",
                    "actions": [], "suggestions": self._suggestions()}

        ctx = self._context(user)
        intent = self._detect_intent(msg)
        actions = []

        if intent == "greeting":
            reply = (
                f"Bonjour {user.username} 👋 Je suis l'assistant T2G. "
                "Je connais votre profil et vos {ctx['total_sessions']} sessions : "
                f"{ctx['total_energy_kwh']} kWh chargés, {ctx['total_cost_tnd']} TND dépensés"
                f"{' et ' + str(ctx['v2g_balance_tnd']) + ' TND de gains V2G' if ctx['v2g_balance_tnd'] else ''}. "
                "Demandez-moi vos conseils, vos coûts ou je peux optimiser une session pour vous."
            )

        elif intent == "tariffs":
            hc, hp, p = TARIFS["heures_creuses"], TARIFS["heures_pleines"], TARIFS["pointe"]
            reply = (
                "**Tarifs T2G (TND/kWh)** :\n"
                f"• {hc['label']} ({hc['hours']}) : **{hc['tnd_kwh']:.3f} TND/kWh**\n"
                f"• {hp['label']} ({hp['hours']}) : **{hp['tnd_kwh']:.3f} TND/kWh**\n"
                f"• {p['label']} ({p['hours']}) : **{p['tnd_kwh']:.3f} TND/kWh**\n\n"
                "Astuce : brancher après 22h ou avant 6h divise votre coût par ~2,5. "
                "En V2G, l'énergie revendue en pointe rapporte "
                f"{p['tnd_kwh'] * V2G_SELL_RATIO:.3f} TND/kWh."
            )

        elif intent == "v2g":
            if ctx["v2g_eligible"]:
                reply = (
                    "**V2G (Vehicle-to-Grid)** : votre véhicule est compatible. "
                    f"Solde gains : **{ctx['v2g_balance_tnd']} TND** · "
                    f"{ctx['v2g_count']} session(s) V2G · "
                    f"{ctx['v2g_energy_returned_kwh']} kWh restitués au réseau.\n\n"
                    "Le principe : décharger la batterie vers le réseau pendant les heures "
                    "de pointe (18h–22h, rachat ~0,38 TND/kWh), puis recharger en heures "
                    "creuses (0,141 TND/kWh). Demandez « optimise ma dernière session » "
                    "pour que je calcule le planning gagnant."
                )
            else:
                reply = (
                    "Votre véhicule n'est pas encore compatible V2G. "
                    "Le mode **Smart V1G** reste disponible : il décale votre charge vers "
                    "les heures creuses sans revendre d'énergie. "
                    "Vous pouvez marquer un véhicule comme V2G dans « Mes véhicules »."
                )

        elif intent == "battery":
            veh = ctx["primary_vehicle"]
            cap = veh["battery_capacity_kwh"] if veh else 50
            model_label = f" ({veh['model']})" if veh else ""
            reply = (
                f"**Votre batterie** : {cap} kWh{model_label}.\n\n"
                "Bonnes pratiques :\n"
                "• Maintenez le SoC entre **20 % et 80 %** au quotidien\n"
                "• Évitez les décharges sous 15 % et les charges 100 % sauf long trajet\n"
                "• Charges fréquentes plutôt que cycles complets = meilleure longévité\n"
                "• Évitez de charger sous 0 °C ou au-dessus de 35 °C si possible"
            )

        elif intent == "stats":
            reply = (
                "**Votre bilan T2G** :\n"
                f"• Sessions : **{ctx['total_sessions']}** "
                f"({ctx['v1g_count']} V1G · {ctx['v2g_count']} V2G · reste classique)\n"
                f"• Énergie chargée : **{ctx['total_energy_kwh']} kWh**\n"
                f"• Coût total : **{ctx['total_cost_tnd']} TND** "
                f"(moyenne {ctx['avg_cost_tnd']} TND/session)\n"
                f"• Gains V2G : **{ctx['v2g_balance_tnd']} TND**\n"
                f"• CO₂ évité estimé : **{ctx['co2_avoided_kg']} kg**"
            )

        elif intent == "environment":
            reply = (
                "**Impact environnemental** 🌱\n"
                f"• Énergie chargée : {ctx['total_energy_kwh']} kWh\n"
                f"• Énergie restituée au réseau (V2G) : {ctx['v2g_energy_returned_kwh']} kWh\n"
                f"• CO₂ évité estimé : **{ctx['co2_avoided_kg']} kg** "
                f"(facteur {CO2_KG_PER_KWH} kg/kWh, réseau tunisien)\n"
                f"• Équivalent : ~{ctx['co2_avoided_kg'] / 21:.1f} arbres plantés/an\n\n"
                "Chaque kWh V2G vendu en pointe évite aussi le recours à des "
                "centrales thermiques polluantes aux heures de pointe."
            )

        elif intent == "advice":
            user_data = dict(ctx["user"])
            user_data["v2g_eligible"] = ctx["v2g_eligible"]
            user_data["vehicle_age_years"] = (ctx["primary_vehicle"] or {}).get("vehicle_age_years", 0)
            hist = [s.to_dict() for s in ChargingSession.query.filter_by(user_id=user.id).limit(200).all()]
            advices = rag_system.get_personalized_advice(user_data, hist)
            top = advices[:3]
            reply = "**Conseils personnalisés** (RAG) :\n" + "\n\n".join(
                f"• **{a['title']}** — {a['advice']}\n  → {a.get('action', '')}"
                for a in top
            )

        elif intent == "optimal_time":
            veh = ctx["primary_vehicle"] or {}
            cap = float(veh.get("battery_capacity_kwh") or 50)
            power = float(veh.get("max_charge_power_kw") or 7.4)
            times = time_predictor.predict_optimal_time(
                current_soc=20, target_soc=90, battery_capacity=cap,
                max_power=power, flexibility_hours=24,
            )
            actions.append({"type": "optimal_times", "data": times[:3]})
            reply = (
                "Voici les **3 meilleurs créneaux** pour charger vos "
                f"{cap} kWh (flexibilité 24 h) :\n" + "\n".join(
                    f"• {t.get('start_time')} — {t.get('tariff_type')} · "
                    f"~{t.get('estimated_cost_tnd')} TND"
                    for t in times[:3]
                )
            )

        elif intent == "predict_cost":
            veh = ctx["primary_vehicle"] or {}
            cap = float(veh.get("battery_capacity_kwh") or 50)
            power = float(veh.get("max_charge_power_kw") or 7.4)
            pred = cost_predictor.predict_cost(
                battery_capacity=cap, soc_start=20, soc_target=90,
                charging_power=power, start_time=datetime.now(),
            )
            actions.append({"type": "cost_prediction", "data": pred})
            reply = (
                f"**Prédiction de coût** (véhicule {cap} kWh, SoC 20→90 %, maintenant) :\n"
                f"• Coût estimé : **{pred.get('predicted_cost_tnd')} TND**\n"
                f"• Méthode : {pred.get('method', 'tariff_based')}\n"
                + (f"• Durée estimée : {pred.get('estimated_duration_h')} h\n" if pred.get('estimated_duration_h') else "")
                + "\nDites-moi une autre heure ou SoC si vous voulez affiner."
            )

        elif intent == "optimize_action":
            session = (
                ChargingSession.query.filter_by(user_id=user.id)
                .order_by(ChargingSession.start_time.desc()).first()
            )
            if not session:
                reply = "Vous n'avez aucune session à optimiser pour le moment."
            else:
                result = self._run_optimization(session)
                actions.append({"type": "optimization", "data": result, "session_id": session.id})
                reply = (
                    f"**Agent : optimisation effectuée** sur la session {session.session_code} "
                    f"({session.start_time.strftime('%d/%m/%Y %H:%M')}).\n"
                    f"• Coût classique : {result['baseline_cost_tnd']} TND\n"
                    f"• Coût optimisé : {result['optimized_cost_tnd']} TND\n"
                    f"• **Économie : {result['savings_tnd']} TND ({result['savings_pct']} %)**\n"
                    + (f"• Gain V2G : {result['v2g_gain_tnd']} TND\n" if result.get("v2g_gain_tnd") else "")
                    + "Le planning détaillé est affiché ci-dessous."
                )

        elif intent == "help":
            reply = (
                "Je peux vous aider sur :\n"
                "• **Tarifs** — « quels sont les tarifs ? »\n"
                "• **V2G** — « explique le V2G » / « mon solde V2G »\n"
                "• **Coûts** — « combien coûte une charge de 50 kWh ? »\n"
                "• **Conseils** — « donne-moi des conseils »\n"
                "• **Stats** — « mon bilan » / « mon historique »\n"
                "• **Environnement** — « mon impact CO₂ »\n"
                "• **Actions agent** — « optimise ma dernière session », "
                "« meilleurs moments pour charger »"
            )

        else:
            reply = (
                f"Je n'ai pas bien compris. Je suis spécialiste de votre profil T2G "
                f"({ctx['total_sessions']} sessions connues). Essayez :\n"
                "• « conseils » · « tarifs » · « V2G » · « mon bilan » · "
                "« impact CO₂ » · « optimise ma dernière session »"
            )

        return {
            "reply": reply,
            "intent": intent,
            "actions": actions,
            "suggestions": self._suggestions(),
        }

    # ------------------------------------------------------------------
    # Outils agent
    # ------------------------------------------------------------------
    def _run_optimization(self, session: ChargingSession) -> dict:
        """Exécute l'optimisation V1G/V2G sur une session et la persiste."""
        vehicle = session.vehicle
        v2g_enabled = bool(vehicle and vehicle.v2g_eligible)
        result = optimiser_session(
            soc_start_pct=session.soc_start_pct,
            soc_target_pct=session.soc_target_pct,
            battery_capacity_kwh=vehicle.battery_capacity_kwh if vehicle else 50,
            max_charge_kw=vehicle.max_charge_power_kw if vehicle else 7.4,
            max_discharge_kw=vehicle.max_discharge_power_kw if vehicle else 0.0,
            efficiency=vehicle.battery_efficiency if vehicle else 0.92,
            start_dt=session.start_time,
            end_dt=session.end_time,
            v2g_enabled=v2g_enabled,
            cfg=Config,
        )
        import json as _json
        opt = OptimizationResult(
            session_id=session.id,
            mode="v2g" if v2g_enabled else "v1g",
            baseline_cost_tnd=result["baseline_cost_tnd"],
            optimized_cost_tnd=result["optimized_cost_tnd"],
            v2g_gain_tnd=result["v2g_gain_tnd"],
            savings_tnd=result["savings_tnd"],
            savings_pct=result["savings_pct"],
            schedule_json=_json.dumps(result["schedule"]),
        )
        self.db.add(opt)
        session.mode = opt.mode
        if session.user:
            previous_gain = (
                self.db.query(OptimizationResult.v2g_gain_tnd)
                .filter(OptimizationResult.session_id == session.id,
                        OptimizationResult.id != opt.id)
                .all()
            )
            prev = sum(g or 0 for (g,) in previous_gain)
            new_gain = result["v2g_gain_tnd"] if v2g_enabled else 0.0
            session.user.v2g_balance_tnd = (session.user.v2g_balance_tnd or 0) - prev + new_gain
        self.db.commit()
        return {
            "baseline_cost_tnd": result["baseline_cost_tnd"],
            "optimized_cost_tnd": result["optimized_cost_tnd"],
            "optimized_net_cost_tnd": result.get("optimized_net_cost_tnd", result["optimized_cost_tnd"]),
            "v2g_gain_tnd": result["v2g_gain_tnd"],
            "savings_tnd": result["savings_tnd"],
            "savings_pct": result["savings_pct"],
            "schedule": result["schedule"],
        }

    def _suggestions(self) -> list:
        return [
            "Conseils personnalisés",
            "Tarifs TND/kWh",
            "Explique le V2G",
            "Mon bilan",
            "Impact CO₂",
            "Optimise ma dernière session",
        ]
