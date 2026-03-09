//
// app/static/js/charts/chart-theme.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Chart theme management: dynamic color updates on theme switching.
 * Depends on:
 *   - utils/throttle.js → throttle()
 *   - utils/with-alpha.js → getCssVar(), withAlpha()
 *   - appState.theme, appState.charts (from target-detail.js)
 * @module charts/chart-theme
 */

/**
 * Read current theme colors from CSS custom properties.
 * @returns {object} { text, muted, grid, bg, mode }
 */
function getChartTheme() {
	const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
	// Use --bs-border-color for grid to avoid alpha issues with --bs-body-color
	const gridColor = getCssVar('--bs-border-color', null);
	return {
		text: getCssVar('--bs-body-color', '#212529'),
		muted: getCssVar('--bs-secondary-color', '#6c757d'),
		// Prefer border-color for grid, fallback to body-color with alpha
		grid: gridColor || withAlpha(getCssVar('--bs-body-color', '#212529'), 0.12),
		bg: getCssVar('--bs-body-bg', '#ffffff'),
		mode: isDark ? 'dark' : 'light',
	};
}

/**
 * Apply current Bootstrap theme to all charts and Chart.js defaults.
 * Called when user switches between light/dark mode.
 */
function applyChartTheme() {
	const theme = getChartTheme();

	// Update global appState.theme if available
	if (typeof appState !== 'undefined') {
		appState.theme = theme;
	}

	if (!window.Chart) return;

	// ── Update Chart.js global defaults ──
	Chart.defaults.color = theme.text;
	Chart.defaults.borderColor = theme.grid;
	Chart.defaults.font.family = "'Roboto', 'Helvetica Neue', Arial, sans-serif";

	// Ensure default paths exist before setting
	var defPlugins = Chart.defaults.plugins;
	if (defPlugins) {
		if (defPlugins.legend) {
			if (!defPlugins.legend.labels) defPlugins.legend.labels = {};
			defPlugins.legend.labels.color = theme.text;
		}
		if (defPlugins.tooltip) {
			defPlugins.tooltip.titleColor = theme.text;
			defPlugins.tooltip.bodyColor = theme.text;
			defPlugins.tooltip.backgroundColor = theme.bg;
			defPlugins.tooltip.borderColor = theme.grid;
		}
	}

	if (typeof appState === 'undefined' || !appState.charts) return;

	// ── Update each chart instance ──
	appState.charts.forEach(function (ch) {
		if (!ch || !ch.options) return;

		// — Scales —
		['x', 'y'].forEach(function (axis) {
			var scale = ch.options.scales && ch.options.scales[axis];
			if (!scale) return;
			if (scale.ticks) scale.ticks.color = theme.muted;
			if (scale.grid) scale.grid.color = theme.grid;
		});

		// — Legend (ensure path exists, don't skip via optional chaining) —
		if (!ch.options.plugins) ch.options.plugins = {};
		if (!ch.options.plugins.legend) ch.options.plugins.legend = {};
		if (!ch.options.plugins.legend.labels) ch.options.plugins.legend.labels = {};
		ch.options.plugins.legend.labels.color = theme.text;

		// Also update the resolved legend-plugin options directly (belt & suspenders)
		if (ch.legend && ch.legend.options) {
			if (!ch.legend.options.labels) ch.legend.options.labels = {};
			ch.legend.options.labels.color = theme.text;
		}

		// — Tooltip —
		if (!ch.options.plugins.tooltip) ch.options.plugins.tooltip = {};
		ch.options.plugins.tooltip.titleColor = theme.text;
		ch.options.plugins.tooltip.bodyColor = theme.text;
		ch.options.plugins.tooltip.backgroundColor = theme.bg;
		ch.options.plugins.tooltip.borderColor = theme.grid;

		// — SLA annotation (label + line) —
		var annotations = ch.options.plugins.annotation &&
						  ch.options.plugins.annotation.annotations;
		if (annotations && annotations.slaLine) {
			// Update label text color
			if (annotations.slaLine.label) {
				annotations.slaLine.label.color = theme.text;
			}
			// Update line color for theme consistency
			if (annotations.slaLine.borderColor !== undefined) {
				annotations.slaLine.borderColor = theme.muted;
			}
		}

		// — Update dataset colors for theme-aware charts (TCP port colors) —
		if (ch.data && ch.data.datasets) {
			ch.data.datasets.forEach(function(dataset) {
				// Check if dataset has theme-aware color metadata
				if (dataset._tcpPortHue !== undefined) {
					// Recalculate TCP port color with theme-appropriate lightness
					var lightness = theme.mode === 'dark' ? 65 : 45;
					var newColor = 'hsl(' + Math.round(dataset._tcpPortHue) + ', 70%, ' + lightness + '%)';
					dataset.borderColor = newColor;
					dataset.backgroundColor = withAlpha(newColor, 0.1);
				}
			});
		}

		// Mark chart as undergoing theme update to prevent sampling recalculation
		// Theme changes colors/styles but not data density or chart dimensions
		ch._isThemeUpdating = true;

		// Update without animation. This triggers legend rebuild (afterUpdate) with fresh colors.
		ch.update('none');

		// Clear flag after update completes
		setTimeout(function() {
			if (ch) ch._isThemeUpdating = false;
		}, 0);
	});
}

/**
 * Initialize a MutationObserver that watches for Bootstrap theme changes.
 * Monitors data-bs-theme attribute on <html> and re-themes all charts.
 * Stores the observer on appState.themeObserver for cleanup.
 * 
 * @param {number} [throttleMs=200] - Throttle delay in milliseconds
 * @returns {MutationObserver} The created observer (also stored on appState)
 */
function initChartThemeObserver(throttleMs) {
	const delay = typeof throttleMs === 'number' ? throttleMs : 200;
	
	// Defer theme application to next frame to allow CSS recomputation after attribute change
	const deferredApply = function() {
		requestAnimationFrame(applyChartTheme);
	};
	const throttledApply = throttle(deferredApply, delay);

	const observer = new MutationObserver(throttledApply);
	observer.observe(document.documentElement, {
		attributes: true,
		attributeFilter: ['data-bs-theme'],
	});

	// Also listen to the explicit theme event dispatched by theme-init.js.
	// This makes theme updates reliable even if MutationObserver timing differs across browsers.
	const themeChangedListener = function() {
		throttledApply();
	};
	window.addEventListener('theme-changed', themeChangedListener);

	if (typeof appState !== 'undefined') {
		appState.themeObserver = observer;
		appState._themeChangedListener = themeChangedListener;
	}

	return observer;
}

/**
 * Cleanup theme observer and disconnect it properly.
 * Call this when navigating away from pages with charts to prevent memory leaks.
 */
function cleanupChartThemeObserver() {
	if (typeof appState !== 'undefined' && appState.themeObserver) {
		appState.themeObserver.disconnect();
		appState.themeObserver = null;
	}
	if (typeof appState !== 'undefined' && appState._themeChangedListener) {
		window.removeEventListener('theme-changed', appState._themeChangedListener);
		appState._themeChangedListener = null;
	}
}

// Automatic cleanup on page unload (SPA navigation safety)
window.addEventListener('pagehide', cleanupChartThemeObserver);
