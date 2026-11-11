"""
Importe les VRAIES bornes de recharge EV en Tunisie depuis OpenStreetMap
(via l'API Overpass) dans la table `stations`.

Complément du dataset Workplace Charging Experiment : les sessions
historiques restent rattachées à leurs stations dataset (STN-*), tandis
que les bornes OSM-* sont de vraies bornes publiques recensées, affichées
sur la carte et réservables.

Usage : python import_osm_stations.py
"""
import json
import math
import os
import sys
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app import create_app
from models import db, Station

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Bounding box Tunisie (lat_min, lon_min, lat_max, lon_max)
BBOX = "30.0,7.0,38.5,12.0"

# Centres-villes pour rattacher chaque borne à la ville la plus proche
CITY_COORDS = {
    "Tunis": (36.8065, 10.1815), "Ariana": (36.8819, 10.1930),
    "La Marsa": (36.8760, 10.3220), "Ben Arous": (36.7532, 10.2280),
    "Sousse": (35.8256, 10.6369), "Sfax": (34.7406, 10.7603),
    "Nabeul": (36.4561, 10.7357), "Monastir": (35.7839, 10.8260),
    "Bizerte": (37.2744, 9.8739), "Gabès": (33.8815, 10.0982),
    "Kairouan": (35.6781, 10.0963), "Djerba": (33.9229, 10.8486),
}

QUERY = f"""
[out:json][timeout:120];
(
  node["amenity"="charging_station"]({BBOX});
  way["amenity"="charging_station"]({BBOX});
  relation["amenity"="charging_station"]({BBOX});
);
out center tags;
"""


def ville_la_plus_proche(lat, lon):
    best, best_d = None, float("inf")
    for city, (clat, clon) in CITY_COORDS.items():
        d = (lat - clat) ** 2 + (lon - clon) ** 2
        if d < best_d:
            best, best_d = city, d
    return best


def puissance_depuis_tags(tags):
    """Tente d'extraire une puissance (kW) des tags OSM."""
    for key in ("socket:capacity", "capacity", "socket:power"):
        v = tags.get(key, "")
        try:
            kw = float(str(v).replace("kW", "").replace("kwh", "").strip())
            if 1 <= kw <= 400:
                return kw
        except ValueError:
            pass
    return 22.0  # valeur médiane par défaut


def fetch_overpass():
    data = urllib.parse.urlencode({"data": QUERY}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data,
                                 headers={"User-Agent": "t2g-app/1.0"})
    with urllib.request.urlopen(req, timeout=150) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("Requête Overpass (bornes EV Tunisie)…")
    result = fetch_overpass()
    elements = result.get("elements", [])
    print(f"{len(elements)} éléments trouvés dans OSM.")

    app = create_app()
    with app.app_context():
        created = 0
        for el in elements:
            tags = el.get("tags", {})
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            osm_id = el.get("id")
            code = f"OSM-{osm_id}"
            if Station.query.filter_by(station_code=code).first():
                continue
            name = tags.get("name") or tags.get("operator") or "Borne publique"
            city = ville_la_plus_proche(lat, lon)
            power = puissance_depuis_tags(tags)
            charger_type = "DC Rapide" if power >= 40 else ("AC Rapide" if power >= 11 else "AC Lent")
            station = Station(
                station_code=code,
                city=city,
                area=name[:80],
                charger_type=charger_type,
                charger_power_kw=power,
                latitude=round(lat, 6),
                longitude=round(lon, 6),
            )
            db.session.add(station)
            created += 1
        db.session.commit()
        print(f"{created} vraies bornes ajoutées (total stations : "
              f"{Station.query.count()}).")


if __name__ == "__main__":
    main()
