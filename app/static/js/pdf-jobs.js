//
// app/static/js/pdf-jobs.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Global PDF job poller. Jobs live in memory; localStorage only lets a poll survive a
 * page navigation, so a browser without storage access still downloads in the session.
 */
(function () {
	'use strict';

	var PDF_JOBS_KEY = 'justup_pdf_jobs'; // JSON array of {jobId, targetId, addedAt}
	var MAX_JOB_AGE_MS = 5 * 60 * 1000; // 5 minutes
	var POLL_INTERVAL_MS = 2000;
	var MAX_ATTEMPTS = 5; // consecutive transient failures before giving up
	var MAX_BACKOFF_MS = 30000;
	// Definitive answers: retrying cannot change them
	var FINAL_STATUSES = [401, 403, 404, 410];

	var jobs = [];
	var inFlightJobs = {};

	function loadStoredJobs() {
		try {
			var stored = JSON.parse(localStorage.getItem(PDF_JOBS_KEY) || '[]');
			stored.forEach(function (j) {
				if (j && j.jobId && !jobs.some(function (known) { return known.jobId === j.jobId; })) {
					jobs.push(j);
				}
			});
		} catch (e) { /* storage unavailable or corrupt: memory only */ }
	}

	function saveJobs() {
		try {
			localStorage.setItem(PDF_JOBS_KEY, JSON.stringify(jobs));
		} catch (e) { /* storage unavailable: memory only */ }
	}

	function getPendingJobs() {
		var now = Date.now();
		var active = jobs.filter(function (j) {
			return (now - (j.addedAt || 0)) < MAX_JOB_AGE_MS;
		});
		if (active.length !== jobs.length) {
			jobs = active;
			saveJobs();
		}
		return active.slice();
	}

	function removeJob(jobId) {
		jobs = jobs.filter(function (j) { return j.jobId !== jobId; });
		saveJobs();
	}

	function httpError(status) {
		var err = new Error('HTTP ' + status);
		err.status = status;
		return err;
	}

	function finish(job, success, error) {
		removeJob(job.jobId);
		window.dispatchEvent(new CustomEvent('pdf-job-completed', {
			detail: { jobId: job.jobId, targetId: job.targetId, success: success, error: error }
		}));
	}

	function fail(job, message) {
		if (window.justUpToast) window.justUpToast.error(message);
		finish(job, false, message);
	}

	// Navigating to the attachment URL (session cookie, no token in the URL) keeps the server's
	// Content-Disposition, so mobile browsers download instead of opening a viewer (unlike a blob URL).
	function startDownload(url, filename) {
		// HEAD first: a 404/410 must surface as a message, not replace the app page
		return window.apiFetch(url, { method: 'HEAD' }).then(function (resp) {
			if (!resp.ok) throw httpError(resp.status);
			var a = document.createElement('a');
			a.href = url;
			if (filename) a.download = filename;
			a.style.display = 'none';
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
		});
	}

	function download(job, data) {
		var url = data.download_url || ('/ui/reports/jobs/' + job.jobId + '/download');
		return startDownload(url, data.filename || ('report_' + (job.targetId || 'target') + '.pdf')).then(function () {
			// The job leaves the list only after the browser was handed the file
			if (window.justUpToast) window.justUpToast.success('PDF report download started');
			finish(job, true);
		});
	}

	function onError(job, err) {
		console.error('PDF job error for', job.jobId, err);
		if (err && FINAL_STATUSES.indexOf(err.status) !== -1) {
			fail(job, err.status === 404 || err.status === 410 ? 'PDF report is no longer available' : 'PDF download failed');
			return;
		}
		job.attempts = (job.attempts || 0) + 1;
		if (job.attempts >= MAX_ATTEMPTS) {
			fail(job, 'PDF download failed: server unavailable');
			return;
		}
		job.nextAt = Date.now() + Math.min(POLL_INTERVAL_MS * Math.pow(2, job.attempts), MAX_BACKOFF_MS);
	}

	function pollJob(job) {
		return window.apiFetch('/reports/jobs/' + job.jobId).then(function (resp) {
			if (!resp.ok) throw httpError(resp.status);
			return resp.json();
		}).then(function (data) {
			job.attempts = 0;
			job.nextAt = 0;
			if (data.status === 'completed') return download(job, data);
			if (data.status === 'failed') {
				fail(job, 'PDF generation failed: ' + (data.error || 'Unknown error'));
			}
			// else: still pending/processing, continue polling
		}).catch(function (err) {
			onError(job, err);
		});
	}

	function pollAllJobs() {
		var now = Date.now();
		getPendingJobs().forEach(function (job) {
			if (inFlightJobs[job.jobId] || (job.nextAt || 0) > now) return;
			inFlightJobs[job.jobId] = true;
			pollJob(job).finally(function () {
				delete inFlightJobs[job.jobId];
			});
		});
	}

	// Global API to add a job (called from target_detail.html)
	window.justUpPdfJobs = {
		add: function (jobId, targetId) {
			if (!jobs.some(function (j) { return j.jobId === jobId; })) {
				jobs.push({ jobId: jobId, targetId: targetId, addedAt: Date.now() });
				saveJobs();
			}
		},
		remove: removeJob,
		getPending: getPendingJobs,
		startDownload: startDownload
	};

	loadStoredJobs();

	setInterval(function () {
		if (document.visibilityState === 'hidden') return;
		pollAllJobs();
	}, POLL_INTERVAL_MS);

	// Also poll immediately on page load
	if (getPendingJobs().length > 0) {
		setTimeout(pollAllJobs, 500);
	}
})();
