//
// app/static/js/theme-init.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Theme and Chart initialization - runs before DOM render to prevent flash.
 * Must be loaded in <head> before page content, after utils/with-alpha.js
 * (withAlpha, getCssVar).
 */
(function () {
	'use strict';

	// ─────────────────────────────────────────────────────────────
	// Chart.js Theme Defaults
	// ─────────────────────────────────────────────────────────────

	function getChartTheme() {
		return {
			text: getCssVar('--bs-body-color', '#212529'),
			grid: withAlpha(getCssVar('--bs-body-color', '#212529'), 0.12)
		};
	}

	window.applyChartDefaults = function () {
		if (!window.Chart) return;
		// Guard: styles may not be computed yet if called too early
		if (!document.body) return;
		var theme = getChartTheme();
		Chart.defaults.font.family = "'IBM Plex Sans', sans-serif";
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
	 * @param {string} theme - 'system', 'light', or 'dark'
	 */
	window.setTheme = function (theme) {
		var valid = ['light', 'dark', 'system'].indexOf(theme) !== -1 ? theme : 'system';
		var effective = valid;
		if (valid === 'system') {
			effective = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
		}

		var root = document.documentElement;
		var current = root.getAttribute('data-bs-theme');
		var currentPreference = root.getAttribute('data-user-theme');
		var effectiveChanged = current !== effective;
		if (effectiveChanged) root.setAttribute('data-bs-theme', effective);
		root.setAttribute('data-user-theme', valid);

		// Notify charts and maps only when the rendered color scheme changes.
		if (effectiveChanged) {
			window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme: effective } }));
		}

		// Sync controls even if the theme was applied before the body existed.
		if (document.body) {
			var icon = document.getElementById('theme-icon');
			var toggleBtn = document.getElementById('theme-toggle');
			if (icon) {
				// Show the selected mode; automatic mode stays distinguishable in either system theme.
				icon.textContent = valid === 'system' ? 'brightness_auto'
					: effective === 'dark' ? 'light_mode' : 'dark_mode';
			}
			if (toggleBtn) {
				// The button cycles automatic -> light -> dark -> automatic.
				var mode = valid === 'system' ? 'automatic' : valid === 'light' ? 'light' : 'dark';
				var nextMode = valid === 'system' ? 'light' : valid === 'light' ? 'dark' : 'automatic';
				var label = 'Color mode: ' + mode + '. Switch to ' + nextMode + ' mode.';
				toggleBtn.setAttribute('aria-label', label);
				toggleBtn.setAttribute('title', label);
			}
		}

		if (!effectiveChanged && currentPreference === valid) return;
		// Update Charts - defer to next frame to allow CSS recomputation
		if (document.body && window.applyChartDefaults) {
			requestAnimationFrame(function() {
				window.applyChartDefaults();
			});
		}
	};

	// Restore the desktop sidebar rail state before first paint to avoid a layout jump.
	try {
		if (localStorage.getItem('ju-sidebar') === 'collapsed') document.documentElement.classList.add('ju-nav-sm');
	} catch (e) { /* storage blocked */ }

	// Apply the server-rendered user preference before the page content paints.
	var initialTheme = document.documentElement.getAttribute('data-user-theme') || 'system';
	window.setTheme(initialTheme);

	// System preference change listener
	try {
		window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
			if (document.documentElement.getAttribute('data-user-theme') === 'system') {
				window.setTheme('system');
			}
		});
	} catch (e) { /* older browsers */ }
})();
