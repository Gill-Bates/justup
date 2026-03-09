//
// app/static/js/utils/with-alpha.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Color utility functions for chart theming.
 * @module utils/with-alpha
 */

/**
 * Read a CSS custom property from :root, with fallback.
 * @param {string} name - CSS variable name (e.g. '--bs-body-color')
 * @param {string} fallback - Fallback value if not found
 * @returns {string}
 */
function getCssVar(name, fallback) {
	const val = getComputedStyle(document.documentElement).getPropertyValue(name);
	return val && val.trim() ? val.trim() : fallback;
}

/**
 * Add an alpha channel to a hex or rgb() color string.
 * @param {string} color - Hex (#rrggbb) or rgb(r, g, b) color
 * @param {number} alpha - Alpha value (0–1)
 * @returns {string} rgba() color string
 */
function withAlpha(color, alpha) {
	if (!color || typeof color !== 'string') return `rgba(0, 0, 0, ${alpha})`;

	// Handle hex colors (#rgb or #rrggbb)
	if (color.startsWith('#')) {
		let hex = color.slice(1);
		if (hex.length === 3) {
			hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
		}
		const r = parseInt(hex.substring(0, 2), 16);
		const g = parseInt(hex.substring(2, 4), 16);
		const b = parseInt(hex.substring(4, 6), 16);
		if (isNaN(r) || isNaN(g) || isNaN(b)) return `rgba(0, 0, 0, ${alpha})`;
		return `rgba(${r}, ${g}, ${b}, ${alpha})`;
	}

	// Handle rgb()/rgba()
	const match = color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
	if (match) {
		return `rgba(${match[1]}, ${match[2]}, ${match[3]}, ${alpha})`;
	}

	return color;
}
