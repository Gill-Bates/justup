<p align="center">
  <img src="app/static/img/justup_1c.svg" width="220" alt="justUp! Logo">
</p>

<p align="center">
  <strong>justUp! – selbst gehosteter Uptime-Monitor. Done right.</strong>
</p>

<p align="center">
  <a href="https://hub.docker.com/r/giiibates/justup"><img src="https://img.shields.io/docker/pulls/giiibates/justup?logo=docker&logoColor=white" alt="Docker Pulls"></a>
  <a href="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey"><img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

---

justUp! prüft Hosts per Ping, HTTP(S), TCP und Zertifikatslaufzeit. Ein geführter
Wizard schlägt passende Checks für neue Targets vor. justUp! speichert Messreihen
lokal, alarmiert per E-Mail und Signal und erzeugt druckoptimierte PDF-Berichte mit
eingebetteten Schriften und hochauflösenden Diagrammen.
Es läuft als **ein einziger Container** – ohne externe Datenbank, ohne Agenten,
ohne Grafana-Stack. Persistenz übernimmt eine eingebettete SQLite-Datenbank mit
verdichteter Zeitreihen-Ablage.

## Schnellstart (Docker)

Voraussetzung: Docker mit Compose-Plugin. Die mitgelieferte Compose-Datei nutzt
`giiibates/justup:latest`. Sie veröffentlicht standardmäßig **keinen Port**;
für den direkten Zugriff ergänzen Sie unter `services.justup` vor dem Start:

```yaml
ports:
  - "8000:8000"
```

```sh
git clone https://github.com/Gill-Bates/justup.git
cd justup
cp .env.example .env
# Fernet-Schlüssel erzeugen und als SECRET_KEY in .env eintragen:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

Mit der Portfreigabe ist die Oberfläche unter `http://localhost:8000` erreichbar.
Das zufällige Startpasswort für `admin` erscheint in `docker logs justup`
und bei jedem Neustart erneut, bis Sie es geändert haben. Die erste Anmeldung
erzwingt den Passwortwechsel. Die Compose-Datei speichert Daten im Verzeichnis
`docker/` und legt erzeugte PDF-Dateien in einem flüchtigen `tmpfs` ab;
Details stehen unter [Docker](docs/getting-started/docker.md).

## Dokumentation

Die vollständige Benutzer- und Betriebsdokumentation ist die zentrale Anlaufstelle:

**→ [Dokumentation](https://gill-bates.github.io/justup/)**

Schnelleinstiege: [Schnellstart](docs/getting-started/quick-start.md) ·
[Installation](docs/getting-started/installation.md) ·
[Umgebungsvariablen](docs/configuration/environment.md) ·
[Sicherheit](docs/security/overview.md)

Für lokale Entwicklung siehe [Setup](docs/development/setup.md) und
[Mitwirken](docs/development/contributing.md).

## Lizenz

Lizenziert unter der [MIT-Lizenz](LICENSE).
