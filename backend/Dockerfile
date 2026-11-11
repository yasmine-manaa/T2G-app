FROM python:3.11-slim

WORKDIR /app

# CBC (solveur utilisé par PuLP) + libpq pour psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    coinor-cbc gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Le dossier ./dataset est monté en volume par docker-compose (voir docker-compose.yml)

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
