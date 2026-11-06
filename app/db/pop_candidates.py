#!/usr/bin/env python3
#
# app/db/pop_candidates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bounded, non-authoritative PoP observations and explicit human review."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import sqlite3

from app.db import sqlite as sqlite_db
from app.db.pop_locations import CURATED_ANCHOR_KM, valid_coordinates
from app.services.ixp_import import great_circle_km
from app.utils.time import utcnow

MAX_CANDIDATES = 5000
MAX_OBSERVATIONS_PER_CANDIDATE = 20
# Observed and proposed/reviewed positions farther apart than this conflict.
LOCATION_CONFLICT_KM = 250
CANDIDATE_CODE_RE = re.compile(r"^[a-z][a-z0-9]{1,4}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")


def observe(
    conn: sqlite3.Connection,
    *,
    code: str,
    hostname: str,
    public_ip: str,
    provider: str,
    measurement_key: str,
    proposal: dict | None = None,
) -> bool:
    """Record one route observation; return whether it was new, never a lookup result."""
    code = code.strip().lower()
    hostname = hostname.strip().lower().rstrip(".")
    if (
        not CANDIDATE_CODE_RE.fullmatch(code)
        or not _HOST.fullmatch(hostname)
        or not re.fullmatch(r"[a-z0-9_-]{1,40}", provider)
    ):
        return False
    try:
        address = ipaddress.ip_address(public_ip)
    except ValueError:
        return False
    if not address.is_global or not re.fullmatch(r"[0-9a-f]{64}", measurement_key):
        return False
    network = ipaddress.ip_network(f"{address}/{24 if address.version == 4 else 48}", strict=False)
    context_key = hashlib.sha256(str(network).encode()).hexdigest()
    guess = proposal or {}
    country = str(guess.get("country") or "").upper()[:5]
    city = str(guess.get("city") or "").strip()[:64]
    if not re.fullmatch(r"[A-Z]{2}", country):
        country = ""
    if not city.isprintable():
        city = ""
    lat, lon = guess.get("lat"), guess.get("lon")
    if not valid_coordinates(lat, lon):
        lat = lon = None
    source = "geolite2-coarse" if city and country and lat is not None else None
    canonical_guess = guess.get("canonical_guess") if source else None
    if not isinstance(canonical_guess, str) or not CANDIDATE_CODE_RE.fullmatch(canonical_guess):
        canonical_guess = None
    if canonical_guess:
        source = "geolite2-city+curated-code"
    now = utcnow().isoformat()
    with sqlite_db.transaction(conn, immediate=True):
        row = conn.execute(
            "SELECT id, country_guess, proposal_lat, proposal_lon, proposal_source "
            "FROM pop_alias_candidates WHERE alias_code = ?",
            (code,),
        ).fetchone()
        if row is None:
            if conn.execute("SELECT COUNT(*) FROM pop_alias_candidates").fetchone()[0] >= MAX_CANDIDATES:
                return False
            cur = conn.execute(
                """INSERT INTO pop_alias_candidates
                (alias_code, canonical_guess, city_guess, country_guess, seen_count, last_seen, reviewed, created_at,
                 hostname, public_ip, parser_context, proposal_source, proposal_lat, proposal_lon)
                VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    canonical_guess,
                    city or None,
                    country or None,
                    now,
                    now,
                    hostname,
                    str(address),
                    provider,
                    source,
                    lat,
                    lon,
                ),
            )
            candidate_id = cur.lastrowid
            conflict = 0
            prior_source = None
        else:
            candidate_id, prior_country, prior_lat, prior_lon, prior_source = row
            conflict = int(bool(country and prior_country and country != prior_country))
            if lat is not None and prior_lat is not None and prior_lon is not None:
                conflict |= int(great_circle_km((lat, lon), (prior_lat, prior_lon)) > LOCATION_CONFLICT_KM)
        count = conn.execute(
            "SELECT COUNT(*) FROM pop_candidate_observations WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()[0]
        if count >= MAX_OBSERVATIONS_PER_CANDIDATE:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO pop_candidate_observations "
            "(candidate_id, measurement_key, context_key, observed_at) VALUES (?, ?, ?, ?)",
            (candidate_id, measurement_key, context_key, now),
        )
        if not cur.rowcount:
            return False
        independent = conn.execute(
            "SELECT COUNT(DISTINCT context_key) FROM pop_candidate_observations WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()[0]
        conflicts = (
            conn.execute("SELECT conflict_count FROM pop_alias_candidates WHERE id = ?", (candidate_id,)).fetchone()[0]
            + conflict
        )
        quality = 70 if independent >= 2 and conflicts == 0 else 40
        if not (source or prior_source):
            quality = 0
        conn.execute(
            """UPDATE pop_alias_candidates SET seen_count = seen_count + 1, last_seen = ?, hostname = ?, public_ip = ?,
            parser_context = ?, independent_count = ?, conflict_count = ?, quality_score = ?,
            canonical_guess = COALESCE(canonical_guess, ?), city_guess = COALESCE(city_guess, ?),
            country_guess = COALESCE(country_guess, ?),
            proposal_source = COALESCE(proposal_source, ?), proposal_lat = COALESCE(proposal_lat, ?),
            proposal_lon = COALESCE(proposal_lon, ?),
            status = CASE WHEN ? > 0 AND status = 'pending' THEN 'conflict' ELSE status END
            WHERE id = ?""",
            (
                now,
                hostname,
                str(address),
                provider,
                independent,
                conflicts,
                quality,
                canonical_guess,
                city or None,
                country or None,
                source,
                lat,
                lon,
                conflicts,
                candidate_id,
            ),
        )
        return True


def review(
    conn: sqlite3.Connection,
    code: str,
    *,
    action: str,
    actor: str,
    evidence: str,
    canonical: str | None = None,
    city: str | None = None,
    country: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> None:
    """Approve, reject or revoke a candidate without altering historical learned rows."""
    code = code.strip().lower()
    actor, evidence = actor.strip(), evidence.strip()
    if (
        not CANDIDATE_CODE_RE.fullmatch(code)
        or not (1 <= len(actor) <= 80 and 1 <= len(evidence) <= 500)
        or not actor.isprintable()
        or not evidence.isprintable()
    ):
        raise ValueError("Valid code, actor and review evidence are required")
    if action not in {"approve", "reject", "revoke"}:
        raise ValueError("Unsupported review action")
    with sqlite_db.transaction(conn, immediate=True):
        row = conn.execute(
            "SELECT id, status, independent_count, conflict_count, quality_score, approved_location_id, "
            "country_guess, proposal_lat, proposal_lon, canonical_guess "
            "FROM pop_alias_candidates WHERE alias_code = ?",
            (code,),
        ).fetchone()
        if row is None:
            raise ValueError("Candidate not found")
        (
            candidate_id,
            status,
            independent,
            conflicts,
            quality,
            location_id,
            guess_country,
            guess_lat,
            guess_lon,
            guess_code,
        ) = row
        now = utcnow().isoformat()
        if action == "approve":
            if status != "pending" or independent < 2 or conflicts or quality < 70:
                raise ValueError("Candidate needs two independent, non-conflicting observations")
            if not city or not country or lat is None or lon is None:
                raise ValueError("Reviewed city, country and coordinates are required")
            city = city.strip()
            country = country.strip().upper()
            if (
                country != guess_country
                or not re.fullmatch(r"[A-Z]{2}", country)
                or not (1 <= len(city) <= 64 and city.isprintable())
                or not valid_coordinates(lat, lon)
            ):
                raise ValueError("Reviewed location conflicts with the observed country or coordinates")
            if (
                guess_lat is not None
                and guess_lon is not None
                and great_circle_km((lat, lon), (guess_lat, guess_lon)) > LOCATION_CONFLICT_KM
            ):
                raise ValueError("Reviewed coordinates conflict with the observed location")
            if conn.execute("SELECT 1 FROM pop_locations WHERE alias_code = ? AND retired = 0", (code,)).fetchone():
                raise ValueError("An active location already owns this alias")
            chosen_code = (canonical or code).strip().lower()
            if not CANDIDATE_CODE_RE.fullmatch(chosen_code):
                raise ValueError("Reviewed canonical code is invalid")
            if chosen_code != code:
                if chosen_code != guess_code:
                    raise ValueError("Reviewed canonical code must match the documented proposal")
                anchors = conn.execute(
                    "SELECT lat, lon FROM pop_locations WHERE canonical_code = ? AND country = ? "
                    "AND source IN ('manual', 'seed') AND retired = 0",
                    (chosen_code, country),
                ).fetchall()
                if not anchors or any(great_circle_km((lat, lon), (a[0], a[1])) > CURATED_ANCHOR_KM for a in anchors):
                    raise ValueError("Reviewed canonical location lacks a nearby curated anchor")
            cur = conn.execute(
                """INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source,
                   confidence, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'learned', 70, 10, ?, ?)""",
                (chosen_code, code, city, country, lat, lon, now, now),
            )
            location_id = cur.lastrowid
            status = "approved"
        elif action == "revoke":
            if status != "approved" or location_id is None:
                raise ValueError("Only an approved candidate can be revoked")
            cur = conn.execute(
                "UPDATE pop_locations SET retired = 1, updated_at = ? "
                "WHERE id = ? AND source = 'learned' AND retired = 0",
                (now, location_id),
            )
            if cur.rowcount != 1:
                raise ValueError("Approved location was changed; no revocation performed")
            status = "revoked"
        else:
            if status not in {"pending", "conflict"}:
                raise ValueError("Only unresolved candidates can be rejected")
            status = "rejected"
        conn.execute(
            "UPDATE pop_alias_candidates SET status = ?, reviewed = 1, reviewed_at = ?, reviewed_by = ?, "
            "review_evidence = ?, approved_location_id = ? WHERE id = ?",
            (status, now, actor, evidence, location_id, candidate_id),
        )
        conn.execute(
            "INSERT INTO pop_candidate_reviews "
            "(candidate_id, action, actor, evidence, reviewed_at, location_id) VALUES (?, ?, ?, ?, ?, ?)",
            (candidate_id, action, actor, evidence, now, location_id),
        )
