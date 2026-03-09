#!/usr/bin/env python3
#
# app/services/provider_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Provider info lookup (IP, ASN, ISP, rough location).

Implementation is in the compiled Cython extension.
"""

from app._cython.provider_lookup import lookup_provider_info

__all__ = [
	"lookup_provider_info",
]

