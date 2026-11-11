import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    # Par défaut : SQLite local (zéro configuration, parfait pour lancer vite).
    # Pour la prod / Docker : définir DATABASE_URL=postgresql://user:pass@db:5432/t2g_db
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'data', 't2g.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SECRET_KEY = os.environ.get("SECRET_KEY", "t2g-dev-secret-changer-en-prod")
    JWT_EXP_DELTA = timedelta(hours=12)

    # Devise de l'application
    CURRENCY = "TND"

    # Tarification réseau (TND/kWh) - inspirée du tarif STEG « Trois Postes
    # horaires » (BT spéciaux, hors taxes + TVA 13% + taxes STEG ~0.01 TND).
    # Référence : steg.com.tn - clients BT spéciaux (jour/pointe soir/nuit).
    #   Nuit (heures creuses)  : 116 mill/kWh  -> ~0.141 TND
    #   Jour (normal)          : 140 mill/kWh  -> ~0.168 TND
    #   Pointe soir            : 391 mill/kWh  -> ~0.452 TND
    TARIFF_OFFPEAK = 0.141   # 22h-6h (nuit STEG)
    TARIFF_NORMAL = 0.168    # 6h-18h (jour STEG)
    TARIFF_PEAK = 0.452      # 18h-22h (pointe soir STEG)

    # Prix de rachat V2G (le réseau paie le client qui restitue de l'énergie)
    # généralement un % du tarif de pointe pour rester incitatif mais rentable pour l'opérateur
    V2G_SELL_RATIO = 0.85
