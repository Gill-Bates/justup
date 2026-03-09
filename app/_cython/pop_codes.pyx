# cython: language_level=3
# cython: boundscheck=False, wraparound=False

"""
Cython-optimized POP code lookups with embedded location data.

This module provides:
- Embedded POP location table (obfuscated in compiled .so)
- Fast token extraction and matching
- In-memory fallback when database is unavailable

The embedded data is the single source of truth for POP codes.
"""

from typing import Any, Optional
import re as _re
import ipaddress as _ipaddress

# ---------------------------------------------------------------------------
# Embedded POP location data (obfuscated when compiled)
# Format: alias_code -> (city, country, lat, lon)
# ---------------------------------------------------------------------------

cdef dict _POP_DATA = {
    # Netherlands
    b"ams": ("Amsterdam", "NL", 52.3676, 4.9041),
    b"ams01": ("Amsterdam", "NL", 52.3676, 4.9041),
    b"amsterda": ("Amsterdam", "NL", 52.3676, 4.9041),
    # USA - East
    b"ash": ("Ashburn", "US", 39.0438, -77.4874),
    b"iad": ("Ashburn", "US", 39.0438, -77.4874),
    b"nyc": ("New York", "US", 40.7128, -74.006),
    b"bos": ("Boston", "US", 42.3601, -71.0589),
    b"mia": ("Miami", "US", 25.7617, -80.1918),
    b"phl": ("Philadelphia", "US", 39.9526, -75.1652),
    # USA - Central
    b"chi": ("Chicago", "US", 41.8781, -87.6298),
    b"ord": ("Chicago", "US", 41.8781, -87.6298),
    b"atl": ("Atlanta", "US", 33.749, -84.388),
    b"dfw": ("Dallas", "US", 32.7767, -96.797),
    b"hou": ("Houston", "US", 29.7604, -95.3698),
    b"den": ("Denver", "US", 39.7392, -104.9903),
    # USA - West
    b"lax": ("Los Angeles", "US", 34.0522, -118.2437),
    b"sfo": ("San Francisco", "US", 37.7749, -122.4194),
    b"sjc": ("San Jose", "US", 37.3382, -121.8863),
    b"sea": ("Seattle", "US", 47.6062, -122.3321),
    b"phx": ("Phoenix", "US", 33.4484, -112.074),
    # Germany
    b"ber": ("Berlin", "DE", 52.52, 13.405),
    b"berlin": ("Berlin", "DE", 52.52, 13.405),
    b"fra": ("Frankfurt", "DE", 50.1109, 8.6821),
    b"ffm": ("Frankfurt", "DE", 50.1109, 8.6821),
    b"frf": ("Frankfurt", "DE", 50.1109, 8.6821),
    b"frf1": ("Frankfurt", "DE", 50.1109, 8.6821),
    b"fra04": ("Frankfurt", "DE", 50.1109, 8.6821),
    b"frankfur": ("Frankfurt", "DE", 50.1109, 8.6821),
    b"muc": ("Munich", "DE", 48.1351, 11.582),
    b"mcn": ("Munich", "DE", 48.1351, 11.582),
    b"munich": ("Munich", "DE", 48.1351, 11.582),
    b"muenchen": ("Munich", "DE", 48.1351, 11.582),
    b"ham": ("Hamburg", "DE", 53.5511, 9.9937),
    b"hamburg": ("Hamburg", "DE", 53.5511, 9.9937),
    b"dus": ("Düsseldorf", "DE", 51.2277, 6.7735),
    b"duesseld": ("Düsseldorf", "DE", 51.2277, 6.7735),
    b"cgn": ("Cologne", "DE", 50.9375, 6.9603),
    b"stu": ("Stuttgart", "DE", 48.7758, 9.1829),
    b"han": ("Hannover", "DE", 52.3759, 9.732),
    b"dor": ("Dortmund", "DE", 51.5136, 7.4653),
    b"ess": ("Essen", "DE", 51.4556, 7.0116),
    b"bre": ("Bremen", "DE", 53.0793, 8.8017),
    b"dre": ("Dresden", "DE", 51.0504, 13.7373),
    b"lei": ("Leipzig", "DE", 51.3397, 12.3731),
    b"nue": ("Nuremberg", "DE", 49.4521, 11.0767),
    b"nbg": ("Nuremberg", "DE", 49.4521, 11.0767),
    b"fsn": ("Falkenstein", "DE", 50.4756, 12.365),
    b"wup": ("Wuppertal", "DE", 51.2562, 7.1508),
    b"wup01": ("Wuppertal", "DE", 51.2562, 7.1508),
    # UK
    b"lon": ("London", "GB", 51.5074, -0.1278),
    b"london": ("London", "GB", 51.5074, -0.1278),
    # France
    b"par": ("Paris", "FR", 48.8566, 2.3522),
    b"paris": ("Paris", "FR", 48.8566, 2.3522),
    b"mrs": ("Marseille", "FR", 43.2965, 5.3698),
    # Austria
    b"vie": ("Vienna", "AT", 48.2082, 16.3738),
    b"vienna": ("Vienna", "AT", 48.2082, 16.3738),
    b"wien": ("Vienna", "AT", 48.2082, 16.3738),
    # Switzerland
    b"zrh": ("Zurich", "CH", 47.3769, 8.5417),
    b"zurich": ("Zurich", "CH", 47.3769, 8.5417),
    b"zuerich": ("Zurich", "CH", 47.3769, 8.5417),
    b"gva": ("Geneva", "CH", 46.2044, 6.1432),
    # Other Europe
    b"ath": ("Athens", "GR", 37.9838, 23.7275),
    b"bcn": ("Barcelona", "ES", 41.3851, 2.1734),
    b"mad": ("Madrid", "ES", 40.4168, -3.7038),
    b"bru": ("Brussels", "BE", 50.8503, 4.3517),
    b"bud": ("Budapest", "HU", 47.4979, 19.0402),
    b"cph": ("Copenhagen", "DK", 55.6761, 12.5683),
    b"dub": ("Dublin", "IE", 53.3498, -6.2603),
    b"hel": ("Helsinki", "FI", 60.1699, 24.9384),
    b"ist": ("Istanbul", "TR", 41.0082, 28.9784),
    b"lis": ("Lisbon", "PT", 38.7223, -9.1393),
    b"mil": ("Milan", "IT", 45.4642, 9.19),
    b"rom": ("Rome", "IT", 41.9028, 12.4964),
    b"fco": ("Rome", "IT", 41.9028, 12.4964),
    b"osl": ("Oslo", "NO", 59.9139, 10.7522),
    b"prg": ("Prague", "CZ", 50.0755, 14.4378),
    b"sto": ("Stockholm", "SE", 59.3293, 18.0686),
    b"waw": ("Warsaw", "PL", 52.2297, 21.0122),
    # Asia Pacific
    b"hkg": ("Hong Kong", "HK", 22.3193, 114.1694),
    b"sin": ("Singapore", "SG", 1.3521, 103.8198),
    b"tyo": ("Tokyo", "JP", 35.6895, 139.6917),
    b"nrt": ("Tokyo", "JP", 35.6895, 139.6917),
    b"osa": ("Osaka", "JP", 34.6937, 135.5023),
    b"icn": ("Seoul", "KR", 37.5665, 126.978),
    b"syd": ("Sydney", "AU", -33.8688, 151.2093),
    b"mel": ("Melbourne", "AU", -37.8136, 144.9631),
    b"bom": ("Mumbai", "IN", 19.076, 72.8777),
    b"del": ("Delhi", "IN", 28.7041, 77.1025),
}

