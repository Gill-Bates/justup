//
// app/static/js/theme-init.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Theme and Chart initialization - runs before DOM render to prevent flash.
 * Must be loaded in <head> before page content.
 */
(function () {
	'use strict';

	// ─────────────────────────────────────────────────────────────
	// Color Utilities
	// ─────────────────────────────────────────────────────────────

	/**
	 * Add alpha transparency to a color.
	 * @param {string} color - Hex or rgb color
	 * @param {number} alpha - Alpha value (0-1)
	 * @returns {string} rgba color string
	 */
	window.withAlpha = function (color, alpha) {
		if (!color) return color;
		var c = String(color).trim();
		if (c[0] === '#') {
			var hex = c.slice(1);
			if (hex.length === 3) hex = hex.split('').map(function (ch) { return ch + ch; }).join('');
			// Handle 6-digit (#RRGGBB) and 8-digit (#RRGGBBAA) hex
			if (hex.length === 6 || hex.length === 8) {
				var r = parseInt(hex.slice(0, 2), 16);
				var g = parseInt(hex.slice(2, 4), 16);
				var b = parseInt(hex.slice(4, 6), 16);
				// Ignore existing alpha in 8-digit hex; override with requested alpha
				return 'rgba(' + r + ', ' + g + ', ' + b + ', ' + alpha + ')';
			}
			return c;
		}
		var m = c.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([0-9.]+))?\s*\)/i);
		if (m) return 'rgba(' + m[1] + ', ' + m[2] + ', ' + m[3] + ', ' + alpha + ')';
		return c;
	};

	function getCssVar(name, fallback) {
		const val = getComputedStyle(document.documentElement).getPropertyValue(name);
		return val && val.trim() ? val.trim() : fallback;
	}

	// ─────────────────────────────────────────────────────────────
	// Chart.js Theme Defaults
	// ─────────────────────────────────────────────────────────────

	function getChartTheme() {
		return {
			text: getCssVar('--bs-body-color', '#212529'),
			grid: window.withAlpha(getCssVar('--bs-body-color', '#212529'), 0.12)
		};
	}

	window.applyChartDefaults = function () {
		if (!window.Chart) return;
		// Guard: styles may not be computed yet if called too early
		if (!document.body) return;
		var theme = getChartTheme();
		Chart.defaults.font.family = "'Roboto', 'Helvetica Neue', Arial, sans-serif";
		Chart.defaults.color = theme.text;
		Chart.defaults.borderColor = theme.grid;
		Chart.defaults.plugins = Chart.defaults.plugins || {};
		Chart.defaults.plugins.legend = Chart.defaults.plugins.legend || {};
		Chart.defaults.plugins.legend.labels = Chart.defaults.plugins.legend.labels || {};
		Chart.defaults.plugins.legend.labels.color = theme.text;
		Chart.defaults.plugins.legend.labels.font = { size: 12, weight: '600' };
		Chart.defaults.plugins.legend.labels.boxWidth = 12;
		Chart.defaults.plugins.legend.labels.boxHeight = 10;
		Chart.defaults.plugins.legend.labels.padding = 14;

		// Update active charts (defensive for Chart.js v2/v3+ compatibility)
		var instances = Chart.instances || {};
		Object.keys(instances).forEach(function (key) {
			if (instances[key] && typeof instances[key].update === 'function') {
				instances[key].update();
			}
		});
	};

	// Apply chart defaults once after load
	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', window.applyChartDefaults);
	} else {
		window.applyChartDefaults();
	}

	// ─────────────────────────────────────────────────────────────
	// Theme Management
	// ─────────────────────────────────────────────────────────────

	/**
	 * Set the application theme.
	 * @param {string} theme - 'light', 'dark', or 'system'
	 * @param {boolean} persist - Save to localStorage
	 */
	window.setTheme = function (theme, persist) {
		var valid = ['light', 'dark', 'system'].indexOf(theme) !== -1 ? theme : 'system';
		var effective = valid;
		if (valid === 'system') {
			effective = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
		}

		// Skip if theme hasn't effectively changed (performance optimization)
		var current = document.documentElement.getAttribute('data-bs-theme');
		if (current === effective && !persist) return;

		document.documentElement.setAttribute('data-bs-theme', effective);

		// Dispatch theme-changed for map tile updates etc.
		window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme: effective } }));

		if (persist) {
			try {
				localStorage.setItem('userTheme', valid);
			} catch (e) { /* localStorage unavailable (private browsing) */ }
		}

		// Updates that can only happen if DOM is ready
		if (document.body) {
			var icon = document.getElementById('theme-icon');
			var toggleBtn = document.getElementById('theme-toggle');
			if (icon) {
				// light theme -> show moon (to go dark)
				// dark theme -> show sun (to go light)
				icon.textContent = effective === 'dark' ? 'light_mode' : 'dark_mode';
			}
			if (toggleBtn) {
				// Update aria-label for screen readers
				var label = effective === 'dark' 
					? 'Switch to light mode' 
					: 'Switch to dark mode';
				toggleBtn.setAttribute('aria-label', label);
				toggleBtn.setAttribute('title', label);
			}
			// Update Charts - defer to next frame to allow CSS recomputation
			if (window.applyChartDefaults) {
				requestAnimationFrame(function() {
					window.applyChartDefaults();
				});
			}
		}
	};

	// Initial theme load (fast, no flash)
	var saved = 'system';
	try {
		saved = localStorage.getItem('userTheme') || 'system';
	} catch (e) { /* localStorage unavailable (private browsing) */ }
	window.setTheme(saved, false);

	// System preference change listener
	try {
		window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
			var current = 'system';
			try {
				current = localStorage.getItem('userTheme');
			} catch (e) { /* localStorage unavailable */ }
			if (!current || current === 'system') {
				window.setTheme('system', false);
			}
		});
	} catch (e) { /* older browsers */ }
})();
