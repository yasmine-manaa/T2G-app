"""
Moteur de prédiction ML pour T2G
=================================
Modèles d'apprentissage automatique pour prédire :
- Coûts de recharge
- Temps de charge optimal
- Demande par station
- Recommandations personnalisées
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import joblib
import os
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from config import Config


def _parse_datetime(value):
    """Parse une date qui peut être un datetime, une ISO 'YYYY-MM-DDTHH:MM:SS'
    ou une chaîne 'YYYY-MM-DD HH:MM:SS'. Retourne None si impossible."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError:
            try:
                return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    return None


class ChargingCostPredictor:
    """Modèle de prédiction des coûts de recharge basé sur ML"""
    
    def __init__(self):
        self.model = GradientBoostingRegressor(
            n_estimators=100,
            learning_rate=0.1,
            max_depth=6,
            random_state=42
        )
        self.scaler = StandardScaler()
        self.feature_names = [
            'battery_capacity', 'soc_start', 'soc_target', 'charging_power',
            'hour', 'day_of_week', 'temperature', 'vehicle_age',
            'distance_last_charge', 'is_peak_hour', 'is_off_peak'
        ]
        self.is_trained = False
        
    def prepare_features(self, sessions_data: List[Dict]) -> np.ndarray:
        """Prépare les caractéristiques pour le modèle"""
        features = []
        for session in sessions_data:
            # Gérer différents formats de date
            parsed_start = _parse_datetime(session.get('start_time'))
            hour = parsed_start.hour if parsed_start else datetime.now().hour
            
            feature_row = [
                session.get('battery_capacity_kwh', 50),
                session.get('soc_start_pct', 50) / 100,
                session.get('soc_target_pct', 90) / 100,
                session.get('charging_rate_kw', 7.4),
                hour,
                parsed_start.weekday() if parsed_start else datetime.now().weekday(),
                session.get('temperature_c', 25),
                session.get('vehicle_age_years', 2),
                session.get('distance_since_last_charge_km', 100),
                1 if (18 <= hour < 22) else 0,  # Heures de pointe
                1 if (hour >= 22 or hour < 6) else 0  # Heures creuses
            ]
            features.append(feature_row)
        
        return np.array(features)
    
    def train(self, sessions_data: List[Dict]):
        """Entraîne le modèle sur les données historiques"""
        if len(sessions_data) < 50:
            print("Pas assez de données pour l'entraînement (min 50 sessions)")
            return False
            
        X = self.prepare_features(sessions_data)
        y = np.array([s.get('cost_tnd', 0) for s in sessions_data])
        
        # Normalisation des features
        X_scaled = self.scaler.fit_transform(X)
        
        # Entraînement
        X_train, X_test, y_train, y_test = train_test_split(
            X_scaled, y, test_size=0.2, random_state=42
        )
        
        self.model.fit(X_train, y_train)
        
        # Évaluation
        y_pred = self.model.predict(X_test)
        mae = mean_absolute_error(y_test, y_pred)
        r2 = r2_score(y_test, y_pred)
        
        print(f"Modèle entraîné - MAE: {mae:.3f} TND, R²: {r2:.3f}")

        # Validation croisée 5-fold pour estimer la robustesse du modèle
        try:
            from sklearn.model_selection import cross_val_score
            cv_scores = cross_val_score(
                self.model, X_scaled, y, cv=5, scoring="neg_mean_absolute_error"
            )
            print(f"Validation croisée 5-fold - MAE moyen: "
                  f"{-cv_scores.mean():.3f} TND (±{-cv_scores.std():.3f})")
        except Exception as e:
            print(f"Validation croisée ignorée: {e}")

        self.is_trained = True
        return True
    
    def predict_cost(self, battery_capacity: float, soc_start: int, soc_target: int,
                    charging_power: float, start_time, temperature: float = 25,
                    vehicle_age: float = 2, distance: float = 100) -> Dict:
        """Prédit le coût de recharge pour une session donnée"""
        # Convertir start_time en datetime si c'est une string
        if isinstance(start_time, str):
            try:
                start_time = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            except:
                try:
                    start_time = datetime.fromisoformat(start_time)
                except:
                    start_time = datetime.now()
        
        if not self.is_trained:
            # Fallback au calcul basé sur les tarifs
            return self._fallback_cost_prediction(battery_capacity, soc_start, soc_target,
                                                 charging_power, start_time)
        
        hour = start_time.hour
        feature_row = np.array([[
            battery_capacity,
            soc_start / 100,
            soc_target / 100,
            charging_power,
            hour,
            start_time.weekday(),
            temperature,
            vehicle_age,
            distance,
            1 if (18 <= hour < 22) else 0,
            1 if (hour >= 22 or hour < 6) else 0
        ]])
        
        X_scaled = self.scaler.transform(feature_row)
        predicted_cost = self.model.predict(X_scaled)[0]
        
        # Calcul de l'intervalle de confiance
        cost_std = np.std([predicted_cost * 0.9, predicted_cost, predicted_cost * 1.1])
        
        return {
            'predicted_cost_tnd': round(max(0, predicted_cost), 3),
            'confidence_interval': {
                'min': round(max(0, predicted_cost - cost_std), 3),
                'max': round(predicted_cost + cost_std, 3)
            },
            'method': 'ml' if self.is_trained else 'tariff_based'
        }
    
    def _fallback_cost_prediction(self, battery_capacity: float, soc_start: int, 
                                   soc_target: int, charging_power: float, 
                                   start_time) -> Dict:
        """Prédiction basée sur les tarifs si ML non disponible"""
        # Convertir start_time en datetime si c'est une string
        if isinstance(start_time, str):
            try:
                start_time = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            except:
                try:
                    start_time = datetime.fromisoformat(start_time)
                except:
                    start_time = datetime.now()
        
        hour = start_time.hour
        if hour >= 22 or hour < 6:
            tariff = Config.TARIFF_OFFPEAK
        elif 18 <= hour < 22:
            tariff = Config.TARIFF_PEAK
        else:
            tariff = Config.TARIFF_NORMAL
        
        # Énergie nécessaire
        energy_needed = (soc_target - soc_start) / 100 * battery_capacity
        # Temps de charge estimé
        charging_time = energy_needed / charging_power
        # Coût
        cost = energy_needed * tariff
        
        return {
            'predicted_cost_tnd': round(cost, 3),
            'estimated_duration_h': round(charging_time, 2),
            'energy_needed_kwh': round(energy_needed, 2),
            'tarif_type': 'Heures creuses' if hour >= 22 or hour < 6 else 'Heures de pointe' if 18 <= hour < 22 else 'Heures pleines',
            'tarif_tnd_kwh': tariff,
            'method': 'tariff_based'
        }