# ---------------------------------------------------------------------------
# Noise tokens to filter out during extraction
# ---------------------------------------------------------------------------

cdef frozenset _NOISE_TOKENS = frozenset({
    "ip", "static", "dynamic", "cust", "customer", "client",
    "dsl", "fiber", "ftth", "fttb", "pool", "node", "host",
    "net", "com", "org", "de", "uk", "fr", "nl", "us",
    "ptr", "rev", "rdns", "in", "addr", "arpa",
    "bb", "bb1", "bb2", "bb3", "bb4", "bb5", "bb6", "bb7", "bb8", "bb9",
    "ip6", "ipv6",
    "b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8", "b9",
    "link", "core", "edge", "agg", "pe", "ce", "ae",
})

# Regex patterns
cdef object _ALPHA_PREFIX_PATTERN = _re.compile(r'^([a-z]{2,8})\d')
cdef object _ALPHA_ONLY_PATTERN = _re.compile(r"^([a-z]+)")
cdef object _ROLE_AFTER_POP_PATTERN = _re.compile(r"^([a-z]{2,8})(?:core|edge|agg|bb|gw|rtr|router|sw)\d")


def extract_pop_tokens(str hostname) -> list:
    """
    Extract candidate POP/location tokens from a hostname.
    
    Returns tokens of 2-8 characters sorted by length (longest first).
    """
    if not hostname:
        return []
    
    # Short-circuit raw IP addresses
    cdef str cleaned = hostname.strip().strip("[]")
    try:
        _ipaddress.ip_address(cleaned)
        return []
    except ValueError:
        pass

    # Also catch "ip:port" patterns
    if ":" in cleaned and not cleaned.startswith("["):
        host_part = cleaned.rsplit(":", 1)[0]
        try:
            _ipaddress.ip_address(host_part)
            return []
        except ValueError:
            pass

    cdef str h_lower = hostname.lower()
    if h_lower.endswith(".ip6.arpa") or h_lower.endswith(".in-addr.arpa"):
        return []

    # Normalize: lowercase, replace common separators with dots
    cdef list parts = h_lower.replace("-", ".").replace("_", ".").split(".")
    
    cdef set tokens = set()
    cdef str p, alpha, alpha_prefix, pop, digits
    cdef int digits_start, digits_budget
    cdef object m, m_alpha, mr
    
    for p in parts:
        if not p:
            continue

        # Filter out obvious router-role tokens
        m_alpha = _ALPHA_ONLY_PATTERN.match(p)
        if m_alpha:
            alpha = m_alpha.group(1)
            if alpha in _NOISE_TOKENS:
                continue

        # Direct match: 2-8 chars, not pure digits, not noise
        if 2 <= len(p) <= 8 and not p.isdigit() and p not in _NOISE_TOKENS:
            tokens.add(p)
        # Extract alphabetic prefix from longer tokens
        elif len(p) > 5:
            # Handle compact patterns like "loncore1"
            mr = _ROLE_AFTER_POP_PATTERN.match(p)
            if mr:
                pop = mr.group(1)
                if pop not in _NOISE_TOKENS:
                    tokens.add(pop)

            # Extract alphabetic prefix
            m = _ALPHA_PREFIX_PATTERN.match(p)
            if m:
                alpha_prefix = m.group(1)
                if alpha_prefix not in _NOISE_TOKENS:
                    tokens.add(alpha_prefix)

                    digits_start = len(alpha_prefix)
                    digits_budget = max(0, 5 - digits_start)
                    if digits_budget > 0:
                        digits = ""
                        for ch in p[digits_start:]:
                            if ch.isdigit() and len(digits) < digits_budget:
                                digits += ch
                            else:
                                break
                        if digits:
                            tokens.add(alpha_prefix + digits)
    
    return sorted(tokens, key=lambda t: (-len(t), t))


