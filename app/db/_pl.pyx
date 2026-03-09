# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False, cdivision=True
#
# app/db/_pl.pyx
# Copyright (C) 2025-2026 Gill-Bates http://github.com/Gill-Bates
#

# Cython implementation for POP location lookups.
#
# Design:
# - app/db/pop_locations.py is the public facade and owns DB lifecycle/import logic.
# - This module implements only the hot-path lookup functions.
#
# Note: Function names are intentionally short to reduce helpful symbols in the binary.

import logging
import sqlite3

_log = logging.getLogger(__name__)

# Keep these values in sync with app/db/pop_locations.py
CONFIDENCE_HIGH = 100
CONFIDENCE_MEDIUM = 70


def _rf(conn):
	# Ensure sqlite3.Row so callers can access row["col"].
	if conn.row_factory is None:
		conn.row_factory = sqlite3.Row


def _n(code):
	if not code:
		return ""
	return code.lower().strip()


def _cl(confidence):
	if confidence >= CONFIDENCE_HIGH:
		return "high"
	if confidence >= CONFIDENCE_MEDIUM:
		return "medium"
	return "low"


def lkp_t(conn, tokens):
	_rf(conn)
	if not tokens:
		return None

	tokens_norm = [_n(t) for t in tokens if t]
	if not tokens_norm:
		return None

	placeholders = ",".join("?" * len(tokens_norm))

	rows = conn.execute(
		f"""
		SELECT
			canonical_code, alias_code, city, country, lat, lon,
			source, confidence, priority
		FROM pop_locations
		WHERE alias_code IN ({placeholders})
		  AND retired = 0
		ORDER BY priority ASC, confidence DESC
		LIMIT 2
		""",
		tokens_norm,
	).fetchall()

	if not rows:
		return None

	row = rows[0]

	if len(rows) > 1:
		row2 = rows[1]
		if row["priority"] == row2["priority"] and row["confidence"] == row2["confidence"]:
			_log.debug("amb")

	return {
		"canonical_code": row["canonical_code"],
		"alias_code": row["alias_code"],
		"city": row["city"],
		"country": row["country"],
		"lat": row["lat"],
		"lon": row["lon"],
		"source": row["source"],
		"confidence": _cl(row["confidence"]),
		"priority": row["priority"],
	}


def lkp_p(conn, tokens):
	if not tokens:
		return None

	for token in tokens:
		token_norm = _n(token)
		if len(token_norm) >= 3 and token_norm[:3].isalpha():
			prefix = token_norm[:3]
			result = lkp_t(conn, [prefix])
			if result:
				result["matched_token"] = token_norm
				return result

	return None


def lkp_h(conn, hostname):
	_rf(conn)
	if not hostname:
		return None

	hostname_lower = hostname.lower().strip().strip(".")
	hostname_norm = hostname_lower.replace("-", ".").replace("_", ".")
	hostname_pad = f".{hostname_norm}."

	row = conn.execute(
		"""
		SELECT
			canonical_code, alias_code, city, country, lat, lon,
			source, confidence, priority
		FROM pop_locations
		WHERE retired = 0
		  AND (
			  ? LIKE '%.' || alias_code || '.%'
			  OR (
				  alias_code GLOB '*[0-9]*'
				  AND ? LIKE '%.' || alias_code || '%'
			  )
		  )
		ORDER BY
			CASE
				WHEN instr(?, '.' || alias_code || '.') > 0 THEN instr(?, '.' || alias_code || '.')
				ELSE instr(?, '.' || alias_code)
			END ASC,
			length(alias_code) DESC,
			priority ASC,
			confidence DESC
		LIMIT 1
		""",
		(hostname_pad, hostname_pad, hostname_pad, hostname_pad, hostname_pad),
	).fetchone()

	if not row:
		return None

	return {
		"canonical_code": row["canonical_code"],
		"alias_code": row["alias_code"],
		"city": row["city"],
		"country": row["country"],
		"lat": row["lat"],
		"lon": row["lon"],
		"source": row["source"],
		"confidence": _cl(row["confidence"]),
		"priority": row["priority"],
		"match_type": "hostname",
	}
