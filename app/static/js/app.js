//
// app/static/js/app.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * justUp! Main Application JavaScript
 * Handles HTMX configuration, toast notifications, offline detection,
 * theme persistence, and fragment initialization (PDF job polling: pdf-jobs.js).
 */
(function () {
	'use strict';

	try { localStorage.removeItem('token'); } catch (e) { /* storage unavailable */ }

	// ─────────────────────────────────────────────────────────────────────────
	// Global Fetch Helper (available immediately)
	// ─────────────────────────────────────────────────────────────────────────

	/**
	 * Fetch wrapper with automatic CSRF headers and same-origin cookies.
	 * Use this instead of raw fetch() in templates to avoid auth/CSRF drift.
	 */
	window.apiFetch = function apiFetch(url, options) {
		var opts = options ? Object.assign({}, options) : {};
		var headers = new Headers(opts.headers || {});

		var csrf = getCsrfToken();
		if (csrf && !headers.has('X-CSRF-Token')) {
			headers.set('X-CSRF-Token', csrf);
		}

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
		if (!document.body) {
			console.warn('app.js init called before document.body exists');
			return;
		}

		// HTMX Configuration

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

			if (elt.id === 'logout-btn') {
				if (window.TracerouteCache) window.TracerouteCache.clearAll(); // cached diagnostics hold internal hosts
				if (window.JustUpUserStorage) window.JustUpUserStorage.clearUserScoped(); // e.g. PDF file names
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

			var initFn = elt.getAttribute('data-settle-init');
			if (initFn && typeof window[initFn] === 'function') {
				window[initFn]();
			}
		});

		// Global HTMX Configuration (CSRF; authentication uses same-origin cookies)
		document.body.addEventListener('htmx:configRequest', function (evt) {
			// CSRF Token
			var csrfToken = getCsrfToken();
			if (csrfToken) {
				evt.detail.headers['X-CSRF-Token'] = csrfToken;
			}
		});

		// Toast Notifications

		var vanillaToastCount = 0;

		function showVanillaToast(message, variant) {
			var el = document.createElement('div');
			var offset = 1 + (vanillaToastCount * 4.5);
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

		(function () {
			var urlParams = new URLSearchParams(window.location.search);
			var toastMsg = urlParams.get('toast');
			if (toastMsg) {
				var safMsg = toastMsg.length > 200 ? toastMsg.substring(0, 200) + '…' : toastMsg;
				setTimeout(function () { showToast(safMsg, 'success', 'check_circle'); }, 100);
				urlParams.delete('toast');
				var newUrl = urlParams.toString()
					? window.location.pathname + '?' + urlParams.toString() + window.location.hash
					: window.location.pathname + window.location.hash;
				window.history.replaceState({}, '', newUrl);
			}
		})();

		// Server Offline Detection

		(function () {
			var overlayHint = document.getElementById('server-offline-hint');
			var isOffline = false;
			var offlineOpener = null;
			var live = window.JustUpLive && window.JustUpLive.client;

			function setOffline(on) {
				if (on === isOffline) return;
				isOffline = on;
				document.body.classList.toggle('server-offline', on);
				// Keep keyboard and screen-reader users out of the blurred, inert app shell.
				var shell = document.getElementById('app-shell');
				var overlay = document.getElementById('server-offline-overlay');
				if (on && overlay) {
					offlineOpener = document.activeElement;
					overlay.setAttribute('tabindex', '-1');
				}
				if (shell) shell.inert = on;
				if (on) {
					if (overlayHint) overlayHint.textContent = 'Retrying automatically';
					if (overlay) overlay.focus({ preventScroll: true });
				} else {
					var back = offlineOpener && offlineOpener.isConnected ? offlineOpener : document.getElementById('main-content');
					offlineOpener = null;
					if (back && back.focus) back.focus({ preventScroll: true });
				}
			}

			// The live client owns reachability: an SSE drop alone never lands here, only failing HTTP health checks.
			if (live) {
				live.onState(function (state) {
					setOffline(state === 'offline');
				});
			}
		})();

		// Dark Mode Toggle with DB Persistence

		(function () {
			var toggle = document.getElementById('theme-toggle');
			var themeChanged = false;
			var themes = ['system', 'light', 'dark'];
			var selectedTheme = document.documentElement.getAttribute('data-user-theme') || 'system';
			if (themes.indexOf(selectedTheme) === -1) selectedTheme = 'system';
			var themeSaveQueue = Promise.resolve();

			// Sync the control after theme-init ran before the DOM was ready.
			if (toggle && window.setTheme) {
				window.setTheme(selectedTheme);
			}

			function saveThemeToDb(theme) {
				themeSaveQueue = themeSaveQueue.catch(function () {}).then(function () {
					return window.apiFetch('/me/theme', {
						method: 'PATCH',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ theme: theme })
					}).then(function (resp) {
						if (resp.status === 401 || resp.status === 403) return;
						if (!resp.ok) throw new Error('Theme update failed: ' + resp.status);
					});
				}).catch(function (e) {
					console.warn('Failed to save theme to DB:', e);
				});
			}

			if (toggle) {
				toggle.addEventListener('click', function () {
					themeChanged = true;
					var next = themes[(themes.indexOf(selectedTheme) + 1) % themes.length];
					selectedTheme = next;
					if (window.setTheme) window.setTheme(next);
					saveThemeToDb(next);
				});
			}

			function loadThemeFromDb() {
				window.apiFetch('/me/theme').then(function (resp) {
					if (resp.status === 401 || resp.status === 403) return null;
					if (!resp.ok) throw new Error('Theme lookup failed: ' + resp.status);
					return resp.json();
				}).then(function (data) {
					if (!themeChanged && data && themes.indexOf(data.theme) !== -1) {
						selectedTheme = data.theme;
						if (window.setTheme) window.setTheme(selectedTheme);
					}
				}).catch(function (e) {
					console.warn('Failed to load theme from DB:', e);
				});
			}

			loadThemeFromDb();
		})();

		// HTMX Fragment Initialization (local-time formatting & accordion state)

		(function () {
			var useUtc = document.documentElement.dataset.useUtc === 'true';

			function initLocalTimes(root) {
				(root || document).querySelectorAll('.local-time:not([data-init])').forEach(function (el) {
					el.setAttribute('data-init', '1');
					var utc = el.dataset.utc;
					if (utc) {
						var d = new Date(utc);
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
				var targetsTrigger = document.getElementById('quality-targets-tooltip-trigger');
				var targetsCount = document.getElementById('quality-target-count');
				if (!labelEl || !badgeEl || !updatedEl || !targetsTrigger || !targetsCount) return;

				var consecutiveFailures = 0;
				var loadingQuality = false;
				var failureLimit = 2;
				var requestTimeoutMs = 10000;
				var targetsContent = document.createElement('div');
				targetsContent.className = 'quality-targets-tooltip-content';
				var targetsTooltip = null;
				function hideTargetsTooltipIfPointerLeaves() {
					if (targetsTooltip && !targetsTrigger.matches(':hover')) targetsTooltip.hide();
				}
				targetsTrigger.addEventListener('show.bs.tooltip', function () {
					document.addEventListener('pointermove', hideTargetsTooltipIfPointerLeaves);
				});
				targetsTrigger.addEventListener('hidden.bs.tooltip', function () {
					document.removeEventListener('pointermove', hideTargetsTooltipIfPointerLeaves);
				});

				function renderTargets(list) {
					targetsContent.textContent = '';
					if (!Array.isArray(list)) {
						targetsTrigger.hidden = true;
						if (targetsTooltip) targetsTooltip.hide();
						return;
					}
					var targetCount = 0;
					for (var i = 0; i < list.length; i++) {
						var t = String(list[i] || '');
						if (!t) continue;

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
						targetsContent.appendChild(badge);
						targetCount++;
					}
					if (targetCount === 0) {
						targetsTrigger.hidden = true;
						if (targetsTooltip) targetsTooltip.hide();
						return;
					}
					if (!window.bootstrap || !window.bootstrap.Tooltip) {
						targetsTrigger.hidden = true;
						return;
					}
					targetsTrigger.hidden = false;
					targetsCount.textContent = targetCount + (targetCount === 1 ? ' target' : ' targets');
					targetsTooltip = window.bootstrap.Tooltip.getOrCreateInstance(targetsTrigger, {
						container: 'body',
						html: true,
						title: function () { return targetsContent.cloneNode(true); },
					});
				}

				async function loadQuality() {
					if (typeof window.apiFetch !== 'function' || !window.QualityState || loadingQuality) return;
					loadingQuality = true;
					var controller = new AbortController();
					var timeout = window.setTimeout(function () { controller.abort(); }, requestTimeoutMs);
					try {
						var resp = await window.apiFetch('/quality/current', { signal: controller.signal });
						if (!resp.ok) {
							consecutiveFailures++;
						if (consecutiveFailures >= failureLimit) showQualityUnavailable();
						return;
						}
						var data = await resp.json();

						var view = window.QualityState.describe(data);
						scoreEl.textContent = view.scoreText;
						scoreEl.className = view.scoreClass;
						badgeEl.className = 'badge ' + view.badgeClass;
						badgeEl.textContent = view.badgeText;
						labelEl.textContent = view.label;
						var measuredAt = data && data.updated_at ? new Date(data.updated_at) : null;
						updatedEl.textContent = measuredAt && !isNaN(measuredAt.getTime())
							? 'Last measurement: ' + measuredAt.toLocaleString()
							: 'No successful measurement yet';
						consecutiveFailures = 0;
						renderTargets(data && data.targets);
					} catch (e) {
						consecutiveFailures++;
						if (consecutiveFailures >= failureLimit) showQualityUnavailable();
						console.warn('Failed to load quality:', e);
					} finally {
						window.clearTimeout(timeout);
						loadingQuality = false;
					}
				}

				function showQualityUnavailable() {
					var view = window.QualityState.describe({ state: 'unknown' });
					scoreEl.textContent = view.scoreText;
					scoreEl.className = view.scoreClass;
					badgeEl.className = 'badge ' + view.badgeClass;
					badgeEl.textContent = view.badgeText;
					labelEl.textContent = 'Data unavailable';
					renderTargets([]);
				}

				loadQuality();
				if (card) {
					// Poll only while the widget is shown in a visible tab; stop once the card is gone.
					var refreshIfShown = function () {
						if (!card.isConnected) {
							window.clearInterval(card._qualityPollInterval);
							document.removeEventListener('visibilitychange', refreshIfShown);
							document.removeEventListener('dashboard:widget-shown', refreshIfShown);
							document.body.removeEventListener('dashboard-refresh', refreshIfShown);
							return;
						}
						if (document.hidden || card.closest('.d-none')) return;
						loadQuality();
					};
					card._qualityPollInterval = window.setInterval(refreshIfShown, 30000);
					// Reload immediately when the tab or the widget becomes visible again.
					document.addEventListener('visibilitychange', refreshIfShown);
					document.addEventListener('dashboard:widget-shown', refreshIfShown);
					document.body.addEventListener('dashboard-refresh', refreshIfShown);
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

			if (document.readyState === 'loading') {
				document.addEventListener('DOMContentLoaded', function () { initFragment(document); });
			} else {
				initFragment(document);
			}

			document.body.addEventListener('htmx:load', function (evt) {
				initFragment(evt.detail.elt);
			});
		})();

		// Notification Status Poller (for target form redirect)

		(function () {
			var taskJson = sessionStorage.getItem('pendingNotificationTask');
			if (!taskJson) return;

			try {
				var taskInfo = JSON.parse(taskJson);
				// Clear immediately to avoid re-polling on page refresh
				sessionStorage.removeItem('pendingNotificationTask');

				var age = Date.now() - taskInfo.timestamp;
				if (age > 120000) return;

				pollNotificationStatus(taskInfo.targetId, taskInfo.taskId, taskInfo.totalCount);
			} catch (e) {
				console.warn('Failed to parse notification task:', e);
				sessionStorage.removeItem('pendingNotificationTask');
			}

			function pollNotificationStatus(targetId, taskId, totalCount) {
				var maxAttempts = 60;
				var attempts = 0;

				function poll() {
					// Skip polling when tab is hidden (don't count against maxAttempts)
					if (document.visibilityState === 'hidden') {
						setTimeout(poll, 1000);
						return;
					}
					attempts++;
					window.apiFetch('/targets/' + targetId + '/notification-status/' + taskId).then(function (resp) {
						if (!resp.ok) return null;
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
							if (window.justUpToast) window.justUpToast.warning(
								status.success + '/' + totalCount + ' notified. Some notifications failed.'
							);
						} else if (attempts < maxAttempts) {
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

	}

	if (document.body) {
		init();
	} else {
		document.addEventListener('DOMContentLoaded', init);
	}

})();

// ─────────────────────────────────────────────────────────────────────────
// Modal accessibility: initial focus and focus return
// ─────────────────────────────────────────────────────────────────────────
(function () {
	'use strict';

	var openers = new WeakMap();
	// Central policy: a click or tap outside the dialog never dismisses a modal.
	// Escape and explicit close/cancel controls still do (keyboard stays true).
	if (window.bootstrap && window.bootstrap.Modal) {
		window.bootstrap.Modal.Default.backdrop = 'static';
		window.bootstrap.Modal.Default.keyboard = true;
	}

	// Remember the opener, also for modals opened via bootstrap.Modal#show().
	document.addEventListener('show.bs.modal', function (event) {
		var modal = event.target;
		var opener = event.relatedTarget || document.activeElement;
		if (opener && opener !== document.body) openers.set(modal, opener);
		// Bootstrap sets role/aria-modal itself; make sure the name is present.
		if (!modal.hasAttribute('aria-label') && !modal.hasAttribute('aria-labelledby')) {
			var title = modal.querySelector('.modal-title');
			if (title && title.id) modal.setAttribute('aria-labelledby', title.id);
		}
	});

	// Prefer an explicit [autofocus] control; otherwise Bootstrap focuses the dialog.
	document.addEventListener('shown.bs.modal', function (event) {
		var target = event.target.querySelector('[autofocus]');
		if (target) target.focus();
	});

	document.addEventListener('hidden.bs.modal', function (event) {
		var opener = openers.get(event.target);
		openers.delete(event.target);
		if (opener && opener.isConnected && typeof opener.focus === 'function' &&
			!document.querySelector('.modal.show')) {
			opener.focus({ preventScroll: true });
		}
	});
})();
