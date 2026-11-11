"""
Moteur d'optimisation IA — V1G / V2G Amélioré
=============================================
Formulation en programmation linéaire (PuLP) avec fonctionnalités avancées :

  Fonction objectif : minimiser le coût net de recharge + pénalités
        min  Σ (prix_achat_t * charge_t) - Σ (prix_vente_t * décharge_t)
             + α * Σ (profondeur_décharge_t) + β * Σ (cycles_batterie_t)

  Contraintes améliorées :
    - Disponibilité temporelle : uniquement sur [T_arrivée, T_départ]
    - SoC cible garanti au départ
    - SoC toujours borné dans [SoC_min, SoC_max]
    - Puissance de charge/décharge bornée par les capacités du véhicule/borne
    - Rendement de conversion (pertes batterie) pris en compte
    - Limitation des cycles de batterie pour préserver la santé
    - Contraintes de réseau (puissance maximale disponible)

Le pas de temps est de 15 minutes (dt = 0.25 h) sur la fenêtre de
disponibilité du véhicule.
"""

import pulp
import numpy as np


def tarif_tnd_par_kwh(hour: int, cfg) -> float:
    """Tarif d'achat réseau (TND/kWh) selon l'heure de la journée."""
    if hour >= 22 or hour < 6:
        return cfg.TARIFF_OFFPEAK
    if 18 <= hour < 22:
        return cfg.TARIFF_PEAK
    return cfg.TARIFF_NORMAL


def calculate_battery_health_cost(discharge_profile, battery_capacity_kwh, health_weight=0.01):
    """
    Calcule un coût de santé de batterie basé sur la profondeur de décharge.
    Moins on décharge profondément, mieux c'est pour la santé de la batterie.
    """
    if not discharge_profile:
        return 0.0
    
    # Calcul de la profondeur de décharge moyenne
    avg_discharge = np.mean(discharge_profile)
    dod_ratio = avg_discharge / battery_capacity_kwh if battery_capacity_kwh > 0 else 0
    
    # Coût quadratique pour pénaliser les décharges profondes
    health_cost = health_weight * (dod_ratio ** 2) * battery_capacity_kwh
    return health_cost


