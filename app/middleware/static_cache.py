#!/usr/bin/env python3
#
# app/middleware/static_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Custom StaticFiles handler with aggressive browser caching."""

from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response


class CachedStaticFiles(StaticFiles):
	"""StaticFiles with aggressive browser caching headers."""

	async def get_response(self, path: str, scope) -> Response:
		response = await super().get_response(path, scope)

		# Only set cache headers for successful responses
		if response.status_code != 200:
			return response

		# Determine cache duration based on file type/location
		cache_max_age = self._get_cache_duration(path)

		if cache_max_age > 0:
			# Set Cache-Control header
			cache_control = f"public, max-age={cache_max_age}"

			# Vendor files are immutable (never change)
			if path.startswith("vendor/"):
				cache_control += ", immutable"

			response.headers["Cache-Control"] = cache_control

			# Add Vary header for proper caching
			response.headers.setdefault("Vary", "Accept-Encoding")

		return response

	def _get_cache_duration(self, path: str) -> int:
		"""Return cache duration in seconds based on file path/type."""

		# Vendor libraries (Bootstrap, Chart.js, etc.) - 1 year
		if path.startswith("vendor/"):
			return 31536000  # 365 days

		# Font files - 1 year (rarely change)
		if any(path.endswith(ext) for ext in [".woff2", ".woff", ".ttf", ".otf", ".eot"]):
			return 31536000  # 365 days

		# Images - 1 month
		if any(path.endswith(ext) for ext in [".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"]):
			return 2592000  # 30 days

		# CSS and JS (own code) - 1 hour (for development flexibility)
		# In production, you'd version these files and use longer cache
		if any(path.endswith(ext) for ext in [".css", ".js"]):
			return 3600  # 1 hour

		# Default: 5 minutes
		return 300
