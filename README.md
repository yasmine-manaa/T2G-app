# ⚡ T2G — Smart EV Charging & Transport-to-Grid (Tunisie)

Application complète (Frontend + Backend + Base de données + IA) de gestion
intelligente de la recharge des véhicules électriques, avec **restitution
d'énergie au réseau (V2G)**, adaptée au contexte tunisien (devise : **TND**).

Conforme au cahier des charges du projet :
- **Frontend** : dashboard de suivi en temps réel (graphes de puissance P(t), reçu financier coût/gain V2G).
- **Backend** : API REST Python/Flask.
- **Base de données** : PostgreSQL (tables `users`, `vehicles`, `ev_sessions`, `payments`, `optimization_results`).
- **Dataset réel** : sessions de recharge réelles issues du dataset public
  *"Workplace Charging Experiment"* (Asensio et al., DOI 10.7910/DVN/NFPQLW —
  3 395 sessions, 85 conducteurs, 105 bornes, 25 sites),
  converties dans le schéma tunisien de l'app — voir `dataset/`.
  **Note de provenance** : les sessions datent de 2014-2015 (étude US,
  données anonymisées publiques) et sont recentrées sur ~12 mois glissants
  par `import_workplace_dataset.py`. Les champs absents des données brutes
  (villes tunisiennes, véhicules, coûts TND, SoC, coordonnées GPS des
  stations dataset) sont des **enrichissements estimés**, documentés dans
  le script — pas des mesures tunisiennes réelles.
- **Moteur IA** : optimisation linéaire (Python / **PuLP**) du planning de charge/décharge V1G/V2G.
- **Conteneurisation** : Docker & Docker Compose (frontend, backend, DB).

---

## 🚀 Démarrage rapide (Docker — recommandé)

```bash
# 1. Importer le dataset réel (une seule fois)
cd dataset && python import_workplace_dataset.py && cd ..

# 2. Lancer toute la stack
docker compose up --build
```

- Frontend : http://localhost:8080
- API backend : http://localhost:5000/api/health
- PostgreSQL : localhost:5432 (user: `t2g_user` / pass: `t2g_pass` / db: `t2g_db`)

Le compte de démonstration est créé automatiquement au premier démarrage :
**identifiant `demo` / mot de passe `demo1234`**.

---

## 🛠️ Démarrage manuel (sans Docker)

### 1. Dataset
```bash
cd dataset
python import_workplace_dataset.py   # dataset réel (recommandé)
# ou : python generate_dataset.py    # fallback synthétique tunisien
```

### 2. Backend (Flask + SQLite par défaut — zéro config)
```bash
cd backend
python3 -m venv venv && source venv/bin/activate   # Windows : venv\Scripts\activate
pip install -r requirements.txt
python seed.py            # importe le dataset dans la base (SQLite: data/t2g.db)
python app.py              # démarre l'API sur http://localhost:5000
```

> Pour utiliser PostgreSQL au lieu de SQLite, exportez :
> `export DATABASE_URL=postgresql://user:pass@localhost:5432/t2g_db` avant `python seed.py`.

### 3. Frontend (fichiers statiques)
```bash
cd frontend
python3 -m http.server 8080
```
Ouvrez http://localhost:8080 — connectez-vous avec `demo` / `demo1234`.

---

## 🧠 Le moteur d'optimisation IA (V1G / V2G)

Fichier : `backend/optimizer.py`. Formulation en **programmation linéaire (PuLP)** :

```
min  Σ (prix_achat_t · charge_t) − Σ (prix_vente_t · décharge_t)

s.c.  SoC(t+1) = SoC(t) + η·charge_t·Δt − décharge_t·Δt/η
      SoC_min ≤ SoC(t) ≤ SoC_max
      SoC(T_départ) ≥ SoC_cible
      0 ≤ charge_t ≤ P_max_charge
      0 ≤ décharge_t ≤ P_max_décharge   (si V2G activé)
      t ∈ [T_arrivée, T_départ]
```

Le solveur (CBC, embarqué avec PuLP) trouve automatiquement le planning qui
recharge le véhicule pendant les heures creuses (tarif bas) et, si le V2G est
activé, restitue de l'énergie au réseau pendant les heures de pointe (tarif
haut), tout en garantissant le niveau de batterie requis au départ du client.

Tarification utilisée dans `backend/config.py`, inspirée du tarif STEG
**« Trois Postes horaires »** (BT spéciaux, steg.com.tn — hors taxes + TVA
13 % + taxes ~0.01 TND/kWh) :

| Période | Heures | Tarif TND/kWh |
|---|---|---|
| Nuit (heures creuses) | 22h–6h | 0.141 |
| Jour (heures pleines) | 6h–18h | 0.168 |
| Pointe soir | 18h–22h | 0.452 |

*Les fenêtres horaires et le prix de revente V2G (85 % du tarif de pointe)
restent des hypothèses de modélisation.*

