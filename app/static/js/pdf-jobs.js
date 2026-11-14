//
// app/static/js/pdf-jobs.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Global PDF job poller. Jobs live in memory; localStorage only lets a poll survive a page
 * navigation, so a browser without storage access still works in the session. The poller renders
 * nothing: the finished report reaches the user as a server-created notification (Notification
 * Center), and the button that started the job resets on 'pdf-job-completed'. Job ids are stored per
 * user; other users' entries and the old global/ready-list keys are dropped.
 * Known limitation: writes are last-writer-wins across tabs, so a tab can drop a job another tab
 * stored; that job then only reaches the user through its notification.
 * Auto-download: only the tab that started a job (marker in sessionStorage) navigates a hidden
 * same-origin iframe to the server's attachment URL once the job is completed (HEAD check first, then
 * a marker in user-scoped localStorage). Best effort: the marker is not atomic across tabs, and the
 * iframe navigates after an async check, so a browser may block it (iOS Safari is not verified).
 * No Blob/object URL is ever created: iOS Safari would open it in its PDF viewer. A hidden page
 * waits for visibilitychange (kept in sessionStorage over a reload). The notification row stays the fallback.
 */
(function () {
	'use strict';

	var LEGACY_KEYS = ['justup_pdf_jobs', 'justup_pdf_ready']; // global, owner unknown: deleted
	var userStorage = window.JustUpUserStorage;
	var JOB_ID_RE = /^[a-zA-Z0-9_-]{1,64}$/; // same format the server accepts
	var MAX_JOB_AGE_MS = 5 * 60 * 1000; // 5 minutes
	var POLL_INTERVAL_MS = 2000;
	var MAX_ATTEMPTS = 5; // consecutive transient failures before giving up
	var MAX_BACKOFF_MS = 30000;
	// Definitive answers: retrying cannot change them
	var FINAL_STATUSES = [401, 403, 404, 410];

	var AUTO_KEY = 'pdf_auto'; // sessionStorage: jobs this tab started (survives a reload of the tab)
	var PENDING_KEY = 'pdf_auto_pending'; // sessionStorage: finished jobs waiting for the tab to become visible
	var DONE_KEY = 'pdf_auto_done'; // localStorage: jobs whose automatic download was already started
	var MAX_MARKERS = 20;
	var IFRAME_LIFETIME_MS = 60000;
	var jobs = [];
	var inFlightJobs = {};
	var deferredDownloads = [];

	// Storage key of the current user; null (memory only) when the user is unknown
	function storageKey(name) {
		return userStorage ? userStorage.scopedKey(name) : null;
	}
	function readList(name, store) {
		var key = storageKey(name);
		if (!key) return [];
		try {
			var list = JSON.parse((store || localStorage).getItem(key) || '[]');
			return Array.isArray(list) ? list : [];
		} catch (e) { return []; /* storage unavailable or corrupt */ }
	}
	function writeList(name, list, store) {
		var key = storageKey(name);
		if (!key) return false;
		try {
			(store || localStorage).setItem(key, JSON.stringify(list));
			return true;
		} catch (e) { return false; /* storage unavailable: memory only */ }
	}

	// The 'ready' list of earlier versions held file names; nothing writes it any more.
	function dropReadyList() {
		var key = storageKey('pdf_ready');
		if (!key) return;
		try { localStorage.removeItem(key); } catch (e) { /* storage unavailable */ }
	}

	function loadStoredJobs() {
		try {
			var stored = readList('pdf_jobs');
			stored.forEach(function (j) {
				if (j && JOB_ID_RE.test(String(j.jobId)) && !jobs.some(function (known) { return known.jobId === j.jobId; })) {
					jobs.push(j);
				}
			});
		} catch (e) { /* storage unavailable or corrupt: memory only */ }
	}

	function saveJobs() {
		writeList('pdf_jobs', jobs);
	}

	function getPendingJobs() {
		var now = Date.now();
		var expired = jobs.filter(function (j) {
			return (now - (j.addedAt || 0)) >= MAX_JOB_AGE_MS;
		});
		var active = jobs.filter(function (j) {
			return (now - (j.addedAt || 0)) < MAX_JOB_AGE_MS;
		});
		if (active.length !== jobs.length) {
			jobs = active;
			saveJobs();
			expired.forEach(function (job) {
				window.dispatchEvent(new CustomEvent('pdf-job-monitoring-expired', {
					detail: { jobId: job.jobId, targetId: job.targetId }
				}));
			});
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

	function tabStore() {
		try { return window.sessionStorage || null; } catch (e) { return null; /* storage blocked */ }
	}
	function startedHere(jobId) {
		var store = tabStore();
		return !!store && readList(AUTO_KEY, store).indexOf(jobId) !== -1;
	}
	function rememberStartedHere(jobId) {
		var store = tabStore();
		if (store) writeList(AUTO_KEY, readList(AUTO_KEY, store).concat(jobId).slice(-MAX_MARKERS), store);
	}
	// Claims the job; false when its automatic download was already started, the user is unknown
	// or the marker could not be stored (then no auto-download: the notification row remains).
	// Best effort: the read-modify-write is not atomic across tabs, so a rare double download is possible.
	function claimDownload(jobId) {
		if (!storageKey(DONE_KEY)) return false;
		var done = readList(DONE_KEY);
		if (done.indexOf(jobId) !== -1) return false;
		return writeList(DONE_KEY, done.concat(jobId).slice(-MAX_MARKERS));
	}
	// A plain navigation (no blob): the response is Content-Disposition: attachment, so the page
	// stays; an error response only fills the invisible iframe, hence the HEAD check first.
	// The claim comes after the check, so a failed check does not use up the single attempt.
	function startDownload(jobId) {
		var url = '/ui/reports/jobs/' + jobId + '/download';
		return window.fetch(url, { method: 'HEAD', credentials: 'same-origin', cache: 'no-store' }).then(function (resp) {
			if (!resp.ok || resp.redirected) return false; // the notification row stays the fallback
			if (!claimDownload(jobId)) return false;
			var frame = document.createElement('iframe');
			frame.hidden = true;
			frame.setAttribute('aria-hidden', 'true');
			frame.tabIndex = -1;
			frame.src = url;
			document.body.appendChild(frame);
			setTimeout(function () { frame.remove(); }, IFRAME_LIFETIME_MS);
			return true;
		}).catch(function (err) {
			console.warn('PDF auto-download skipped for', jobId, err);
			return false;
		});
	}
	// Deferred ids also live in sessionStorage so a reload of the hidden tab does not lose them.
	function saveDeferred() {
		var store = tabStore();
		if (store) writeList(PENDING_KEY, deferredDownloads, store);
	}
	function autoDownload(jobId) {
		if (!startedHere(jobId)) return; // other tabs and reloads only show the notification
		if (document.visibilityState === 'hidden') {
			if (deferredDownloads.indexOf(jobId) === -1) {
				deferredDownloads.push(jobId);
				saveDeferred();
			}
			return;
		}
		startDownload(jobId);
	}
	function flushDeferred() {
		if (document.visibilityState === 'hidden' || !deferredDownloads.length) return;
		var ids = deferredDownloads.splice(0);
		saveDeferred();
		ids.forEach(startDownload);
	}
	function loadDeferred() {
		var store = tabStore();
		if (!store) return;
		readList(PENDING_KEY, store).forEach(function (jobId) {
			if (JOB_ID_RE.test(String(jobId)) && deferredDownloads.indexOf(jobId) === -1) deferredDownloads.push(jobId);
		});
	}
	function finish(job, success, error) {
		removeJob(job.jobId);
		window.dispatchEvent(new CustomEvent('pdf-job-completed', {
			detail: { jobId: job.jobId, targetId: job.targetId, success: success, error: error }
		}));
	}
	function fail(job, message) {
		finish(job, false, message);
	}
	function onError(job, err) {
		console.error('PDF job error for', job.jobId, err);
		if (err && FINAL_STATUSES.indexOf(err.status) !== -1) {
			fail(job, err.status === 404 || err.status === 410 ? 'PDF report is no longer available' : 'PDF status unavailable');
			return;
		}
		job.attempts = (job.attempts || 0) + 1;
		if (job.attempts >= MAX_ATTEMPTS) {
			fail(job, 'PDF status unavailable: server unavailable');
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
			// The server already stored the result as a notification; the poller only ends the job.
			if (data.status === 'completed') {
				finish(job, true);
				autoDownload(job.jobId);
				return;
			}
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

	// Global API; add() is called from target-detail/pdf-export.js
	window.justUpPdfJobs = {
		add: function (jobId, targetId) {
			if (!JOB_ID_RE.test(String(jobId))) return;
			if (!jobs.some(function (j) { return j.jobId === jobId; })) {
				jobs.push({ jobId: jobId, targetId: targetId, addedAt: Date.now() });
				saveJobs();
				rememberStartedHere(jobId);
			}
		},
		remove: removeJob,
		getPending: getPendingJobs
	};

	if (userStorage) userStorage.dropOtherUsers(LEGACY_KEYS);
	dropReadyList();
	loadStoredJobs();
	loadDeferred();
	flushDeferred();

	document.addEventListener('visibilitychange', flushDeferred);
	setInterval(function () {
		if (document.visibilityState === 'hidden') return;
		pollAllJobs();
	}, POLL_INTERVAL_MS);

	// Also poll immediately on page load
	if (getPendingJobs().length > 0) {
		setTimeout(pollAllJobs, 500);
	}
})();