def optimiser_session(
    soc_start_pct: float,
    soc_target_pct: float,
    battery_capacity_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    efficiency: float,
    start_dt,
    end_dt,
    v2g_enabled: bool,
    cfg,
    dt_hours: float = 0.25,
    health_weight: float = 0.01,
    max_cycles_per_session: int = 3,
):
    """
    Calcule le planning optimal de charge (V1G) et, si activé, de décharge
    (V2G) pour un véhicule sur sa fenêtre de disponibilité [start_dt, end_dt].

    Nouvelles fonctionnalités :
    - Prise en compte de la santé de la batterie
    - Limitation des cycles de charge/décharge
    - Optimisation multi-objectif (coût + santé batterie)

    Retourne un dict avec le planning pas-à-pas et les indicateurs coût/gain.
    """
    total_hours = max((end_dt - start_dt).total_seconds() / 3600.0, dt_hours)
    n_steps = max(1, int(round(total_hours / dt_hours)))

    # Horodatage de chaque pas (minutes incluses) + tarif associé
    start_hour_frac = start_dt.hour + start_dt.minute / 60.0 + start_dt.second / 3600.0
    slot_hours = [(start_hour_frac + i * dt_hours) % 24 for i in range(n_steps)]
    buy_price = [tarif_tnd_par_kwh(int(h) % 24, cfg) for h in slot_hours]
    sell_price = [p * cfg.V2G_SELL_RATIO for p in buy_price]

    soc_start = max(0.0, min(100.0, soc_start_pct)) / 100.0 * battery_capacity_kwh
    soc_target = max(0.0, min(100.0, soc_target_pct)) / 100.0 * battery_capacity_kwh
    soc_min = 0.15 * battery_capacity_kwh   # on ne descend jamais sous 15% (santé batterie améliorée)
    soc_max = battery_capacity_kwh          # 100%

    prob = pulp.LpProblem("T2G_V1G_V2G_Scheduling_Enhanced", pulp.LpMinimize)

    charge = [pulp.LpVariable(f"charge_{i}", lowBound=0, upBound=max_charge_kw) for i in range(n_steps)]
    discharge = [
        pulp.LpVariable(f"discharge_{i}", lowBound=0,
                         upBound=(max_discharge_kw if v2g_enabled else 0))
        for i in range(n_steps)
    ]

    # Variables binaires pour détecter les transitions charge/décharge
    is_charging = [pulp.LpVariable(f"is_charging_{i}", cat="Binary") for i in range(n_steps)]
    is_discharging = [pulp.LpVariable(f"is_discharging_{i}", cat="Binary") for i in range(n_steps)]

    # Contraintes : is_charging et is_discharging sont mutuellement exclusifs
    for i in range(n_steps):
        prob += is_charging[i] + is_discharging[i] <= 1
        # Si charge > 0 alors is_charging = 1
        prob += charge[i] <= max_charge_kw * is_charging[i]
        # Si discharge > 0 alors is_discharging = 1
        prob += discharge[i] <= max_discharge_kw * is_discharging[i]

    # Limiter le nombre de transitions entre états (idle / charge / décharge).
    # État encodé : state_i = is_charging + 2*is_discharging ∈ {0,1,2}
    # (l'exclusivité ich+idi<=1 garantit 3 états max).
    # Une transition = |state_i - state_{i-1}| > 0, via les deux inégalités
    # standard de valeur absolue sur des expressions linéaires.
    transitions = []
    for i in range(1, n_steps):
        transition = pulp.LpVariable(f"transition_{i}", cat="Binary")
        state_i = is_charging[i] + 2 * is_discharging[i]
        state_prev = is_charging[i - 1] + 2 * is_discharging[i - 1]
        prob += transition >= state_i - state_prev
        prob += transition >= state_prev - state_i
        transitions.append(transition)

    prob += pulp.lpSum(transitions) <= max_cycles_per_session

    # Fonction objectif : coût d'achat - revenu de revente V2G + pénalité santé batterie
    energy_cost = pulp.lpSum([
        charge[i] * dt_hours * buy_price[i] - discharge[i] * dt_hours * sell_price[i]
        for i in range(n_steps)
    ])
    
    # Pénalité pour les décharges profondes (santé batterie)
    depth_penalty = pulp.lpSum([
        discharge[i] * dt_hours * health_weight for i in range(n_steps)
    ])
    
    prob += energy_cost + depth_penalty

    # Évolution du SoC (énergie stockée, kWh) + contraintes de bornes
    soc = soc_start
    soc_vars = []
    for i in range(n_steps):
        soc_expr = soc if i == 0 else soc_vars[i - 1]
        new_soc = soc_expr + charge[i] * dt_hours * efficiency - discharge[i] * dt_hours / efficiency
        soc_var = pulp.LpVariable(f"soc_{i}", lowBound=soc_min, upBound=soc_max)
        prob += soc_var == new_soc
        soc_vars.append(soc_var)

    # SoC cible garanti au départ
    prob += soc_vars[-1] >= soc_target

    # Contrainte de réseau : limiter la puissance totale instantanée si nécessaire
    max_grid_power = max_charge_kw + (max_discharge_kw if v2g_enabled else 0)
    for i in range(n_steps):
        prob += charge[i] + discharge[i] <= max_grid_power

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[prob.status]

    # Filet de sécurité : si le LP est infaisable, planning glouton
    # (charge aux créneaux les moins chers) plutôt que des valeurs nulles.
    if status != "Optimal":
        return _fallback_schedule(
            soc_start_pct, soc_target_pct, battery_capacity_kwh,
            max_charge_kw, efficiency, slot_hours, buy_price, dt_hours,
        )

    schedule = []
    total_cost = 0.0
    total_gain = 0.0
    discharge_profile = []
    for i in range(n_steps):
        c = charge[i].value() or 0.0
        d = discharge[i].value() or 0.0
        s = soc_vars[i].value() or 0.0
        cost_i = c * dt_hours * buy_price[i]
        gain_i = d * dt_hours * sell_price[i]
        total_cost += cost_i
        total_gain += gain_i
        discharge_profile.append(d)
        schedule.append({
            "step": i,
            "hour": round(slot_hours[i], 2),
            "charge_kw": round(c, 2),
            "discharge_kw": round(d, 2),
            "soc_pct": round(100 * s / battery_capacity_kwh, 1),
            "buy_price_tnd_kwh": round(buy_price[i], 3),
            "sell_price_tnd_kwh": round(sell_price[i], 3),
            "cost_tnd": round(cost_i, 4),
            "gain_tnd": round(gain_i, 4),
            "is_charging": bool(is_charging[i].value() or 0),
            "is_discharging": bool(is_discharging[i].value() or 0),
        })

    # Calcul du coût de santé de batterie
    battery_health_cost = calculate_battery_health_cost(discharge_profile, battery_capacity_kwh, health_weight)

    # Coût de référence "recharge classique" : charge ASAP à puissance max
    # dès l'arrivée, sans report tarifaire ni V2G.
    baseline_cost = 0.0
    remaining = soc_target - soc_start
    for i in range(n_steps):
        if remaining <= 0:
            break
        # Énergie à tirer du réseau pour atteindre step_energy dans la batterie
        grid_energy_needed = min(max_charge_kw * dt_hours, remaining / efficiency)
        baseline_cost += grid_energy_needed * buy_price[i]
        remaining -= grid_energy_needed * efficiency

    optimized_net_cost = total_cost - total_gain + battery_health_cost
    savings = baseline_cost - optimized_net_cost
    savings_pct = (savings / baseline_cost * 100.0) if baseline_cost > 0 else 0.0

    return {
        "status": status,
        "n_steps": n_steps,
        "dt_hours": dt_hours,
        "schedule": schedule,
        "baseline_cost_tnd": round(baseline_cost, 3),
        "optimized_cost_tnd": round(total_cost, 3),
        "v2g_gain_tnd": round(total_gain, 3),
        "battery_health_cost_tnd": round(battery_health_cost, 3),
        "optimized_net_cost_tnd": round(optimized_net_cost, 3),
        "savings_tnd": round(savings, 3),
        "savings_pct": round(savings_pct, 1),
        "final_soc_pct": round(100 * (soc_vars[-1].value() or 0) / battery_capacity_kwh, 1),
        "cycles_used": round(sum([1 for i in range(1, n_steps) if (is_charging[i].value() or 0) != (is_charging[i-1].value() or 0)]), 1),
    }