class ChargingTimePredictor:
    """Modèle de prédiction du temps de charge optimal"""
    
    def __init__(self):
        self.model = RandomForestRegressor(
            n_estimators=50,
            max_depth=8,
            random_state=42
        )
        self.is_trained = False
    
    def predict_optimal_time(self, current_soc: int, target_soc: int, 
                           battery_capacity: float, max_power: float,
                           flexibility_hours: int = 24) -> List[Dict]:
        """Prédit les meilleurs moments pour charger"""
        optimal_times = []
        now = datetime.now()
        
        # Tarifs par heure (STEG « Trois Postes », voir config.py)
        def get_tariff(hour):
            if hour >= 22 or hour < 6:
                return Config.TARIFF_OFFPEAK, "Heures creuses"
            elif 18 <= hour < 22:
                return Config.TARIFF_PEAK, "Heures de pointe"
            else:
                return Config.TARIFF_NORMAL, "Heures pleines"
        
        # Analyser chaque heure dans la fenêtre de flexibilité
        for hour_offset in range(flexibility_hours):
            start_time = now + timedelta(hours=hour_offset)
            hour = start_time.hour
            
            tariff, tariff_type = get_tariff(hour)
            
            # Calculer le coût estimé
            energy_needed = (target_soc - current_soc) / 100 * battery_capacity
            estimated_cost = energy_needed * tariff
            
            # Score de qualité (plus c'est bas, mieux c'est)
            score = estimated_cost
            
            optimal_times.append({
                'start_time': start_time.strftime("%Y-%m-%d %H:%M"),
                'hour': hour,
                'tariff_tnd_kwh': tariff,
                'tariff_type': tariff_type,
                'estimated_cost_tnd': round(estimated_cost, 3),
                'estimated_duration_h': round(energy_needed / max_power, 2),
                'energy_needed_kwh': round(energy_needed, 2),
                'score': round(score, 3),
                'recommendation': self._get_recommendation(tariff_type, hour)
            })
        
        # Trier par score (coût)
        optimal_times.sort(key=lambda x: x['score'])
        
        return optimal_times[:5]  # Retourner les 5 meilleurs moments
    
    def _get_recommendation(self, tariff_type: str, hour: int) -> str:
        """Génère une recommandation textuelle"""
        if tariff_type == "Heures creuses":
            return "Excellent moment - tarif minimum"
        elif tariff_type == "Heures pleines":
            return "Moment acceptable - tarif standard"
        else:
            return "Moment à éviter - tarif maximum"


