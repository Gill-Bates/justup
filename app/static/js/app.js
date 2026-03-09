//
// app/static/js/app.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * justUp! Main Application JavaScript
 * Handles HTMX configuration, toast notifications, offline detection,
 * theme persistence, PDF job polling, and fragment initialization.
 */
(function () {
	'use strict';

	// ─────────────────────────────────────────────────────────────────────────
	// Cookie Helper
	// ─────────────────────────────────────────────────────────────────────────

	function getCookie(name) {
		var value = '; ' + document.cookie;
		var parts = value.split('; ' + name + '=');
		if (parts.length === 2) {
			var raw = parts.pop().split(';').shift();
			try { return decodeURIComponent(raw); } catch (e) { return raw; }
		}
		return '';
	}

	// ─────────────────────────────────────────────────────────────────────────
	// Global Fetch Helper (available immediately)
	// ─────────────────────────────────────────────────────────────────────────

	/**
	 * Fetch wrapper with automatic Authorization and CSRF headers.
	 * Use this instead of raw fetch() in templates to avoid auth/CSRF drift.
	 */
	window.apiFetch = function apiFetch(url, options) {
		var opts = options ? Object.assign({}, options) : {};
		var headers = new Headers(opts.headers || {});

		try {
			var token = localStorage.getItem('token');
			if (token && !headers.has('Authorization')) {
				headers.set('Authorization', 'Bearer ' + token);
			}
		} catch (e) { /* localStorage unavailable */ }

		var csrf = getCookie('csrf_token');
		if (csrf && !headers.has('X-CSRF-Token')) {
			headers.set('X-CSRF-Token', csrf);
		}

		// Default: include same-origin cookies (if any)
		if (!opts.credentials) {
			opts.credentials = 'same-origin';
		}

		opts.headers = headers;
		return fetch(url, opts);
	};

	// ─────────────────────────────────────────────────────────────────────────
	// Main Initialization (requires document.body)
	// ─────────────────────────────────────────────────────────────────────────

	function init() {
		// Ensure we have document.body before proceeding
		if (!document.body) {
			console.warn('app.js init called before document.body exists');
			return;
		}

		// ─────────────────────────────────────────────────────────────────────────
		// HTMX Configuration
		// ─────────────────────────────────────────────────────────────────────────

		// Disable htmx localStorage (prevents Safari Tracking Prevention warnings)
		if (window.htmx) {
			htmx.config.historyCacheSize = 0;
			// CSP hardening: do not execute script tags or eval
			htmx.config.allowEval = false;
			htmx.config.allowScriptTags = false;
		}

		// Post-success behaviors for HTMX requests (replaces hx-on::after-request inline JS)
		document.body.addEventListener('htmx:afterRequest', function (evt) {
			if (!evt || !evt.detail || !evt.detail.elt || !evt.detail.successful) return;
			var elt = evt.detail.elt;

			// Logout: clear local token and redirect
			if (elt.id === 'logout-btn') {
				try { localStorage.removeItem('token'); } catch (e) { /* localStorage unavailable */ }
				var logoutRedirect = elt.getAttribute('data-success-redirect') || '/ui/login';
				window.location.href = logoutRedirect;
				return;
			}

			var toastMsg = elt.getAttribute('data-success-toast');
			if (toastMsg && window.justUpToast && window.justUpToast.success) {
				window.justUpToast.success(toastMsg);
			}

			if (elt.getAttribute('data-success-reload') === '1') {
				window.location.reload();
				return;
			}

			var triggerSel = elt.getAttribute('data-success-trigger');
			var triggerEvent = elt.getAttribute('data-success-event');
			if (triggerSel && triggerEvent && window.htmx && typeof htmx.trigger === 'function') {
				htmx.trigger(triggerSel, triggerEvent);
			}

			var redirectUrl = elt.getAttribute('data-success-redirect');
			if (redirectUrl) {
				var delayMs = parseInt(elt.getAttribute('data-success-delay') || '0', 10);
				if (!isNaN(delayMs) && delayMs > 0) {
					setTimeout(function () { window.location.href = redirectUrl; }, delayMs);
				} else {
					window.location.href = redirectUrl;
				}
			}
		});

		// htmx:afterSettle behaviors (replaces hx-on::after-settle inline JS which requires eval)
		document.body.addEventListener('htmx:afterSettle', function (evt) {
			if (!evt || !evt.detail || !evt.detail.elt) return;
			var elt = evt.detail.elt;

			// Call init function if specified via data-settle-init
			var initFn = elt.getAttribute('data-settle-init');
			if (initFn && typeof window[initFn] === 'function') {
				window[initFn]();
			}
		});

		// Global HTMX Configuration (Authorization + CSRF)
		document.body.addEventListener('htmx:configRequest', function (evt) {
			// 1. Authorization Bearer Token (with Safari Tracking Prevention fallback)
			try {
				var token = localStorage.getItem('token');
				if (token) {
					evt.detail.headers['Authorization'] = 'Bearer ' + token;
				}
			} catch (e) {
				// localStorage blocked by Tracking Prevention - user will be redirected to login
			}

			// 2. CSRF Token
			var csrfToken = getCookie('csrf_token');
			if (csrfToken) {
				evt.detail.headers['X-CSRF-Token'] = csrfToken;
			}
		});

		// ─────────────────────────────────────────────────────────────────────────
		// Toast Notifications
		// ─────────────────────────────────────────────────────────────────────────

		var vanillaToastCount = 0;

		function showVanillaToast(message, variant) {
			var el = document.createElement('div');
			var offset = 1 + (vanillaToastCount * 4.5); // rem units
			vanillaToastCount++;
			el.setAttribute('role', variant === 'danger' ? 'alert' : 'status');
			el.setAttribute('aria-live', variant === 'danger' ? 'assertive' : 'polite');
			el.style.cssText = 'position:fixed;top:' + offset + 'rem;right:1rem;z-index:3000;max-width:420px;' +
				'padding:0.75rem 1rem;border-radius:0.5rem;box-shadow:0 12px 30px rgba(0,0,0,.25);' +
				'backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);' +
				'border:1px solid rgba(255,255,255,.2);background:rgba(33,37,41,.92);color:#fff;';
			if (variant === 'danger') el.style.background = 'rgba(220,53,69,.92)';
			if (variant === 'warning') el.style.background = 'rgba(255,193,7,.92)';
			if (variant === 'success' || variant === 'info') el.style.background = 'rgba(25,135,84,.92)';
			el.textContent = String(message);
			document.body.appendChild(el);
			setTimeout(function () {
				el.style.transition = 'opacity .25s ease';
				el.style.opacity = '0';
				setTimeout(function () {
					el.remove();
					vanillaToastCount = Math.max(0, vanillaToastCount - 1);
				}, 300);
			}, 3500);
		}

		function showToast(message, variant, iconName) {
			var toastEl = document.getElementById('global-toast');
			var bodyEl = document.getElementById('global-toast-body');
			if (!toastEl || !bodyEl || !window.bootstrap) {
				console.warn('Toast fallback:', message);
				showVanillaToast(message, variant || 'info');
				return;
			}

			// Normalize variant: info -> success (no cyan toasts)
			var v = (variant === 'info') ? 'success' : (variant || 'success');
			toastEl.classList.remove('text-bg-success', 'text-bg-danger', 'text-bg-warning');
			toastEl.classList.add('text-bg-' + v);

			// Prevent HTML injection: build DOM nodes, set textContent
			bodyEl.replaceChildren();
			if (iconName) {
				var icon = document.createElement('span');
				icon.className = 'material-icons me-2';
				icon.style.fontSize = '20px';
				icon.style.verticalAlign = 'middle';
				icon.textContent = iconName;
				bodyEl.appendChild(icon);
			}
			var span = document.createElement('span');
			span.textContent = String(message);
			bodyEl.appendChild(span);

			var toast = bootstrap.Toast.getOrCreateInstance(toastEl, { delay: 4000 });
			toast.show();
		}

		window.justUpToast = {
			success: function (msg) { showToast(msg, 'success', 'check_circle'); },
			error: function (msg) { showToast(msg, 'danger', 'error'); },
			info: function (msg) { showToast(msg, 'success', 'check_circle'); },  // info -> success (green)
			warning: function (msg) { showToast(msg, 'warning', 'warning'); }
		};

		// Check for toast message in URL params (e.g. after redirect)
		(function () {
			var urlParams = new URLSearchParams(window.location.search);
			var toastMsg = urlParams.get('toast');
			if (toastMsg) {
				// Limit message length to prevent layout issues
				var safMsg = toastMsg.length > 200 ? toastMsg.substring(0, 200) + '…' : toastMsg;
				// Show toast after short delay to ensure DOM is ready
				setTimeout(function () { showToast(safMsg, 'success', 'check_circle'); }, 100);
				// Clean URL (remove toast param)
				urlParams.delete('toast');
				var newUrl = urlParams.toString()
					? window.location.pathname + '?' + urlParams.toString() + window.location.hash
					: window.location.pathname + window.location.hash;
				window.history.replaceState({}, '', newUrl);
			}
		})();

		// ─────────────────────────────────────────────────────────────────────────
		// Server Offline Detection
		// ─────────────────────────────────────────────────────────────────────────

		(function () {
			var overlayHint = document.getElementById('server-offline-hint');
			var isOffline = false;
			var consecutiveFailures = 0;
			var retryTimer = null;
			var retryDelayMs = 1500;
			var maxDelayMs = 10000;

			function setOffline(on) {
				if (on === isOffline) return;
				isOffline = on;
				document.body.classList.toggle('server-offline', on);
				if (on) {
					startRetryLoop();
				} else {
					stopRetryLoop();
				}
			}

			function stopRetryLoop() {
				if (retryTimer) {
					clearTimeout(retryTimer);
					retryTimer = null;
				}
				retryDelayMs = 1500;
			}

			function updateHint(ms) {
				if (!overlayHint) return;
				var sec = Math.max(1, Math.round(ms / 1000));
				overlayHint.textContent = 'Next retry in ~' + sec + 's';
			}

			function ping() {
				// Use a dedicated endpoint so auth issues don't look like "offline"
				var controller = new AbortController();
				var timeoutId = setTimeout(function () { controller.abort(); }, 5000);

				return fetch('/health', {
					cache: 'no-store',
					signal: controller.signal
				}).then(function (resp) {
					clearTimeout(timeoutId);
					if (!resp.ok) throw new Error('Health check failed');
					return true;
				}).catch(function (err) {
					clearTimeout(timeoutId);
					throw err;
				});
			}

			function retryOnce() {
				ping().then(function () {
					// Server is back -> reload to fully recover state
					window.location.reload();
				}).catch(function () {
					retryDelayMs = Math.min(maxDelayMs, Math.floor(retryDelayMs * 1.4));
					scheduleRetry();
				});
			}

			function scheduleRetry() {
				updateHint(retryDelayMs);
				retryTimer = setTimeout(retryOnce, retryDelayMs);
			}

			function startRetryLoop() {
				if (retryTimer) return;
				scheduleRetry();
			}

			// Periodic background ping while online (detect server going away)
			setInterval(function () {
				if (isOffline) return;
				if (document.visibilityState === 'hidden') return;
				ping().then(function () {
					consecutiveFailures = 0;
				}).catch(function () {
					consecutiveFailures += 1;
					if (consecutiveFailures >= 3) {
						setOffline(true);
					}
				});
			}, 5000);

			// Browser network offline/online hints
			window.addEventListener('offline', function () { setOffline(true); });
			window.addEventListener('online', function () {
				// Only reload once server actually responds
				consecutiveFailures = 0;
				if (isOffline) startRetryLoop();
			});

			// HTMX network errors (XHR status 0)
			document.body.addEventListener('htmx:sendError', function () {
				consecutiveFailures += 1;
				if (consecutiveFailures >= 3) {
					setOffline(true);
				}
			});
			document.body.addEventListener('htmx:responseError', function (evt) {
				var status = evt && evt.detail && evt.detail.xhr && evt.detail.xhr.status;
				if (status === 0) {
					consecutiveFailures += 1;
					if (consecutiveFailures >= 3) {
						setOffline(true);
					}
				}
			});

			// Reset counter on successful HTMX responses
			document.body.addEventListener('htmx:afterRequest', function (evt) {
				var xhr = evt.detail.xhr;
				if (xhr && xhr.status > 0) {
					consecutiveFailures = 0;
				}
			});
		})();

		// ─────────────────────────────────────────────────────────────────────────
		// Dark Mode Toggle with DB Persistence
		// ─────────────────────────────────────────────────────────────────────────

		(function () {
			var toggle = document.getElementById('theme-toggle');

			// Initial Icon sync (in case script ran before DOM)
			if (toggle && window.setTheme) {
				try {
					window.setTheme(localStorage.getItem('userTheme') || 'system', false);
				} catch (e) { /* localStorage unavailable */ }
			}

			function saveThemeToDb(theme) {
				// Only persist if user is authenticated
				var hasToken = false;
				try { hasToken = !!localStorage.getItem('token'); } catch (e) { /* */ }
				if (!hasToken) return;

				window.apiFetch('/me/theme', {
					method: 'PATCH',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ theme: theme })
				}).catch(function (e) {
					console.warn('Failed to save theme to DB:', e);
				});
			}

			if (toggle) {
				toggle.addEventListener('click', function () {
					var current = document.documentElement.getAttribute('data-bs-theme');
					var next = current === 'dark' ? 'light' : 'dark';
					if (window.setTheme) window.setTheme(next, true);
					saveThemeToDb(next);
				});
			}

			// Load theme from DB on page load
			function loadThemeFromDb() {
				// Skip if user already has a local preference (prevents theme flash)
				try {
					if (localStorage.getItem('userTheme')) return;
				} catch (e) { return; }

				window.apiFetch('/me/theme').then(function (resp) {
					if (resp.ok) {
						return resp.json();
					}
					throw new Error('Not OK');
				}).then(function (data) {
					if (data.theme && ['light', 'dark'].indexOf(data.theme) !== -1) {
						// Update if different from cache
						try {
							if (data.theme !== localStorage.getItem('userTheme')) {
								if (window.setTheme) window.setTheme(data.theme, true);
							}
						} catch (e) { /* localStorage unavailable */ }
					}
				}).catch(function (e) {
					console.warn('Failed to load theme from DB:', e);
				});
			}

			// Load from DB (async)
			loadThemeFromDb();
		})();

		// ─────────────────────────────────────────────────────────────────────────
		// PDF Job Poller
		// ─────────────────────────────────────────────────────────────────────────

		(function () {
			var PDF_JOBS_KEY = 'justup_pdf_jobs'; // JSON array of {jobId, targetId, filename?}
			var MAX_JOB_AGE_MS = 5 * 60 * 1000; // 5 minutes

			function getPendingJobs() {
				try {
					var jobs = JSON.parse(localStorage.getItem(PDF_JOBS_KEY) || '[]');
					var now = Date.now();
					var active = jobs.filter(function (j) {
						return (now - (j.addedAt || 0)) < MAX_JOB_AGE_MS;
					});
					if (active.length !== jobs.length) {
						savePendingJobs(active);
					}
					return active;
				} catch (e) {
					return [];
				}
			}

			function savePendingJobs(jobs) {
				try {
					localStorage.setItem(PDF_JOBS_KEY, JSON.stringify(jobs));
				} catch (e) { /* localStorage unavailable */ }
			}

			function removeJob(jobId) {
				var jobs = getPendingJobs().filter(function (j) { return j.jobId !== jobId; });
				savePendingJobs(jobs);
			}

			function downloadPdf(jobId, filename, targetId) {
				return window.apiFetch('/reports/jobs/' + jobId + '/download').then(function (response) {
					if (!response.ok) {
						throw new Error('Download failed');
					}
					return response.blob();
				}).then(function (blob) {
					var downloadUrl = URL.createObjectURL(blob);
					var a = document.createElement('a');
					a.href = downloadUrl;
					a.download = filename || ('report_' + (targetId || 'target') + '.pdf');
					document.body.appendChild(a);
					a.click();
					document.body.removeChild(a);
					// Defer URL.revokeObjectURL to allow browser to complete download
					setTimeout(function () {
						URL.revokeObjectURL(downloadUrl);
					}, 10000);
					return true;
				}).catch(function (err) {
					console.error('PDF download failed:', err);
					return false;
				});
			}

			var inFlightJobs = {};

			function pollJob(job) {
				return window.apiFetch('/reports/jobs/' + job.jobId).then(function (resp) {
					if (!resp.ok) {
						// Job not found - remove it
						removeJob(job.jobId);
						return null;
					}
					return resp.json();
				}).then(function (data) {
					if (!data) return;

					if (data.status === 'completed') {
						removeJob(job.jobId);
						downloadPdf(job.jobId, data.filename, job.targetId).then(function (success) {
							if (success) {
								if (window.justUpToast) window.justUpToast.success('PDF report downloaded!');
							} else {
								if (window.justUpToast) window.justUpToast.error('PDF download failed');
							}
							// Notify target_detail page if open (for button reset)
							window.dispatchEvent(new CustomEvent('pdf-job-completed', {
								detail: { jobId: job.jobId, targetId: job.targetId, success: success }
							}));
						});

					} else if (data.status === 'failed') {
						removeJob(job.jobId);
						if (window.justUpToast) window.justUpToast.error('PDF generation failed: ' + (data.error || 'Unknown error'));
						window.dispatchEvent(new CustomEvent('pdf-job-completed', {
							detail: { jobId: job.jobId, targetId: job.targetId, success: false, error: data.error }
						}));
					}
					// else: still pending/processing, continue polling
				}).catch(function (err) {
					console.error('PDF poll error for job', job.jobId, err);
				});
			}

			function pollAllJobs() {
				var jobs = getPendingJobs();
				jobs.forEach(function (job) {
					if (inFlightJobs[job.jobId]) return;
					inFlightJobs[job.jobId] = true;
					pollJob(job).finally(function () {
						delete inFlightJobs[job.jobId];
					});
				});
			}

			// Global API to add a job (called from target_detail.html)
			window.justUpPdfJobs = {
				add: function (jobId, targetId) {
					var jobs = getPendingJobs();
					if (!jobs.find(function (j) { return j.jobId === jobId; })) {
						jobs.push({ jobId: jobId, targetId: targetId, addedAt: Date.now() });
						savePendingJobs(jobs);
					}
				},
				remove: removeJob,
				getPending: getPendingJobs
			};

			// Poll every 2 seconds if there are pending jobs
			setInterval(function () {
				if (document.visibilityState === 'hidden') return;
				if (getPendingJobs().length > 0) {
					pollAllJobs();
				}
			}, 2000);

			// Also poll immediately on page load
			if (getPendingJobs().length > 0) {
				setTimeout(pollAllJobs, 500);
			}
		})();

		// ─────────────────────────────────────────────────────────────────────────
		// HTMX Fragment Initialization (local-time formatting & accordion state)
		// ─────────────────────────────────────────────────────────────────────────

		(function () {
			// Read useUtc from data attribute on <html> element
			var useUtc = document.documentElement.dataset.useUtc === 'true';

			function initLocalTimes(root) {
				(root || document).querySelectorAll('.local-time:not([data-init])').forEach(function (el) {
					el.setAttribute('data-init', '1');
					var utc = el.dataset.utc;
					if (utc) {
						var d = new Date(utc);
						// Guard against invalid date strings
						if (isNaN(d.getTime())) {
							el.textContent = utc; // keep the raw server value
							return;
						}
						var options = useUtc
							? { timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
							: { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' };
						el.textContent = d.toLocaleString(undefined, options) + (useUtc ? ' UTC' : '');
					}
				});
			}

			function initAccordionState(root) {
				(root || document).querySelectorAll('.accordion-collapse:not([data-state-init])').forEach(function (collapse) {
					collapse.setAttribute('data-state-init', '1');
					var key = collapse.dataset.groupKey;
					var forceExpanded = collapse.dataset.forceExpanded === '1';
					if (key) {
						var saved;
						try {
							saved = localStorage.getItem(key);
						} catch (e) { /* localStorage unavailable */ }
						if (saved === 'collapsed' && !forceExpanded) {
							collapse.classList.remove('show');
							var button = document.querySelector('[data-group-key="' + CSS.escape(key) + '"].accordion-button');
							if (button) {
								button.classList.add('collapsed');
								button.setAttribute('aria-expanded', 'false');
							}
						}
						if (forceExpanded) {
							collapse.classList.add('show');
							var button2 = document.querySelector('[data-group-key="' + CSS.escape(key) + '"].accordion-button');
							if (button2) {
								button2.classList.remove('collapsed');
								button2.setAttribute('aria-expanded', 'true');
							}
							try {
								localStorage.setItem(key, 'expanded');
							} catch (e) { /* localStorage unavailable */ }
						}
						// Bind toggle persistence (once)
						collapse.addEventListener('shown.bs.collapse', function () {
							try {
								localStorage.setItem(key, 'expanded');
							} catch (e) { /* localStorage unavailable */ }
						});
						collapse.addEventListener('hidden.bs.collapse', function () {
							try {
								localStorage.setItem(key, 'collapsed');
							} catch (e) { /* localStorage unavailable */ }
						});
					}
				});
			}

			function initFragment(root) {
				initLocalTimes(root);
				initAccordionState(root);
				initTargetsTableMobileCollapse(root);
				initDowntimeHistoryFragment(root);
				initQualityWidget(root);
			}

			function initQualityWidget(root) {
				var scope = root || document;
				if (!scope.querySelector) return;
				var scoreEl = scope.querySelector('#quality-score');
				if (!scoreEl) return;
				var card = scoreEl.closest ? scoreEl.closest('.card') : null;
				if (card && card._qualityPollInterval) return;

				var labelEl = document.getElementById('quality-label');
				var badgeEl = document.getElementById('quality-state-badge');
				var updatedEl = document.getElementById('quality-updated');
				var targetsContainer = document.getElementById('quality-targets-container');
				var targetsEl = document.getElementById('quality-targets');
				if (!labelEl || !badgeEl || !updatedEl || !targetsContainer || !targetsEl) return;

				function setBadge(state) {
					var style = { cls: 'bg-secondary', text: 'Unknown' };
					if (state === 'ok') style = { cls: 'bg-success', text: 'OK' };
					else if (state === 'degraded') style = { cls: 'bg-warning text-dark', text: 'Degraded' };
					else if (state === 'down') style = { cls: 'bg-danger', text: 'Down' };
					badgeEl.className = 'badge ' + style.cls;
					badgeEl.textContent = style.text;
				}

				function renderTargets(list) {
					if (!Array.isArray(list) || list.length === 0) {
						targetsContainer.style.display = 'none';
						return;
					}
					targetsContainer.style.display = 'block';
					targetsEl.textContent = '';
					for (var i = 0; i < list.length; i++) {
						var t = String(list[i] || '');
						if (!t) continue;

						if (i > 0 && i % 3 === 0) {
							var br = document.createElement('div');
							br.className = 'w-100';
							targetsEl.appendChild(br);
						}

						var isHttp = t.indexOf('http://') === 0 || t.indexOf('https://') === 0;
						var icon = isHttp ? 'public' : 'dns';
						var label = isHttp ? t.replace(/^https?:\/\//, '') : t;

						var badge = document.createElement('span');
						badge.className = 'badge badge-muted';
						var iconEl = document.createElement('span');
						iconEl.className = 'material-icons me-1';
						iconEl.setAttribute('aria-hidden', 'true');
						iconEl.textContent = icon;
						badge.appendChild(iconEl);
						badge.appendChild(document.createTextNode(label));
						targetsEl.appendChild(badge);
					}
				}

				async function loadQuality() {
					if (typeof window.apiFetch !== 'function') return;
					try {
						var resp = await window.apiFetch('/quality/current');
						if (!resp.ok) return;
						var data = await resp.json();

						if (data && data.score !== null && data.score !== undefined) {
							var s = Number(data.score);
							if (!isNaN(s)) {
								scoreEl.textContent = s.toFixed(1);
								if (data.state === 'ok') scoreEl.className = 'display-4 fw-bold text-success';
								else if (data.state === 'degraded') scoreEl.className = 'display-4 fw-bold text-warning';
								else scoreEl.className = 'display-4 fw-bold text-danger';
							} else {
								scoreEl.textContent = '--';
								scoreEl.className = 'display-4 fw-bold text-muted';
							}
						} else {
							scoreEl.textContent = '--';
							scoreEl.className = 'display-4 fw-bold text-muted';
						}

						setBadge(data && data.state);

						var scoreNum = (data && data.score !== null && data.score !== undefined) ? Number(data.score) : null;
						if (scoreNum === null || isNaN(scoreNum)) labelEl.textContent = 'No connectivity';
						else if (scoreNum >= 80) labelEl.textContent = 'Excellent connectivity';
						else if (scoreNum >= 60) labelEl.textContent = 'Good connectivity';
						else labelEl.textContent = 'Poor connectivity';

						if (data && data.updated_at) {
							var d = new Date(data.updated_at);
							updatedEl.textContent = isNaN(d.getTime()) ? '' : ('Updated: ' + d.toLocaleTimeString());
						} else {
							updatedEl.textContent = '';
						}

						renderTargets(data && data.targets);
					} catch (e) {
						console.warn('Failed to load quality:', e);
					}
				}

				loadQuality();
				if (card) {
					card._qualityPollInterval = window.setInterval(loadQuality, 30000);
				}
			}

			function initTargetsTableMobileCollapse(root) {
				var acc = (root || document).querySelector && (root || document).querySelector('#targets-accordion');
				if (!acc) return;
				if (acc.getAttribute('data-mobile-collapsed') === '1') return;
				if (window.innerWidth >= 768) return;
				acc.setAttribute('data-mobile-collapsed', '1');
				acc.querySelectorAll('.accordion-collapse.show').forEach(function (el) {
					el.classList.remove('show');
					var btn = acc.querySelector('[data-bs-target="#' + el.id + '"]');
					if (btn) {
						btn.classList.add('collapsed');
						btn.setAttribute('aria-expanded', 'false');
					}
				});
			}

			function initDowntimeHistoryFragment(root) {
				var card = document.getElementById('downtime-history-card');
				if (!card) return;
				if (root && root !== card && !card.contains(root)) return;

				var rangeEl = card.querySelector('[data-range-label]');
				var headerLabel = document.getElementById('downtime-header-label');
				if (headerLabel && rangeEl && rangeEl.dataset.rangeLabel) {
					headerLabel.textContent = 'Downtimes (' + rangeEl.dataset.rangeLabel + ')';
				}

				var isMobile = window.innerWidth < 576;
				card.querySelectorAll('.local-time').forEach(function (el) {
					var utcStr = el.dataset.utc;
					var useUtc = el.dataset.useUtc === 'true';
					if (!utcStr) return;
					var dt = new Date(utcStr);
					if (isNaN(dt.getTime())) return;

					if (useUtc) {
						if (isMobile) {
							el.textContent = dt.toISOString().substring(5, 16).replace('T', ' ');
						} else {
							el.textContent = dt.toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
						}
					} else {
						if (isMobile) {
							el.textContent = dt.toLocaleString(undefined, {
								month: '2-digit',
								day: '2-digit',
								hour: '2-digit',
								minute: '2-digit'
							});
						} else {
							el.textContent = dt.toLocaleString();
						}
					}
				});
			}

			// Initial page load
			if (document.readyState === 'loading') {
				document.addEventListener('DOMContentLoaded', function () { initFragment(document); });
			} else {
				initFragment(document);
			}

			// Re-init after HTMX swaps
			document.body.addEventListener('htmx:load', function (evt) {
				initFragment(evt.detail.elt);
			});
		})();

		// ─────────────────────────────────────────────────────────────────────────
		// Mobile Footer: Show Only When Scrolled to Bottom
		// ─────────────────────────────────────────────────────────────────────────

		(function () {
			var threshold = 24; // px before bottom
			var ticking = false;

			function update() {
				if (window.innerWidth > 576) {
					document.body.classList.remove('show-footer');
					return;
				}
				var scrollPos = (window.scrollY || window.pageYOffset) + window.innerHeight;
				var docHeight = document.documentElement.scrollHeight;
				// Show footer if:
				// 1. User scrolled to bottom, OR
				// 2. Page content is not scrollable (viewport >= document height)
				var atBottom = scrollPos >= docHeight - threshold;
				var noScroll = window.innerHeight >= docHeight - 10;
				document.body.classList.toggle('show-footer', atBottom || noScroll);
			}

			function onScroll() {
				if (!ticking) {
					window.requestAnimationFrame(function () {
						update();
						ticking = false;
					});
					ticking = true;
				}
			}

			window.addEventListener('scroll', onScroll, { passive: true });
			window.addEventListener('load', update, { passive: true });
			window.addEventListener('resize', update, { passive: true });
			document.body.addEventListener('htmx:load', update);
			// Delay initial update to ensure DOM is fully rendered
			setTimeout(update, 100);
		})();

		// ─────────────────────────────────────────────────────────────────────────
		// Notification Status Poller (for target form redirect)
		// ─────────────────────────────────────────────────────────────────────────

		(function () {
			// Check for pending notification task on page load
			var taskJson = sessionStorage.getItem('pendingNotificationTask');
			if (!taskJson) return;

			try {
				var taskInfo = JSON.parse(taskJson);
				// Clear immediately to avoid re-polling on page refresh
				sessionStorage.removeItem('pendingNotificationTask');

				// Check if task is not too old (max 2 minutes)
				var age = Date.now() - taskInfo.timestamp;
				if (age > 120000) return;

				// Start polling
				pollNotificationStatus(taskInfo.targetId, taskInfo.taskId, taskInfo.totalCount);
			} catch (e) {
				console.warn('Failed to parse notification task:', e);
				sessionStorage.removeItem('pendingNotificationTask');
			}

			function pollNotificationStatus(targetId, taskId, totalCount) {
				var maxAttempts = 60; // 60 seconds max
				var attempts = 0;

				function poll() {
					// Skip polling when tab is hidden (don't count against maxAttempts)
					if (document.visibilityState === 'hidden') {
						setTimeout(poll, 1000);
						return;
					}
					attempts++;
					window.apiFetch('/targets/' + targetId + '/notification-status/' + taskId).then(function (resp) {
						if (!resp.ok) return null; // Task not found, stop polling
						return resp.json();
					}).then(function (status) {
						if (!status) return;

						if (status.status === 'completed') {
							if (window.justUpToast) window.justUpToast.success(status.success + ' recipient(s) notified successfully');
						} else if (status.status === 'completed_with_queued') {
							// Some notifications queued for later - this is NOT an error
							var queuedCount = status.queued || 0;
							if (window.justUpToast) window.justUpToast.success(
								status.success + ' recipient(s) notified. ' + queuedCount + ' message(s) queued for delivery.'
							);
						} else if (status.status === 'completed_with_errors') {
							// Actual errors (permanent failures)
							if (window.justUpToast) window.justUpToast.warning(
								status.success + '/' + totalCount + ' notified. Some notifications failed.'
							);
						} else if (attempts < maxAttempts) {
							// Still processing, continue polling
							setTimeout(poll, 1000);
						} else {
							if (window.justUpToast) window.justUpToast.warning(
								'Notification status check timed out. Notifications may still be processing.'
							);
						}
					}).catch(function (e) {
						console.warn('Failed to poll notification status:', e);
					});
				}

				poll();
			}
		})();

	} // end init()

	// Call init() when document.body is ready
	if (document.body) {
		init();
	} else {
		document.addEventListener('DOMContentLoaded', init);
	}

})();