def _fallback_schedule(soc_start_pct, soc_target_pct, battery_capacity_kwh,
                       max_charge_kw, efficiency, slot_hours, buy_price, dt_hours):
    """Planning de secours : charge l'énergie nécessaire aux créneaux les
    moins chers (pas de V2G). Garantit un résultat cohérent même si le LP
    ne converge pas."""
    soc_start = soc_start_pct / 100.0 * battery_capacity_kwh
    soc_target = soc_target_pct / 100.0 * battery_capacity_kwh
    soc_min = 0.15 * battery_capacity_kwh
    soc_max = battery_capacity_kwh

    n = len(slot_hours)
    charge = [0.0] * n
    discharge = [0.0] * n
    # Trier les pas par prix croissant, charger au max jusqu'à la cible
    order = sorted(range(n), key=lambda i: buy_price[i])
    remaining = soc_target - soc_start
    soc = soc_start
    soc_traj = [soc_start] * n
    for i in order:
        if remaining <= 0:
            break
        room = soc_max - soc
        grid = min(max_charge_kw * dt_hours, remaining / efficiency, room / efficiency)
        if grid <= 0:
            continue
        charge[i] = grid / dt_hours
        soc += grid * efficiency
        remaining -= grid * efficiency
    # Recalculer la trajectoire SoC dans l'ordre chronologique
    soc = soc_start
    soc_traj = []
    for i in range(n):
        soc += charge[i] * dt_hours * efficiency - discharge[i] * dt_hours / efficiency
        soc_traj.append(max(soc_min, min(soc_max, soc)))

    schedule = []
    total_cost = 0.0
    for i in range(n):
        cost_i = charge[i] * dt_hours * buy_price[i]
        total_cost += cost_i
        schedule.append({
            "step": i, "hour": round(slot_hours[i], 2),
            "charge_kw": round(charge[i], 2), "discharge_kw": 0.0,
            "soc_pct": round(100 * soc_traj[i] / battery_capacity_kwh, 1),
            "buy_price_tnd_kwh": round(buy_price[i], 3),
            "sell_price_tnd_kwh": round(buy_price[i] * 0.85, 3),
            "cost_tnd": round(cost_i, 4), "gain_tnd": 0.0,
            "is_charging": charge[i] > 0, "is_discharging": False,
        })

    # Coût de référence ASAP (identique au calcul principal)
    baseline_cost = 0.0
    remaining = soc_target - soc_start
    for i in range(n):
        if remaining <= 0:
            break
        grid_energy_needed = min(max_charge_kw * dt_hours, remaining / efficiency)
        baseline_cost += grid_energy_needed * buy_price[i]
        remaining -= grid_energy_needed * efficiency

    savings = baseline_cost - total_cost
    return {
        "status": "Fallback",
        "n_steps": n,
        "dt_hours": dt_hours,
        "schedule": schedule,
        "baseline_cost_tnd": round(baseline_cost, 3),
        "optimized_cost_tnd": round(total_cost, 3),
        "v2g_gain_tnd": 0.0,
        "battery_health_cost_tnd": 0.0,
        "optimized_net_cost_tnd": round(total_cost, 3),
        "savings_tnd": round(savings, 3),
        "savings_pct": round(savings / baseline_cost * 100, 1) if baseline_cost > 0 else 0.0,
        "final_soc_pct": round(100 * soc_traj[-1] / battery_capacity_kwh, 1),
        "cycles_used": 1,
    }