---

## 📊 Le dataset

`dataset/ev_charging_patterns_tunisia.csv` contient **3 395 sessions de
recharge réelles** (pas des données synthétiques) issues de l'étude publique
*"Replication Data for: A Field Experiment on Workplace Norms and Electric
Vehicle Charging Etiquette"* — Asensio, Apablaza, Lawson & Walsh (Georgia
Tech), Harvard Dataverse, DOI `10.7910/DVN/NFPQLW`, fichier
`station_data_dataverse.tab`. Le fichier brut est conservé dans
`dataset/workplace_charging_sessions_2014_2015.tsv`.

**Licence des données : CC0 1.0** (domaine public — réutilisation libre,
crédité mais non requis). Citation recommandée :
> Asensio, O. I., Apablaza, C. Z., Lawson, M. C., & Walsh, S. E. (2020).
> *Replication Data for: A Field Experiment on Workplace Norms and Electric
> Vehicle Charging Etiquette.* Harvard Dataverse. https://doi.org/10.7910/DVN/NFPQLW

`dataset/import_workplace_dataset.py` convertit ces données réelles dans le
schéma de l'application :

- **Réel, conservé tel quel** : énergie livrée (kWh), taux de charge moyen,
  identifiants utilisateurs/bornes, et les **patterns horaires**
  (time-of-day : heures d'arrivée/départ et durées réelles).
- **Recentrement temporel** : les **dates calendaires** 2014-2015 sont
  décalées pour couvrir les ~12 derniers mois — les patterns horaires
  (time-of-day) et les durées sont inchangés.
- **Enrichi (estimé, documenté dans le script)** : modèle/capacité batterie
  par utilisateur (parc EV tunisien), villes/quartiers des stations (les 25
  sites US sont répartis sur les villes tunisiennes), type de chargeur
  (inféré du taux de charge), coût en TND (énergie × tarif STEG indicatif),
  SoC (cohérent avec l'énergie livrée), distance (~5 km/kWh), température
  (saison tunisienne selon le mois), éligibilité V2G (~35 %).

`dataset/generate_dataset.py` reste disponible pour régénérer un dataset
**synthétique** 100 % tunisien (~3 200 sessions) si besoin — il produit le
même fichier CSV.

`dataset/import_osm_stations.py` importe en plus les **vraies bornes de
recharge EV recensées en Tunisie** sur OpenStreetMap (via Overpass) —
actuellement 6 bornes publiques (codes `OSM-*`), affichées sur la carte et
réservables.

Si vous disposez d'un compte Kaggle et souhaitez le dataset original en
complément : [EV Charging Patterns (Kaggle)](https://www.kaggle.com/datasets/valakhorasani/electric-vehicle-charging-patterns).

---

## 📁 Structure du projet

```
t2g-app/
├── docker-compose.yml
├── backend/
│   ├── app.py            # API Flask (auth, sessions, optimisation, stats)
│   ├── models.py         # Modèles SQLAlchemy (users, vehicles, sessions, payments)
│   ├── optimizer.py       # Moteur IA PuLP (V1G/V2G)
│   ├── seed.py            # Import du dataset CSV → base de données
│   ├── config.py          # Config (DB, tarifs, devise)
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html         # Dashboard (KPIs, graphes, table sessions)
│   ├── app.js              # Logique API + Chart.js
│   ├── nginx.conf
│   └── Dockerfile
├── dataset/
│   ├── generate_dataset.py              # fallback : dataset synthétique tunisien
│   ├── import_workplace_dataset.py      # import du dataset RÉEL (Dataverse)
│   ├── workplace_charging_sessions_2014_2015.tsv  # données brutes réelles
│   └── ev_charging_patterns_tunisia.csv # CSV final consommé par seed.py
└── database/
    └── init.sql
```

## 🔌 Principaux endpoints API

| Méthode | Route | Description |
|---|---|---|
| POST | `/api/auth/register` | Créer un compte + véhicule |
| POST | `/api/auth/login` | Connexion (JWT) |
| GET | `/api/sessions` | Liste des sessions (filtres : `city`, `mode`, `user_id`) |
| POST | `/api/optimize/<session_id>` | Lance l'optimisation IA V1G/V2G |
| GET | `/api/stats/dashboard` | KPIs agrégés pour le dashboard |
| GET | `/api/stations` | Liste des bornes |
| GET | `/api/vehicles` | Véhicules d'un utilisateur |

---

## Notes

- Le dataset principal est **réel** (étude workplace US, données anonymisées
  publiques) ; seules les colonnes absentes des données brutes (modèle de
  véhicule, ville tunisienne, coût TND, SoC...) sont estimées — voir
  `dataset/import_workplace_dataset.py` pour le détail du mapping.
- `generate_dataset.py` produit un dataset 100 % synthétique (contexte
  tunisien) en fallback.
- Mot de passe de tous les comptes importés depuis le dataset : `demo1234`.
