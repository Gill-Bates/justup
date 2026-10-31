# Quick Start

This page gets justUp! running as a container in a few minutes. For a detailed
guide, see [Installation](installation.md) and [Docker](docker.md).

## Prerequisites

- Docker and the Compose plugin (`docker compose`)
- A Fernet key for `SECRET_KEY` (see below)
- Outbound network connectivity to the hosts you want to monitor

## 1. Get the repository and create `.env`

```bash
git clone https://github.com/Gill-Bates/justup.git
cd justup
cp .env.example .env
```

## 2. Generate SECRET_KEY

`SECRET_KEY` is mandatory. justUp! uses it for Fernet encryption of secrets at
rest and for the HMAC signature of session and CSRF tokens. There is
deliberately no default.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add the value to `.env`:

```bash
SECRET_KEY=<generated-key>
```

!!! danger "Never commit the key"
    Store `SECRET_KEY` only in `.env` or in the environment, never in the
    repository. If you lose the key, the encrypted secrets can no longer be
    read. For rotation, see [Environment Variables](../configuration/environment.md).

## 3. Start

The supplied Compose file does not publish a port. To access justUp! directly,
add this under `services.justup` in `docker/docker-compose.yml` before starting:

```yaml
ports:
  - "8000:8000"
```

```bash
docker compose --env-file .env -f docker/docker-compose.yml up -d
```

## 4. Sign in

Open `http://localhost:8000` and sign in as `admin`.

On the first start, a random one-time password (pattern `Tujoza47!`) is
generated and printed to stdout, visible in `docker logs justup`, on
every start until it has been changed. It is not sent to the application log
handlers and is kept in a private file that is deleted on password change. After signing in,
justUp! forces a password change; until then all other endpoints respond with
HTTP 423.

!!! tip "Next step"
    Create your first target and set up recipients:
    [First Steps](first-steps.md).