def lookup_embedded(str hostname) -> Optional[dict]:
    """
    Lookup POP location from embedded data (no database required).
    
    Returns dict with city, country, lat, lon, confidence, source
    or None if not found.
    """
    if not hostname:
        return None
    
    cdef list tokens = extract_pop_tokens(hostname)
    if not tokens:
        return None
    
    cdef bytes token_bytes
    cdef tuple data
    cdef str token
    
    # Try each token (longest first)
    for token in tokens:
        token_bytes = token.encode('ascii')
        data = _POP_DATA.get(token_bytes)
        if data is not None:
            return {
                "city": data[0],
                "country": data[1],
                "lat": data[2],
                "lon": data[3],
                "confidence": 95,
                "source": "embedded",
                "matched_token": token,
                "hostname": hostname,
            }
    
    # Try prefix match (fra3 -> fra)
    for token in tokens:
        if len(token) > 3 and token[len(token)-1].isdigit():
            # Strip trailing digits
            prefix = token.rstrip('0123456789')
            if len(prefix) >= 2:
                token_bytes = prefix.encode('ascii')
                data = _POP_DATA.get(token_bytes)
                if data is not None:
                    return {
                        "city": data[0],
                        "country": data[1],
                        "lat": data[2],
                        "lon": data[3],
                        "confidence": 90,
                        "source": "embedded_prefix",
                        "matched_token": prefix,
                        "hostname": hostname,
                    }
    
    return None


def get_all_codes() -> dict:
    """
    Return all embedded POP codes (for database seeding).
    
    Returns dict of alias_code -> (city, country, lat, lon)
    """
    cdef dict result = {}
    cdef bytes k
    cdef tuple v
    
    for k, v in _POP_DATA.items():
        result[k.decode('ascii')] = v
    
    return result


def has_embedded_data() -> bool:
    """Check if embedded POP data is available."""
    return len(_POP_DATA) > 0


# Export for bridge
__all__ = [
    "extract_pop_tokens",
    "lookup_embedded",
    "get_all_codes",
    "has_embedded_data",
]
