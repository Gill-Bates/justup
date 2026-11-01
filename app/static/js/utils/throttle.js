//
// app/static/js/utils/throttle.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Throttle utility: limits function execution rate.
 * @module utils/throttle
 */

/**
 * Throttle a function to execute at most once per delay period.
 * Useful for performance-sensitive event handlers (resize, scroll, theme changes).
 * Returns a function with a .cancel() method for cleanup.
 * 
 * @param {Function} fn - Function to throttle
 * @param {number} delay - Minimum milliseconds between calls
 * @returns {Function} Throttled function with .cancel() method
 */
function throttle(fn, delay) {
	var lastCall = 0;
	var timeoutId = null;

	function throttled() {
		var context = this;
		var args = arguments;
		var now = Date.now();
		var remaining = delay - (now - lastCall);
		
		if (remaining <= 0) {
			// Clear any pending timeout to prevent stale trailing call
			if (timeoutId) {
				clearTimeout(timeoutId);
				timeoutId = null;
			}
			lastCall = now;
			fn.apply(context, args);
		} else if (!timeoutId) {
			timeoutId = setTimeout(function () {
				lastCall = Date.now();
				timeoutId = null;
				fn.apply(context, args);
			}, remaining);
		}
	}

	// Expose cancel method for cleanup during navigation/component destruction
	throttled.cancel = function () {
		if (timeoutId) {
			clearTimeout(timeoutId);
			timeoutId = null;
		}
		lastCall = 0;
	};

	return throttled;
}

// Export for global access
window.throttle = throttle;