class RAGAdviceSystem:
    """Système RAG (Retrieval-Augmented Generation) pour les conseils personnalisés"""
    
    def __init__(self):
        self.knowledge_base = self._build_knowledge_base()
    
    def _build_knowledge_base(self) -> Dict:
        """Construit la base de connaissances pour les conseils"""
        return {
            'cost_optimization': [
                {
                    'condition': 'high_cost',
                    'advice': 'Pour réduire vos coûts, programmez vos recharges pendant les heures creuses (22h-6h) où le tarif est de 0.141 TND/kWh au lieu de 0.452 TND/kWh en pointe.',
                    'potential_savings': 'jusqu\'à 60% d\'économie',
                    'priority': 'high'
                },
                {
                    'condition': 'v2g_opportunity',
                    'advice': 'Votre véhicule est compatible V2G. Activez le mode V2G pendant les heures de pointe (18h-22h) pour revendre l\'énergie au réseau et générer des revenus.',
                    'potential_savings': 'revenus potentiels de 0.384 TND/kWh vendu',
                    'priority': 'medium'
                },
                {
                    'condition': 'battery_health',
                    'advice': 'Pour préserver la santé de votre batterie, évitez les décharges profondes en dessous de 15% et privilégiez des charges fréquentes plutôt que des charges complètes.',
                    'potential_savings': 'prolongation de la durée de vie de 20-30%',
                    'priority': 'medium'
                }
            ],
            'charging_efficiency': [
                {
                    'condition': 'optimal_soc',
                    'advice': 'Maintenez votre batterie entre 20% et 80% pour une meilleure longévité. Les charges complètes à 100% ne sont nécessaires que pour les longs trajets.',
                    'impact': 'réduction de la dégradation de la batterie',
                    'priority': 'high'
                },
                {
                    'condition': 'temperature',
                    'advice': 'Évitez de charger votre véhicule lors de températures extrêmes (< 0°C ou > 35°C) pour optimiser le rendement de charge.',
                    'impact': 'amélioration du rendement de 5-10%',
                    'priority': 'low'
                }
            ],
            'usage_patterns': [
                {
                    'condition': 'frequent_user',
                    'advice': 'En tant qu\'utilisateur fréquent, envisagez d\'installer une borne de recharge à domicile pour bénéficier des tarifs résidentiels avantageux.',
                    'impact': 'économie substantielle sur le long terme',
                    'priority': 'medium'
                },
                {
                    'condition': 'weekend_charger',
                    'advice': 'Vos habitudes de charge le weekend sont optimales. Continuez à programmer vos recharges pendant les heures creuses du weekend.',
                    'impact': 'maintien des coûts au minimum',
                    'priority': 'low'
                }
            ]
        }
    
    def get_personalized_advice(self, user_data: Dict, session_history: List[Dict]) -> List[Dict]:
        """Génère des conseils personnalisés basés sur le profil utilisateur.

        Toujours retourne au moins des conseils de base (tarifs STEG / bonnes pratiques),
        même sans historique riche.
        """
        advice_list = []

        costs = [float(s.get('cost_tnd') or 0) for s in (session_history or [])]
        avg_cost = float(np.mean(costs)) if costs else 0.0
        v2g_usage = sum(1 for s in (session_history or []) if s.get('mode') == 'v2g')
        v1g_usage = sum(1 for s in (session_history or []) if s.get('mode') == 'v1g')
        classique_usage = sum(1 for s in (session_history or []) if s.get('mode') == 'classique')
        total_sessions = len(session_history or [])
        v2g_eligible = bool(user_data.get('v2g_eligible', False))
        vehicle_age = float(user_data.get('vehicle_age_years') or 0)

        # Conseil de base toujours présent
        advice_list.append({
            'category': 'Tarifs STEG',
            'title': 'Profitez des heures creuses',
            'advice': (
                'En Tunisie, le kWh coûte 0,141 TND en heures creuses (22h–6h), '
                '0,168 TND en heures pleines et 0,452 TND en pointe (18h–22h) '
                '(tarif STEG « Trois Postes horaires »). '
                'Branchez dès l\'arrivée et laissez l\'optimiseur T2G décaler la charge.'
            ),
            'action': 'Optimiser une session depuis le tableau de bord',
            'priority': 'high',
            'potential_savings': 'jusqu\'à 60% vs charge en pointe',
        })

        if avg_cost >= 5 or classique_usage >= max(3, total_sessions * 0.4):
            advice_list.append({
                'category': 'Coûts',
                'title': 'Réduire le coût moyen de recharge',
                'advice': (
                    f'Votre coût moyen est de {avg_cost:.2f} TND'
                    f'{" sur " + str(total_sessions) + " sessions" if total_sessions else ""}. '
                    'Passez en mode Smart V1G pour reporter la charge hors pointe.'
                ),
                'action': 'Lancer une optimisation V1G',
                'priority': 'high',
                'potential_savings': f'~{max(avg_cost * 0.4, 1):.1f} TND / session',
            })

        if v2g_eligible and v2g_usage == 0:
            advice_list.append({
                'category': 'Revenus',
                'title': 'Activez le V2G',
                'advice': (
                    'Votre véhicule est compatible V2G. Pendant les heures de pointe, '
                    'revendez de l\'énergie au réseau puis rechargez en heures creuses.'
                ),
                'action': 'Activer le commutateur V2G puis Optimiser',
                'priority': 'medium',
                'potential_savings': '~0,38 TND/kWh revendu',
            })
        elif v2g_usage > 0:
            advice_list.append({
                'category': 'Revenus',
                'title': 'V2G déjà utilisé',
                'advice': (
                    f'Vous avez {v2g_usage} session(s) en mode V2G. '
                    'Continuez sur les fenêtres soir→nuit pour maximiser le gain net.'
                ),
                'action': 'Consulter le solde V2G dans le profil',
                'priority': 'low',
                'potential_savings': 'gains cumulés sur le solde V2G',
            })

        if vehicle_age >= 2.5:
            advice_list.append({
                'category': 'Santé batterie',
                'title': 'Préserver la batterie',
                'advice': (
                    f'Véhicule ~{vehicle_age:.1f} an(s) : visez 20–80 % SoC au quotidien, '
                    'évitez les décharges sous 15 % et les charges 100 % sauf long trajet.'
                ),
                'action': 'Régler la cible SoC à 80 %',
                'priority': 'medium',
                'potential_savings': '+20–30 % durée de vie estimée',
            })
        else:
            advice_list.append({
                'category': 'Santé batterie',
                'title': 'Bonnes pratiques SoC',
                'advice': (
                    'Maintenez la batterie entre 20 % et 80 % et limitez les cycles '
                    'charge/décharge inutiles (paramètre max cycles dans Réglages).'
                ),
                'action': 'Vérifier les paramètres d\'optimisation',
                'priority': 'low',
                'potential_savings': 'meilleure longévité batterie',
            })

        # Comptage correct : nombre de sessions en pointe (pas d'heures distinctes)
        peak_sessions = 0
        for session in session_history or []:
            try:
                st = session.get('start_time')
                if isinstance(st, str):
                    if 'T' in st:
                        hour = datetime.fromisoformat(st.replace('Z', '+00:00')).hour
                    else:
                        hour = datetime.strptime(st, "%Y-%m-%d %H:%M:%S").hour
                elif st is not None:
                    hour = st.hour
                else:
                    continue
                if 18 <= hour < 22:
                    peak_sessions += 1
            except Exception:
                continue

        if total_sessions > 0 and peak_sessions / total_sessions >= 0.25:
            advice_list.append({
                'category': 'Timing',
                'title': 'Trop de charges en pointe',
                'advice': (
                    f'{peak_sessions}/{total_sessions} sessions démarrent entre 18h et 22h. '
                    'Branchez plus tôt ou laissez T2G reporter après 22h.'
                ),
                'action': 'Programmer après 22h',
                'priority': 'high',
                'potential_savings': 'jusqu\'à 60 % d\'économie',
            })

        if v1g_usage == 0 and total_sessions >= 1:
            advice_list.append({
                'category': 'Optimisation',
                'title': 'Essayez le Smart V1G',
                'advice': (
                    'Sans V2G, le mode Smart V1G décale déjà la charge vers les heures '
                    'les moins chères sur votre fenêtre de stationnement.'
                ),
                'action': 'Optimiser sans V2G (commutateur OFF)',
                'priority': 'medium',
                'potential_savings': '20–40 % selon la fenêtre',
            })

        # Priorité : high > medium > low, max 6
        priority_rank = {'high': 0, 'medium': 1, 'low': 2}
        advice_list.sort(key=lambda a: priority_rank.get(a.get('priority', 'low'), 9))
        return advice_list[:6]


