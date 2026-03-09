//
// app/static/js/target-detail/pdf-export.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * PDF report request and job-status handling.
 * Depends on:
 *   - appState       → appState.range (from target-detail.js)
 *   - apiFetch       → global auth-aware fetch wrapper (from base.html)
 *   - justUpPdfJobs  → global PDF job poller (from base.html)
 *   - justUpToast    → global toast utility (from base.html)
 * @module target-detail/pdf-export
 */

// Reentrancy guard: prevents double-click from spawning parallel requests
let _pdfRequestInFlight = false;

// Store HTMX polling state during PDF generation
let _pausedPollingElements = new Map();

/**
 * Get PDF export button elements (centralized DOM lookup).
 * @private
 * @returns {{btn: HTMLElement|null, icon: HTMLElement|null, spinner: HTMLElement|null}}
 */
function _getPdfElements() {
	return {
		btn: document.getElementById('export-pdf-btn'),
		icon: document.getElementById('pdf-icon'),
		spinner: document.getElementById('pdf-spinner'),
	};
}

/**
 * Pause HTMX polling to reduce server load during PDF generation.
 * Stores original hx-trigger values for later restoration.
 * @private
 */
function _pauseHtmxPolling() {
	_pausedPollingElements.clear();
	const pollingElements = document.querySelectorAll('[hx-trigger*="every"]');
	
	pollingElements.forEach(el => {
		const trigger = el.getAttribute('hx-trigger');
		if (trigger) {
			_pausedPollingElements.set(el, trigger);
			el.removeAttribute('hx-trigger');
			// Re-process element to stop HTMX polling
			if (typeof htmx !== 'undefined' && htmx.process) {
				htmx.process(el);
			}
		}
	});
	
	if (_pausedPollingElements.size > 0) {
		console.info('HTMX polling paused for', _pausedPollingElements.size, 'elements during PDF generation');
	}
}

/**
 * Resume HTMX polling after PDF generation completes.
 * Restores original hx-trigger values.
 * @private
 */
function _resumeHtmxPolling() {
	let resumed = 0;
	
	_pausedPollingElements.forEach((trigger, el) => {
		// Only restore if element is still in DOM
		if (el.isConnected) {
			el.setAttribute('hx-trigger', trigger);
			// Re-process to restart polling
			if (typeof htmx !== 'undefined' && htmx.process) {
				htmx.process(el);
			}
			resumed++;
		}
	});
	
	_pausedPollingElements.clear();
	
	if (resumed > 0) {
		console.info('HTMX polling resumed for', resumed, 'elements');
	}
}

/**
 * Reset the PDF export button to its idle state.
 */
function resetPdfButton() {
	_pdfRequestInFlight = false;
	_resumeHtmxPolling();
	
	const { btn, icon, spinner } = _getPdfElements();

	if (spinner) spinner.classList.add('d-none');
	if (icon) icon.classList.remove('d-none');
	if (btn) btn.disabled = false;
}

/**
 * Set the PDF export button to its loading/busy state.
 * @private
 */
function _setPdfButtonBusy() {
	const { btn, icon, spinner } = _getPdfElements();

	if (icon) icon.classList.add('d-none');
	if (spinner) spinner.classList.remove('d-none');
	if (btn) btn.disabled = true;
}

/**
 * Request async PDF report generation from the backend.
 * Registers the job with the global poller for cross-page tracking.
 *
 * Button remains in busy state until 'pdf-job-completed' event fires
 * or safety timeout (5min) expires.
 *
 * @param {number} targetId - Target ID
 * @param {string} pdfPageSize - Page size ('a4' or 'letter')
 * @returns {Promise<number|undefined>} Safety timeout handle, or undefined if request fails early
 */
async function downloadPdfReport(targetId, pdfPageSize) {
	// Input validation
	if (!targetId || typeof targetId !== 'number') {
		console.error('downloadPdfReport: invalid targetId', targetId);
		return;
	}

	const validSizes = ['a4', 'letter'];
	const size = validSizes.includes(pdfPageSize) ? pdfPageSize : 'a4';

	// Reentrancy guard (prevent double-click)
	if (_pdfRequestInFlight) return;
	_pdfRequestInFlight = true;

	_setPdfButtonBusy();
	_pauseHtmxPolling();

	// Safety timeout: reset button if job completion event never arrives
	const safetyTimeout = setTimeout(() => {
		console.warn('PDF export: safety timeout reached (5min), resetting button');
		resetPdfButton();
	}, 5 * 60 * 1000); // 5 minutes

	try {
		const since = appState.range.from ? appState.range.from.toISOString() : '';
		const until = appState.range.to ? appState.range.to.toISOString() : '';

		const params = new URLSearchParams({ page_size: size });
		if (since) params.set('since', since);
		if (until) params.set('until', until);

		const response = await apiFetch(
			`/reports/targets/${targetId}/pdf/request?${params}`,
			{ method: 'POST' }
		);

		if (!response.ok) {
			let errorMsg = `Server error: ${response.status}`;
			try {
				const errData = await response.json();
				if (typeof errData.detail === 'string') {
					errorMsg = errData.detail;
				} else if (Array.isArray(errData.detail)) {
					errorMsg = errData.detail.map(e => e.msg || JSON.stringify(e)).join(', ');
				}
			} catch {
				// Fallback: plaintext
				const text = await response.text();
				if (text) errorMsg += ` – ${text.substring(0, 200)}`;
			}
			throw new Error(errorMsg);
		}

		const data = await response.json();
		const jobId = data.job_id;

		// Register with global poller (survives navigation)
		window.justUpPdfJobs?.add(jobId, targetId);

		// Warn if poller unavailable (button will reset via safety timeout)
		if (!window.justUpPdfJobs) {
			console.warn('PDF job poller not available, button will reset after timeout');
		}

		window.justUpToast?.success?.('PDF report generation started…');
		return safetyTimeout;

	} catch (err) {
		clearTimeout(safetyTimeout);
		_pdfRequestInFlight = false;
		console.error('PDF request failed:', err);
		window.justUpToast?.error?.('PDF generation failed: ' + err.message);
		resetPdfButton(); // This also resumes HTMX polling
	}
}

/**
 * Listen for job-completed events from the global PDF poller.
 * Resets the button when the completed job matches the current target.
 *
 * @param {number} targetId - Target ID to watch for
 * @returns {{cleanup: Function, setSafetyTimeout: Function}} Object with cleanup function and timeout setter
 */
function initPdfJobListener(targetId) {
	let safetyTimeout = null;

	function onJobCompleted(e) {
		if (Number(e.detail?.targetId) === Number(targetId)) {
			if (safetyTimeout) clearTimeout(safetyTimeout);
			resetPdfButton();
		}
	}

	window.addEventListener('pdf-job-completed', onJobCompleted);

	// Return cleanup object with timeout integration
	return {
		cleanup: () => {
			window.removeEventListener('pdf-job-completed', onJobCompleted);
			if (safetyTimeout) clearTimeout(safetyTimeout);
		},
		setSafetyTimeout: (t) => { safetyTimeout = t; },
	};
}

/**
 * Check if there is already a pending PDF job for this target
 * (e.g. started before navigation) and show the spinner accordingly.
 *
 * @param {number} targetId - Target ID
 */
function checkPendingPdfJob(targetId) {
	if (!targetId) return;

	const pendingJobs = window.justUpPdfJobs?.getPending() || [];
	const tid = Number(targetId);
	const hasPending = pendingJobs.some(j => Number(j.targetId) === tid);

	if (hasPending) {
		_setPdfButtonBusy();
	}
}
