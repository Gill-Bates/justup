# Quick Start

Run justUp! as a container in a few minutes. Details: [Installation](installation.md)
and [Docker](docker.md).

## Prerequisites

- Docker with the Compose plugin
- Outbound network access to the hosts you want to monitor

## 1. Get the repository

```bash
git clone https://github.com/Gill-Bates/justup.git
cd justup
cp .env.example .env
```

## 2. Generate SECRET_KEY

`SECRET_KEY` encrypts stored secrets and keys the [session token](../api/authentication.md#session-token) hashes. It has no default.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add the value to `.env`:

```bash
SECRET_KEY=<generated-key>
```

!!! danger "Keep the key"
    Never commit the key. If you lose it, stored secrets cannot be read.
    For rotation, see [Environment variables](../configuration/environment.md).

## 3. Start

The supplied Compose file publishes no port. Add this under `services.justup` in
`docker/docker-compose.yml`:

```yaml
ports:
  - "8000:8000"
```

```bash
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

## 4. Sign in

Open `http://localhost:8000` and sign in as `admin`. The one-time password is
printed to the container output until you change it:

```bash
docker logs justup
```

justUp! forces a password change at first sign-in. Until then, browser pages
redirect to `/ui/change-password` and API calls answer HTTP 428 (`Password change required`).

Next: [First steps](first-steps.md).