class DemandPredictor:
    """Prédiction de la demande par station"""
    
    def predict_station_demand(self, station_id: int, hour: int, day_of_week: int,
                               historical_data: List[Dict]) -> Dict:
        """Prédit la demande pour une station donnée"""
        # Filtrer les données pour cette station
        station_data = [s for s in historical_data if s.get('station_id') == station_id]
        
        if not station_data:
            return {
                'predicted_demand': 'low',
                'confidence': 'low',
                'recommendation': 'Station disponible - peu de données historiques'
            }
        
        # Analyser les patterns horaires
        hourly_counts = {}
        for session in station_data:
            parsed = _parse_datetime(session.get('start_time'))
            h = parsed.hour if parsed else datetime.now().hour
            hourly_counts[h] = hourly_counts.get(h, 0) + 1
        
        avg_demand = np.mean(list(hourly_counts.values())) if hourly_counts else 0
        current_hour_demand = hourly_counts.get(hour, 0)
        
        # Prédire le niveau de demande
        if current_hour_demand > avg_demand * 1.5:
            demand_level = 'high'
            recommendation = 'Forte affluence prévue - arrivez tôt ou choisissez une autre station'
        elif current_hour_demand > avg_demand:
            demand_level = 'medium'
            recommendation = 'Affluence modérée - disponibilité probable'
        else:
            demand_level = 'low'
            recommendation = 'Faible affluence - station disponible'
        
        return {
            'predicted_demand': demand_level,
            'current_hour_demand': current_hour_demand,
            'average_demand': round(avg_demand, 1),
            'confidence': 'medium' if len(station_data) > 10 else 'low',
            'recommendation': recommendation
        }


# Instance globale des prédicteurs
cost_predictor = ChargingCostPredictor()
time_predictor = ChargingTimePredictor()
rag_system = RAGAdviceSystem()
demand_predictor = DemandPredictor()