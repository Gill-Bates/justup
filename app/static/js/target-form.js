//
// app/static/js/target-form.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

	const config = document.getElementById('js-config').dataset;
	const targetId = parseInt(config.targetId, 10);
	let clearCredentials = false;

	// ═══════════════════════════════════════════════════════════════════════════════
	// TCP PORTS TAG INPUT
	// ═══════════════════════════════════════════════════════════════════════════════
	(function initTcpPortsInput() {
		const container = document.querySelector('.tcp-ports-container');
		const tagsContainer = document.getElementById('tcp-ports-tags');
		const input = document.getElementById('tcp_ports_input');
		const hiddenInput = document.getElementById('tcp_ports');

		if (!container || !tagsContainer || !input || !hiddenInput) return;

		let ports = [];

		// Strict parse: digits only, 1-65535; parseInt would turn "80abc" into 80.
		function parsePort(raw) {
			const text = String(raw).trim();
			if (!/^[0-9]+$/.test(text)) return null;
			const port = Number(text);
			return port >= 1 && port <= 65535 ? port : null;
		}

		// Initialize from hidden input value
		function initFromValue() {
			ports = [];
			const value = hiddenInput.value.trim();
			if (value) {
				value.split(',').forEach(p => {
					if (!p.trim()) return;
					const port = parsePort(p);
					if (port === null) {
						window.justUpToast?.error?.(`Ignored invalid TCP port "${p.trim()}"`);
					} else if (!ports.includes(port)) {
						ports.push(port);
					}
				});
			}
			renderTags();
		}

		function renderTags() {
			tagsContainer.innerHTML = '';
			ports.forEach(port => {
				const tag = document.createElement('span');
				tag.className = 'tcp-port-tag';
				tag.append(String(port));
				const removeBtn = document.createElement('button');
				removeBtn.type = 'button';
				removeBtn.className = 'remove-port';
				removeBtn.dataset.port = String(port);
				removeBtn.setAttribute('aria-label', `Remove TCP port ${port}`);
				const icon = document.createElement('span');
				icon.className = 'material-icons';
				icon.setAttribute('aria-hidden', 'true');
				icon.textContent = 'close';
				removeBtn.appendChild(icon);
				tag.appendChild(removeBtn);
				tagsContainer.appendChild(tag);
			});
			updateHiddenInput();
		}

		function updateHiddenInput() {
			hiddenInput.value = ports.join(',');
			// Lets the port picker's selected-ports list follow tag edits ("change" would re-initialise this input).
			hiddenInput.dispatchEvent(new Event('input', { bubbles: true }));
		}

		function addPort(value) {
			const port = parsePort(value);
			if (port === null) {
				window.justUpToast?.error?.(`Invalid TCP port "${value.trim()}": use a whole number from 1 to 65535`);
				return false;
			}
			if (ports.includes(port)) {
				return false;
			}
			ports.push(port);
			ports.sort((a, b) => a - b);
			renderTags();
			return true;
		}

		function removePort(port) {
			ports = ports.filter(p => p !== port);
			renderTags();
		}

		// Handle input
		input.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ' || e.key === ',') {
				e.preventDefault();
				const value = input.value.trim().replace(/,/g, '');
				if (value) {
					addPort(value);
					input.value = '';
				}
			} else if (e.key === 'Backspace' && !input.value && ports.length > 0) {
				removePort(ports[ports.length - 1]);
			}
		});

		// Handle paste
		input.addEventListener('paste', (e) => {
			e.preventDefault();
			const text = (e.clipboardData || window.clipboardData).getData('text');
			const parts = text.split(/[,\s]+/);
			parts.filter(Boolean).forEach(p => addPort(p));
			input.value = '';
		});

		// Handle blur - add any remaining input
		input.addEventListener('blur', () => {
			const value = input.value.trim().replace(/,/g, '');
			if (value) {
				addPort(value);
				input.value = '';
			}
		});

		// Handle remove button clicks
		tagsContainer.addEventListener('click', (e) => {
			const removeBtn = e.target.closest('.remove-port');
			if (removeBtn) {
				removePort(parseInt(removeBtn.dataset.port, 10));
			}
		});

		// Click on container focuses input
		container.addEventListener('click', () => {
			input.focus();
		});

		initFromValue();
		hiddenInput.addEventListener('change', initFromValue);
	})();

	// ═══════════════════════════════════════════════════════════════════════════════
	// CHECK CARD LOGIC (replaces accordion complexity)
	// ═══════════════════════════════════════════════════════════════════════════════
	document.querySelectorAll('.check-card').forEach(card => {
		const toggle = card.querySelector('.card-header .form-check-input');
		const body = card.querySelector('.check-body');

		if (!toggle || !body) return;

		function updateCardState() {
			const enabled = toggle.checked;
			body.classList.toggle('hidden', !enabled);
			body.querySelectorAll('input, select, button').forEach(el => {
				// Don't disable the toggle itself
				if (el !== toggle) el.disabled = !enabled;
			});
			card.classList.toggle('disabled', !enabled);
		}

		toggle.addEventListener('change', updateCardState);
		updateCardState();
	});

	// ═══════════════════════════════════════════════════════════════════════════════
	// SLA TOGGLE
	// ═══════════════════════════════════════════════════════════════════════════════
	const slaToggle = document.getElementById('sla_enabled');
	const slaFields = document.getElementById('sla-fields');
	if (slaToggle && slaFields) {
		slaToggle.addEventListener('change', function () {
			slaFields.hidden = !this.checked;
		});
	}

	// ═══════════════════════════════════════════════════════════════════════════════
	// BASIC AUTH TOGGLE
	// ═══════════════════════════════════════════════════════════════════════════════
	const basicAuthToggle = document.getElementById('http_basic_auth_enabled');
	const authFields = document.querySelectorAll('.auth-field');
	const usernameField = document.getElementById('http_username');
	const passwordField = document.getElementById('http_password');

	function _hasBasicAuthConfigured() {
		const u = usernameField?.value?.trim?.() || '';
		const p = passwordField?.value || '';
		// On edit, password is not prefilled; we signal stored secret via placeholder.
		const hasStoredPassword = !!(passwordField?.placeholder && passwordField.placeholder.trim());
		return !!(u || p || hasStoredPassword || usernameField?.placeholder);
	}

	function _applyBasicAuthUiState(enabled) {
		if (basicAuthToggle) basicAuthToggle.checked = !!enabled;
		authFields.forEach(field => {
			field.hidden = !enabled;
		});
	}

	if (basicAuthToggle && authFields.length) {
		// Fix browser form-state restore oddities: the checkbox can come back checked
		// even when no credentials exist (e.g. after reload/navigation).
		// For new targets (no existing data), always start with auth OFF.
		_applyBasicAuthUiState(config.basicAuthEnabled === 'true');

		basicAuthToggle.addEventListener('change', function () {
			_applyBasicAuthUiState(this.checked);

			// Reset clearCredentials flag when auth is re-enabled
			if (this.checked) {
				clearCredentials = false;
			}

			// UX: disabling auth should NOT delete stored credentials.
			// Only clear the *typed* password value to avoid keeping secrets in hidden inputs.
			if (!this.checked) {
				if (passwordField) passwordField.value = '';
			}
		});

		// If the browser auto-fills credentials after our initial sync, enable UI.
		[usernameField, passwordField].forEach(el => {
			if (!el) return;
			el.addEventListener('input', () => {
				if (_hasBasicAuthConfigured()) _applyBasicAuthUiState(true);
			});
		});
	}

	// ═══════════════════════════════════════════════════════════════════════════════
	// CREDENTIALS HANDLING
	// ═══════════════════════════════════════════════════════════════════════════════
	const clearBtn = document.getElementById('clear-credentials-btn');
	if (clearBtn) {
		clearBtn.addEventListener('click', async () => {
			if (await window.confirmAction({ title: 'Clear credentials', message: 'The stored username and password are removed when you save.', confirmLabel: 'Clear' })) {
				clearCredentials = true;
				_applyBasicAuthUiState(false);
				document.getElementById('http_username').value = '';
				document.getElementById('http_password').value = '';
				document.getElementById('http_password').placeholder = '';
				clearBtn.classList.replace('btn-outline-danger', 'btn-danger');
				clearBtn.innerHTML = '<span class="material-icons" aria-hidden="true">check</span>';
				clearBtn.disabled = true;
			}
		});
	}

	// ═══════════════════════════════════════════════════════════════════════════════
	// URL PREVIEW
	// ═══════════════════════════════════════════════════════════════════════════════
	const hostInput = document.getElementById('host-input');
	const httpUrlInput = document.getElementById('http-url-input');
	const httpPortInput = document.getElementById('http_port');
	const httpPreviewUrl = document.getElementById('http-preview-url');

	function _applyPortToUrl(url, port) {
		try {
			if (!url) return url;
			const p = parseInt(port, 10);
			if (!p || p < 1 || p > 65535) return url;
			const u = new URL(url);
			if (u.port) return url;
			const defaultPort = (u.protocol === 'https:') ? 443 : 80;
			if (p === defaultPort) return url;
			u.port = String(p);
			return u.toString();
		} catch (e) {
			return url;
		}
	}

	function getConfiguredHttpUrl() {
		const host = hostInput?.value.trim();
		if (!host) return null;
		const path = httpUrlInput?.value.trim() || '';
		if (path.includes('://')) return path;
		const scheme = httpUrlInput?.dataset.defaultScheme || 'https';
		const urlHost = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
		if (!path) return `${scheme}://${urlHost}/`;
		return `${scheme}://${urlHost}${path.startsWith('/') ? '' : '/'}${path}`;
	}

	function updatePreviews() {
		const fullUrl = _applyPortToUrl(getConfiguredHttpUrl(), httpPortInput?.value) || '---';
		if (httpPreviewUrl) httpPreviewUrl.textContent = fullUrl;
	}

	if (hostInput && httpUrlInput) {
		hostInput.addEventListener('input', updatePreviews);
		httpUrlInput.addEventListener('input', updatePreviews);
		httpPortInput?.addEventListener('input', updatePreviews);
		updatePreviews();
	}

	// Danger zone warning icon for "ignore cert errors"
	const ignoreCertToggleUi = document.getElementById('ignore_cert_errors');
	const ignoreCertWarningIcon = document.getElementById('ignore_cert_warning');
	if (ignoreCertToggleUi && ignoreCertWarningIcon) {
		const updateIgnoreCertIcon = () => {
			ignoreCertWarningIcon.hidden = !ignoreCertToggleUi.checked;
		};
		ignoreCertToggleUi.addEventListener('change', updateIgnoreCertIcon);
		updateIgnoreCertIcon();
	}

	// Auto-fill host from monitor name
	const nameInput = document.getElementById('name');
	if (nameInput && hostInput) {
		nameInput.addEventListener('input', function () {
			if (hostInput.value.trim()) return;
			let name = this.value.trim();
			if (!name) return;

			// Extract hostname before any parentheses (e.g., "host.example.com (Web)" -> "host.example.com")
			const parenIndex = name.indexOf('(');
			if (parenIndex > 0) {
				name = name.substring(0, parenIndex).trim();
			}

			try {
				const candidate = name.includes('://') ? name : `https://${name}`;
				const parsed = new URL(candidate);
				const labels = parsed.hostname.split('.').filter(Boolean);
				const tld = labels[labels.length - 1] || '';
				if (tld.length >= 2) {
					hostInput.value = parsed.hostname;
					updatePreviews();
				}
			} catch (e) { }
		});
	}

	// ═══════════════════════════════════════════════════════════════════════════════
	// FORM SUBMISSION
	// ═══════════════════════════════════════════════════════════════════════════════
	document.getElementById('udp_ports').addEventListener('input', event => event.target.setCustomValidity(''));
	document.getElementById('target-form').addEventListener('submit', async (e) => {
		e.preventDefault();
		const form = e.target;
		const udpValue = form.udp_ports.value.trim();
		const validUdpPorts = udpValue && udpValue.split(',').every(part => {
			const text = part.trim();
			return /^[0-9]+$/.test(text) && Number(text) >= 1 && Number(text) <= 65535;
		});
		if ((form.enable_udp_scan.checked || udpValue) && !validUdpPorts) {
			form.udp_ports.setCustomValidity('Enter UDP ports from 1 to 65535, separated by commas.');
			form.udp_ports.reportValidity();
			return;
		}
		form.udp_ports.setCustomValidity('');
		const submitBtn = form.querySelector('button[type="submit"]');
		const originalSubmitHtml = submitBtn.innerHTML;

		// Prevent double-submit
		if (submitBtn.disabled) return;
		submitBtn.disabled = true;
		submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span> Saving...';

		const _normText = (v) => {
			if (v === undefined || v === null) return null;
			const s = String(v).trim();
			return s || null;  // Only empty string → null
		};

		const _normUrl = (v) => {
			const s = _normText(v);
			if (!s) return null;
			if (!s.includes('://')) return `https://${s}`;
			return s;
		};

		const computedHttpUrl = getConfiguredHttpUrl();

		const basicAuthEnabled = form.http_basic_auth_enabled?.checked ?? false;

		const data = {
			name: _normText(form.name.value),
			group: _normText(form.group.value),
			host: _normText(form.host.value),
			interval_seconds: parseInt(form.interval_seconds.value),
			retention_days: parseInt(form.retention_days.value),
			max_retries: parseInt(form.max_retries.value),
			is_enabled: form.is_enabled.checked,
			auto_close_alert: form.auto_close_alert.checked,
			enable_ping: form.enable_ping.checked,
			enable_http_check: form.enable_http_check.checked,
			http_prefer_head: form.http_prefer_head.checked,
			http_basic_auth_enabled: basicAuthEnabled,
			http_port: form.http_port?.value ? parseInt(form.http_port.value) : null,
			http_success_codes: _normText(form.http_success_codes?.value) || '200-399',
			enable_tcp_connect: form.enable_tcp_connect.checked,
			enable_udp_scan: form.enable_udp_scan.checked,
			enable_cert_expiration: form.enable_http_check.checked && form.enable_cert_expiration.checked,
			// Ignore cert errors only when HTTP is enabled
			ignore_cert_errors: form.enable_http_check.checked && (form.ignore_cert_errors?.checked ?? false),
			ping_host: _normText(form.ping_host.value),
			http_url: computedHttpUrl,
			tcp_host: _normText(form.tcp_host.value),
			tcp_ports: _normText(form.tcp_ports.value),
			udp_ports: _normText(form.udp_ports.value),
			cert_host: _normText(form.cert_host.value),
			cert_port: form.cert_port.value ? parseInt(form.cert_port.value) : null,
			sla_enabled: form.sla_enabled.checked,
			// Clear SLA values when disabled to avoid dirty data
			sla_availability_pct: form.sla_enabled.checked && form.sla_availability_pct.value ? parseFloat(form.sla_availability_pct.value) : null,
			sla_response_time_ms: form.sla_enabled.checked && form.sla_response_time_ms.value ? parseFloat(form.sla_response_time_ms.value) : null,
			sla_ping_latency_ms: form.sla_enabled.checked && form.sla_ping_latency_ms.value ? parseFloat(form.sla_ping_latency_ms.value) : null
		};
		if (form.network_scan_id.value) data.network_scan_id = form.network_scan_id.value;

		// Handle password: new password has ALWAYS priority over clear flag
		if (basicAuthEnabled && form.http_password.value) {
			// New password entered → send it (overwrites old)
			data.http_password = form.http_password.value;
		} else if (clearCredentials) {
			// No new password, but explicitly cleared
			data.clear_http_password = true;
		}
		if (basicAuthEnabled && form.http_username.value.trim()) {
			data.http_username = form.http_username.value.trim();
		}

		// Stays true after a complete save so the button remains locked until the redirect lands.
		let saveComplete = false;
		try {
			const resp = await apiFetch(`/targets/${targetId}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(data)
			});
			if (!resp.ok) {
				const err = await resp.json();
				let msg = 'Error saving target';
				if (typeof err.detail === 'string' && err.detail.trim()) {
					msg = err.detail;
				} else if (Array.isArray(err.detail)) {
					msg = err.detail.map(e => {
						const loc = Array.isArray(e.loc) ? e.loc.join('.') : '';
						return `${loc ? loc + ': ' : ''}${e.msg || 'Invalid value'}`;
					}).join('\n');
				}
				throw new Error(msg);
			}
			const result = await resp.json();

			// Save alert recipients (returns immediately, notifications sent in background).
			// Skipped while the list is not fully loaded: PUT [] would wipe existing assignments.
			let alertResp = null;
			if (alertRecipientsLoaded) {
				const selectedRecipients = Array.from(document.querySelectorAll('#alert-users-checkboxes input:checked'))
					.map(cb => parseInt(cb.value));
				alertResp = await apiFetch(`/targets/${result.id}/alert-users`, {
					method: 'PUT',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify(selectedRecipients)
				});

				if (!alertResp.ok) {
					throw new Error(`Target saved, but alert recipients could not be saved (${alertResp.status}). Retry Save Target to complete the assignment.`);
				}
			}

			window.justUpToast?.success?.('Target updated');
			if (!alertResp) {
				window.justUpToast?.warning?.('Alert recipients were not loaded, so their assignment was left unchanged.');
			}

			// If notifications are being sent, save task info for polling after redirect
			if (alertResp?.ok) {
				const alertResult = await alertResp.json();
				if (alertResult.notification_task_id && alertResult.new_recipient_count > 0) {
					const taskInfo = {
						targetId: result.id,
						taskId: alertResult.notification_task_id,
						totalCount: alertResult.new_recipient_count,
						timestamp: Date.now()
					};
					sessionStorage.setItem('pendingNotificationTask', JSON.stringify(taskInfo));
				}
			}

			saveComplete = true;
			// Back to the targets overview after a short delay so the toast can be seen
			setTimeout(() => {
				window.location.href = '/ui/targets';
			}, 800);
			// A bfcache restore (browser Back) must not show a stuck "Saving..." button
			window.addEventListener('pageshow', (event) => {
				if (!event.persisted) return;
				submitBtn.disabled = false;
				submitBtn.innerHTML = originalSubmitHtml;
			}, { once: true });
		} catch (err) {
			window.justUpToast?.error?.(err.message);
		} finally {
			if (!saveComplete) {
				submitBtn.disabled = false;
				submitBtn.innerHTML = originalSubmitHtml;
			}
		}
	});

	// ═══════════════════════════════════════════════════════════════════════════════
	// ALERT RECIPIENTS LOADING
	// ═══════════════════════════════════════════════════════════════════════════════
	// True only after recipients and current assignments were both read successfully.
	let alertRecipientsLoaded = false;

	async function loadAlertUsersCheckboxes() {
		const container = document.getElementById('alert-users-checkboxes');
		alertRecipientsLoaded = false;
		try {
			const recipientsResp = await apiFetch('/recipients');
			if (!recipientsResp.ok) throw new Error('Failed to load recipients');
			const recipients = await recipientsResp.json();

			if (recipients.length === 0) {
				container.innerHTML = '<div class="text-body-secondary small">No recipients configured. <a href="/ui/settings#recipients">Add recipients in settings</a></div>';
				alertRecipientsLoaded = true;
				return;
			}

			let selectedIds = [];
			if (targetId) {
				const selectedResp = await apiFetch(`/targets/${targetId}/alert-users`);
				if (!selectedResp.ok) throw new Error('Failed to load alert assignments');
				selectedIds = await selectedResp.json();
			}

			const tilesHtml = recipients.map(r => {
				const checked = selectedIds.includes(r.id) ? 'checked' : '';
				const statusBadge = r.is_enabled
					? JuStatusDot.html('success', 'Active')
					: JuStatusDot.html('neutral', 'Disabled');

				const channels = [];
				if (r.phone) {
					channels.push(`
							<span class="alert-recipient-channel" title="${escapeHtml(r.phone)}">
								<span class="material-icons" aria-hidden="true">sms</span>
								<span class="alert-recipient-channel-text">${escapeHtml(r.phone)}</span>
							</span>
						`);
				}
				if (r.email) {
					channels.push(`
							<span class="alert-recipient-channel" title="${escapeHtml(r.email)}">
								<span class="material-icons" aria-hidden="true">email</span>
								<span class="alert-recipient-channel-text">${escapeHtml(r.email)}</span>
							</span>
						`);
				}

				return `
						<div class="alert-recipient-tile">
							<label class="alert-recipient-label" for="alert-recipient-${r.id}">
								<input class="form-check-input" type="checkbox" value="${r.id}" id="alert-recipient-${r.id}" ${checked}>
								<span class="alert-recipient-body">
									<span class="alert-recipient-name-row">
										<span class="alert-recipient-name">
											<span class="material-icons" aria-hidden="true">person</span>
											<span class="alert-recipient-name-text">${escapeHtml(r.name)}</span>
										</span>
										<span class="alert-recipient-status">${statusBadge}</span>
									</span>
									${channels.length ? `<span class="alert-recipient-channels">${channels.join('')}</span>` : '<span class="alert-recipient-channels"><span class="text-body-secondary">No channels</span></span>'}
								</span>
							</label>
						</div>
					`;
			}).join('');

			container.innerHTML = `<div class="alert-recipients-grid">${tilesHtml}</div>`;
			alertRecipientsLoaded = true;
		} catch (e) {
			container.innerHTML = '<div class="text-danger small">Error loading recipients. Alert recipient assignments will not be changed when saving.</div>';
		}
	}



	loadAlertUsersCheckboxes();
