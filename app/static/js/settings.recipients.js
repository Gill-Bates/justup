//
// app/static/js/settings.recipients.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Recipients Management for Settings Page

// Cache for Signal registration status
// Key: phone number as returned by backend (E.164 normalized)
// Value: { registered: true|false|null, cachedAt: timestamp }
let _signalStatusCache = {};
// Frontend TTL is shorter (10min) than backend (24h) to allow re-verification
// after user actions like linking a Signal account
const SIGNAL_STATUS_TTL_MS = 10 * 60 * 1000;

const SIGNAL_ICONS = {
	registered: 'check_circle',
	unregistered: 'warning',
	pending: 'hourglass_empty',
	unknown: 'help_outline',
	unavailable: 'cancel',  // Gray X for "no Signal account"
};

// Normalize phone for cache key lookup (strip whitespace only)
// Note: Backend returns E.164 normalized phones, this just handles minor display variations
function normalizePhoneForCache(phone) {
	if (!phone) return '';
	return String(phone).replace(/\s+/g, '').trim();
}

async function extractApiError(response, fallbackMessage) {
	let msg = fallbackMessage;
	try {
		const data = await response.json();
		// Classic script: the ES module formatter is loaded dynamically.
		const { formatErrorDetail } = await import('./settings.core.js');
		msg = formatErrorDetail(data?.detail) || (typeof data?.error === 'string' && data.error) || msg;
	} catch (e) {
		// ignore parse errors
	}
	return msg;
}

function validateRecipientInput({ name, signalEnabled, emailEnabled, phone, email }) {
	if (!name) return 'Please enter a name';
	if (!signalEnabled && !emailEnabled) return 'Please enable at least one notification channel';
	if (signalEnabled && !phone) return 'Please enter a Signal phone number';
	if (emailEnabled && !email) return 'Please enter an email address';
	return null;
}

function _getCachedSignalRegistration(phone) {
	const key = normalizePhoneForCache(phone);
	if (!key) return undefined;
	const entry = _signalStatusCache[key];
	// Backwards-compat: previously allowed boolean
	if (typeof entry === 'boolean') {
		_signalStatusCache[key] = { registered: entry, cachedAt: Date.now() };
		return entry;
	}
	if (!entry || typeof entry !== 'object') return undefined;
	// Allow true, false, or null (unavailable)
	if (entry.registered !== true && entry.registered !== false && entry.registered !== null) return undefined;
	if (typeof entry.cachedAt !== 'number') return undefined;
	if ((Date.now() - entry.cachedAt) > SIGNAL_STATUS_TTL_MS) return undefined;
	return entry.registered;
}

function _setCachedSignalRegistration(phone, registered) {
	const key = normalizePhoneForCache(phone);
	if (!key) return;
	// Allow boolean or null (unavailable)
	if (registered !== true && registered !== false && registered !== null) return;
	_signalStatusCache[key] = { registered, cachedAt: Date.now() };
}

/**
 * Purge expired entries from Signal status cache.
 * @private
 */
function _purgeExpiredCache() {
	const now = Date.now();
	for (const key of Object.keys(_signalStatusCache)) {
		const entry = _signalStatusCache[key];
		if (!entry || typeof entry !== 'object') {
			delete _signalStatusCache[key];
			continue;
		}
		if ((now - (entry.cachedAt || 0)) > SIGNAL_STATUS_TTL_MS) {
			delete _signalStatusCache[key];
		}
	}
}

/**
 * Set Signal status icon for a recipient.
 * @private
 * @param {number} recipientId - Recipient ID
 * @param {string} icon - Material icon name
 * @param {string} cssClass - Color class (text-success, text-warning, etc.)
 * @param {string} title - Tooltip text
 */
function _setSignalIcon(recipientId, icon, cssClass, title) {
	const statusEl = document.getElementById(`signal-status-${recipientId}`);
	const iconEl = statusEl?.querySelector('.signal-status-icon');
	if (!iconEl) return;

	iconEl.textContent = icon;
	iconEl.classList.remove('text-success', 'text-warning', 'text-body-secondary');
	iconEl.classList.add(cssClass);
	iconEl.title = title;
}

function applyCachedSignalStatus(recipients) {
	for (const r of recipients) {
		if (!r.phone) continue;
		const cached = _getCachedSignalRegistration(r.phone);
		// Skip if still pending (undefined)
		if (cached === undefined) continue;

		if (cached === true) {
			_setSignalIcon(r.id, SIGNAL_ICONS.registered, 'text-success', 'Registered with Signal');
		} else if (cached === false) {
			_setSignalIcon(r.id, SIGNAL_ICONS.unregistered, 'text-warning', 'Not registered with Signal');
		} else {
			// null = unavailable (no Signal account linked)
			_setSignalIcon(r.id, SIGNAL_ICONS.unavailable, 'text-body-secondary', 'No Signal account linked');
		}
	}
}

// Load Recipients List
async function loadRecipients() {
	const container = document.getElementById('recipients-list');
	
	// Guard: container might not exist on page (e.g., before HTMX loads fragment)
	if (!container) {
		return;  // Silently skip if container not yet rendered
	}

	// Purge expired cache entries to prevent unbounded growth
	_purgeExpiredCache();
	
	try {
		const response = await window.apiFetch('/recipients');
		if (!response.ok) throw new Error('HTTP ' + response.status);
		
		const recipients = await response.json();
		
		if (recipients.length === 0) {
			container.innerHTML = `
				<div class="text-center text-body-secondary py-4">
					<span class="material-icons mb-2" style="font-size: 48px;">notifications_off</span>
					<p class="mb-0">No recipients configured yet.</p>
				</div>
			`;
			return;
		}
		
		let html = `
			<table class="table table-sm table-hover align-middle mb-0">
				<thead class="table-light">
					<tr>
						<th>Name</th>
						<th>Signal</th>
						<th>Email</th>
						<th class="text-center">Status</th>
						<th class="text-end"><span class="visually-hidden">Actions</span></th>
					</tr>
				</thead>
				<tbody>
		`;
		
			for (const r of recipients) {
				const statusBadge = r.is_enabled 
					? '<span class="badge bg-success">Enabled</span>'
					: '<span class="badge bg-secondary">Disabled</span>';
				
				// Signal column - with placeholder for verification status
				const cachedSignal = _getCachedSignalRegistration(r.phone);
				let signalIcon, signalClass, signalTitle;
				if (cachedSignal === true) {
					signalIcon = SIGNAL_ICONS.registered;
					signalClass = 'text-success';
					signalTitle = 'Registered with Signal';
				} else if (cachedSignal === false) {
					signalIcon = SIGNAL_ICONS.unregistered;
					signalClass = 'text-warning';
					signalTitle = 'Not registered with Signal';
				} else if (cachedSignal === null) {
					signalIcon = SIGNAL_ICONS.unavailable;
					signalClass = 'text-body-secondary';
					signalTitle = 'No Signal account linked';
				} else {
					signalIcon = SIGNAL_ICONS.pending;
					signalClass = 'text-body-secondary';
					signalTitle = 'Checking...';
				}
				const signalCell = r.phone
					? `<span class="d-flex align-items-center gap-1" id="signal-status-${r.id}">
						<span class="material-icons signal-status-icon ${signalClass}" style="font-size: 16px;"
							title="${signalTitle}">${signalIcon}</span>
						<span class="small">${escapeHtml(r.phone)}</span>
					   </span>`
					: '<span class="text-body-secondary">—</span>';
			
			// Email column
			const emailCell = r.email 
				? `<span class="d-flex align-items-center gap-1">
					<span class="material-icons text-success" style="font-size: 16px;">check_circle</span>
					<span class="small">${escapeHtml(r.email)}</span>
				   </span>`
				: '<span class="text-body-secondary">—</span>';
			
				html += `
					<tr data-recipient-id="${r.id}">
						<td data-label="Name">
							<span class="d-flex align-items-center gap-2">
								<span class="material-icons" style="font-size: 20px;">person</span>
								<strong>${escapeHtml(r.name)}</strong>
						</span>
					</td>
					<td data-label="Signal">${signalCell}</td>
					<td data-label="Email">${emailCell}</td>
					<td data-label="Status" class="text-center">${statusBadge}</td>
					<td class="text-end">
						<div class="d-flex justify-content-end gap-1 flex-nowrap">
							<button class="btn btn-sm btn-icon btn-outline-secondary toggle-recipient-btn"
								data-recipient-id="${r.id}"
								data-enabled="${r.is_enabled ? '1' : '0'}"
								title="${r.is_enabled ? 'Pause notifications' : 'Enable notifications'}">
								<span class="material-icons">${r.is_enabled ? 'pause_circle' : 'play_circle'}</span>
							</button>
							<button class="btn btn-sm btn-icon btn-outline-secondary test-recipient-btn"
								data-recipient-id="${r.id}"
								data-recipient-name="${escapeHtml(r.name)}"
								title="Send test message"
								aria-label="Send test message to ${escapeHtml(r.name)}">
								<span class="material-icons" aria-hidden="true">send</span>
							</button>
							<button class="btn btn-sm btn-icon btn-outline-secondary edit-recipient-btn"
								data-recipient-id="${r.id}"
								title="Edit recipient">
								<span class="material-icons">edit</span>
							</button>
							<button class="btn btn-sm btn-icon btn-outline-danger delete-recipient-btn"
								data-recipient-id="${r.id}"
								data-recipient-name="${escapeHtml(r.name)}"
								title="Delete recipient">
								<span class="material-icons">delete</span>
							</button>
						</div>
					</td>
				</tr>
			`;
		}
		
		html += '</tbody></table>';
			container.innerHTML = html;
		
		// Event delegation - remove old listener and re-bind (handles HTMX swaps)
		container.removeEventListener('click', handleRecipientListClick);
		container.addEventListener('click', handleRecipientListClick);
		
			// Apply cached Signal statuses (if any) and verify in background
			applyCachedSignalStatus(recipients);
			verifySignalNumbersInBackground(recipients);
		
	} catch (error) {
		console.error('Load recipients error:', error);
		container.innerHTML = `
			<div class="text-center text-danger py-4">
				<span class="material-icons mb-2" style="font-size: 48px;">error</span>
				<p class="mb-0">Failed to load recipients</p>
			</div>
		`;
	}
}

// Handle clicks on recipient list (event delegation)
async function handleRecipientListClick(e) {
	const editBtn = e.target.closest('.edit-recipient-btn');
	const deleteBtn = e.target.closest('.delete-recipient-btn');
	const toggleBtn = e.target.closest('.toggle-recipient-btn');
	
	const testBtn = e.target.closest('.test-recipient-btn');
	if (testBtn) {
		await sendTestFromList(testBtn);
	} else if (toggleBtn) {
		toggleBtn.disabled = true;
		try {
			await toggleRecipientEnabled(Number(toggleBtn.dataset.recipientId), toggleBtn.dataset.enabled === '1');
		} finally {
			toggleBtn.disabled = false;
		}
	} else if (editBtn) {
		await editRecipient(Number(editBtn.dataset.recipientId));
	} else if (deleteBtn) {
		await deleteRecipient(Number(deleteBtn.dataset.recipientId), deleteBtn.dataset.recipientName);
	}
}

// Verify Signal numbers in background and update UI
async function verifySignalNumbersInBackground(recipients) {
	const phonesToCheck = recipients
		.filter(r => r.phone)
		.map(r => r.phone)
		// Only check if status is undefined (pending) - skip true/false/null (all final)
		.filter(phone => _getCachedSignalRegistration(phone) === undefined);
	
	if (phonesToCheck.length === 0) return;
	
	try {
		const response = await window.apiFetch('/recipients/verify-signal', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(phonesToCheck),
		});
		
		if (!response.ok) {
			// Endpoint might not exist - fall back to showing neutral icons
			console.warn('Signal verification endpoint not available');
			showAllSignalStatusAsUnavailable(recipients);
			return;
		}
		
		const data = await response.json();
		// API returns { results: { phone: bool|null }, invalid: [phone] }
		const results = data.results || data;  // Backwards compat
		for (const [phone, isRegistered] of Object.entries(results || {})) {
			// Accept true, false, or null (unavailable)
			if (isRegistered === true || isRegistered === false || isRegistered === null) {
				_setCachedSignalRegistration(phone, isRegistered);
			}
		}

		// Update UI via the shared renderer (single source of truth)
		applyCachedSignalStatus(recipients);
	} catch (error) {
		console.warn('Failed to verify Signal numbers:', error);
		// Show neutral icons as fallback
		showAllSignalStatusAsUnavailable(recipients);
	}
}

// Fallback: show all Signal statuses as "unknown" when verification API fails
function showAllSignalStatusAsUnavailable(recipients) {
	for (const r of recipients) {
		if (!r.phone) continue;
		// Use 'unknown' icon for API failure (different from 'unavailable' = no Signal account)
		_setSignalIcon(r.id, SIGNAL_ICONS.unknown, 'text-body-secondary', 'Verification failed');
	}
}

// Add Recipient
async function addRecipient() {
	const btn = document.getElementById('add-recipient-btn');
	const errorDiv = document.getElementById('add-recipient-error');
	const form = document.getElementById('add-recipient-form');
	
	if (!btn || !errorDiv || !form) return;
	
	const originalBtnText = btn.innerHTML;
	const name = document.getElementById('recipient-name').value.trim();
	const signalEnabled = document.getElementById('recipient-signal-enabled').checked;
	const emailEnabled = document.getElementById('recipient-email-enabled').checked;
	const phone = document.getElementById('recipient-phone').value.trim();
	const email = document.getElementById('recipient-email').value.trim();
	const isEnabled = document.getElementById('recipient-enabled').checked;
	
	// Validation
	const validationError = validateRecipientInput({ name, signalEnabled, emailEnabled, phone, email });
	if (validationError) {
		errorDiv.textContent = validationError;
		errorDiv.classList.remove('d-none');
		return;
	}
	
	btn.disabled = true;
	errorDiv.classList.add('d-none');
	
	try {
		const payload = { 
			name, 
			is_enabled: isEnabled,
			phone: signalEnabled ? phone : null,
			email: emailEnabled ? email : null
		};
		
		const response = await window.apiFetch('/recipients', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(payload),
		});
		
		if (response.ok) {
			const data = await response.json();
			const modal = document.getElementById('addRecipientModal');
			if (modal) {
				const bsModal = bootstrap.Modal.getInstance(modal);
				if (bsModal) bsModal.hide();
			}
			form.reset();
			loadRecipients();
			
			// Show warning toast if phone not registered with Signal
			if (data.warnings && data.warnings.length > 0) {
				for (const warning of data.warnings) {
					showToast(warning, 'warning');
				}
			} else {
				showToast('Recipient added successfully', 'success');
			}
		} else {
			const errorMsg = await extractApiError(response, 'Failed to add recipient');
			errorDiv.textContent = errorMsg;
			errorDiv.classList.remove('d-none');
		}
	} catch (error) {
		console.error('Add recipient error:', error);
		errorDiv.textContent = 'Network error. Please check your connection.';
		errorDiv.classList.remove('d-none');
	} finally {
		btn.disabled = false;
		btn.innerHTML = originalBtnText;
	}
}

// Edit Recipient - open modal
async function editRecipient(recipientId) {
	try {
		const response = await window.apiFetch(`/recipients/${recipientId}`);
		if (!response.ok) throw new Error('HTTP ' + response.status);
		
		const r = await response.json();
		
		const idInput = document.getElementById('edit-recipient-id');
		const nameInput = document.getElementById('edit-recipient-name');
		const phoneInput = document.getElementById('edit-recipient-phone');
		const emailInput = document.getElementById('edit-recipient-email');
		const signalCheckbox = document.getElementById('edit-recipient-signal-enabled');
		const emailCheckbox = document.getElementById('edit-recipient-email-enabled');
		const enabledInput = document.getElementById('edit-recipient-enabled');
		const errorDiv = document.getElementById('edit-recipient-error');
		
		if (idInput) idInput.value = r.id;
		if (nameInput) nameInput.value = r.name;
		if (enabledInput) enabledInput.checked = r.is_enabled;
		if (errorDiv) errorDiv.classList.add('d-none');
		
		// Set channel data
		if (phoneInput) phoneInput.value = r.phone || '';
		if (emailInput) emailInput.value = r.email || '';
		if (signalCheckbox) signalCheckbox.checked = !!r.phone;
		if (emailCheckbox) emailCheckbox.checked = !!r.email;
		// Test messages use the saved values; remember them to detect unsaved edits
		_editSaved = { phone: r.phone || '', email: r.email || '' };
		_clearTestResult();
		updateEditTestButtons();
		
		const modal = document.getElementById('editRecipientModal');
		if (modal) {
			// Use existing instance or create new one (avoids aria-hidden focus issues)
			const bsModal = bootstrap.Modal.getInstance(modal) || new bootstrap.Modal(modal);
			bsModal.show();
		}
	} catch (error) {
		console.error('Edit recipient error:', error);
		showToast('Failed to load recipient', 'danger');
	}
}

// ─── Test messages ───────────────────────────────────────────────────────────
// The server always tests the SAVED recipient configuration (what alerts will use), so the dialog
// only enables a test button while the field still matches the saved value.
const TEST_POLL_INTERVAL_MS = 2000;
const TEST_POLL_MAX_ATTEMPTS = 25;
const TEST_CHANNEL_LABELS = { signal: 'Signal', email: 'Email', all: 'Test message' };
const _testInFlight = new Set();
let _editSaved = { phone: '', email: '' };

function _sleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

async function _pollTestStatus(recipientId, queueId) {
	for (let attempt = 0; attempt < TEST_POLL_MAX_ATTEMPTS; attempt++) {
		await _sleep(TEST_POLL_INTERVAL_MS);
		const response = await window.apiFetch(`/recipients/${recipientId}/test/${queueId}`);
		if (!response.ok) {
			return { state: 'failed', message: await extractApiError(response, 'Could not read the delivery status') };
		}
		const data = await response.json();
		if (data.state !== 'pending') return data;
	}
	return { state: 'pending', message: 'Still waiting for delivery. Check the Spooler log for the result.' };
}

/**
 * Queue a test message for a saved recipient and wait for the delivery result.
 * @param {number} recipientId
 * @param {'signal'|'email'|'all'} channel
 * @param {{setBusy?: function(boolean), report?: function(Array)}} ui - callbacks for progress/result
 * @returns {Promise<Array|null>} lines [{channel, state: sending|sent|failed|pending, message}], null if already running
 */
async function sendRecipientTest(recipientId, channel, ui = {}) {
	const key = String(recipientId);
	if (_testInFlight.has(key)) return null;  // double-click guard
	_testInFlight.add(key);
	ui.setBusy?.(true);
	const report = (lines) => ui.report?.(lines);
	report([{ channel, state: 'sending', message: 'Sending test message…' }]);
	let lines;
	try {
		const response = await window.apiFetch(`/recipients/${recipientId}/test`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ channel }),
		});
		if (!response.ok) {
			lines = [{ channel, state: 'failed', message: await extractApiError(response, 'Failed to send test message') }];
		} else {
			const { results } = await response.json();
			lines = results.map((r) => ({ channel: r.channel, state: r.state === 'queued' ? 'sending' : 'failed', message: r.message }));
			report(lines);
			lines = await Promise.all(results.map(async (r, i) => {
				if (r.state !== 'queued') return lines[i];
				const final = await _pollTestStatus(recipientId, r.queue_id);
				return { channel: r.channel, state: final.state, message: final.message };
			}));
		}
	} catch (error) {
		console.error('Send test message error:', error);
		lines = [{ channel, state: 'failed', message: 'Network error. Please check your connection.' }];
	} finally {
		_testInFlight.delete(key);
		ui.setBusy?.(false);
	}
	report(lines);
	return lines;
}

const TEST_STATE_VIEW = {
	sending: { icon: 'hourglass_empty', cls: 'text-body-secondary' },
	pending: { icon: 'hourglass_empty', cls: 'text-warning' },
	sent: { icon: 'check_circle', cls: 'text-success' },
	failed: { icon: 'error', cls: 'text-danger' },
};

function _clearTestResult() {
	document.getElementById('edit-recipient-test-result')?.replaceChildren();
}

// Result lines inside the aria-live region of the dialog (textContent only, no HTML injection)
function renderTestResult(lines) {
	const box = document.getElementById('edit-recipient-test-result');
	if (!box) return;
	box.replaceChildren();
	for (const line of lines) {
		const view = TEST_STATE_VIEW[line.state] || TEST_STATE_VIEW.failed;
		const row = document.createElement('div');
		row.className = `d-flex align-items-start gap-1 ${view.cls}`;
		const icon = document.createElement('span');
		icon.className = 'material-icons';
		icon.style.fontSize = '16px';
		icon.setAttribute('aria-hidden', 'true');
		icon.textContent = view.icon;
		const text = document.createElement('span');
		text.textContent = `${TEST_CHANNEL_LABELS[line.channel] || line.channel}: ${line.message}`;
		row.append(icon, text);
		box.appendChild(row);
	}
}

// Enable a channel test only while its toggle is on and the field equals the saved value
function updateEditTestButtons() {
	const busy = _testInFlight.has(String(document.getElementById('edit-recipient-id')?.value));
	const testable = {};
	let dirty = false;
	for (const [channel, field] of [['signal', 'phone'], ['email', 'email']]) {
		const checked = document.getElementById(`edit-recipient-${channel}-enabled`)?.checked ?? false;
		const value = document.getElementById(`edit-recipient-${field}`)?.value?.trim() ?? '';
		testable[channel] = checked && !!_editSaved[field] && value === _editSaved[field];
		if ((_editSaved[field] || value) && !testable[channel]) dirty = true;
		const btn = document.getElementById(`edit-recipient-test-${channel}`);
		if (btn) btn.disabled = busy || !testable[channel];
	}
	const allBtn = document.getElementById('edit-recipient-test-all');
	if (allBtn) allBtn.disabled = busy || !(testable.signal || testable.email);
	const hint = document.getElementById('edit-recipient-test-hint');
	if (hint) {
		hint.textContent = dirty
			? 'A channel is unsaved or switched off: tests use the saved values. Save changes first.'
			: 'Tests use the saved channels. Save changes first to test new values.';
	}
}

async function sendTestFromDialog(channel) {
	const recipientId = Number(document.getElementById('edit-recipient-id')?.value);
	if (!recipientId) return;
	await sendRecipientTest(recipientId, channel, {
		setBusy: () => updateEditTestButtons(),
		report: renderTestResult,
	});
}

async function sendTestFromList(btn) {
	const recipientId = Number(btn.dataset.recipientId);
	const name = btn.dataset.recipientName || 'recipient';
	await sendRecipientTest(recipientId, 'all', {
		setBusy: (busy) => { btn.disabled = busy; },
		report: (lines) => {
			for (const line of lines) {
				if (line.state === 'sending') continue;
				const label = TEST_CHANNEL_LABELS[line.channel] || line.channel;
				showToast(`${name} · ${label}: ${line.message}`, line.state === 'sent' ? 'success' : 'warning');
			}
		},
	});
}

// Save Recipient edits
async function saveRecipient() {
	const btn = document.getElementById('edit-recipient-btn');
	const errorDiv = document.getElementById('edit-recipient-error');

	// Guard: required DOM elements
	if (!btn || !errorDiv) {
		console.error('saveRecipient: required DOM elements not found');
		return;
	}

	const originalBtnText = btn.innerHTML;
	
	const recipientId = document.getElementById('edit-recipient-id')?.value;
	const name = document.getElementById('edit-recipient-name')?.value?.trim();
	const signalEnabled = document.getElementById('edit-recipient-signal-enabled')?.checked ?? false;
	const emailEnabled = document.getElementById('edit-recipient-email-enabled')?.checked ?? false;
	const phone = document.getElementById('edit-recipient-phone')?.value?.trim() ?? '';
	const email = document.getElementById('edit-recipient-email')?.value?.trim() ?? '';
	const isEnabled = document.getElementById('edit-recipient-enabled')?.checked ?? true;

	if (!recipientId || !name) {
		errorDiv.textContent = 'Missing required fields';
		errorDiv.classList.remove('d-none');
		return;
	}
	
	// Validation
	const validationError = validateRecipientInput({ name, signalEnabled, emailEnabled, phone, email });
	if (validationError) {
		errorDiv.textContent = validationError;
		errorDiv.classList.remove('d-none');
		return;
	}
	
	btn.disabled = true;
	errorDiv.classList.add('d-none');
	
	try {
		const payload = {
			name,
			is_enabled: isEnabled,
			phone: signalEnabled ? phone : null,
			email: emailEnabled ? email : null
		};
		
		const response = await window.apiFetch(`/recipients/${recipientId}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(payload),
		});
		
		if (response.ok) {
			const data = await response.json();
			bootstrap.Modal.getInstance(document.getElementById('editRecipientModal'))?.hide();
			loadRecipients();
			
			// Show warning toast if phone not registered with Signal
			if (data.warnings && data.warnings.length > 0) {
				for (const warning of data.warnings) {
					showToast(warning, 'warning');
				}
			} else {
				showToast('Recipient updated successfully', 'success');
			}
		} else {
			const errorMsg = await extractApiError(response, 'Failed to update recipient');
			errorDiv.textContent = errorMsg;
			errorDiv.classList.remove('d-none');
		}
	} catch (error) {
		errorDiv.textContent = 'Network error: ' + error.message;
		errorDiv.classList.remove('d-none');
	} finally {
		btn.disabled = false;
		btn.innerHTML = originalBtnText;
	}
}

// Ask for confirmation in a Bootstrap modal; resolves true only on explicit confirm.
function confirmDeleteRecipient(recipientName) {
	const modalEl = document.getElementById('deleteRecipientModal');
	const confirmBtn = document.getElementById('confirm-delete-recipient-btn');
	if (!modalEl || !confirmBtn || !window.bootstrap?.Modal) return Promise.resolve(false);
	document.getElementById('delete-recipient-name').textContent = `"${recipientName}"`;
	const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
	return new Promise((resolve) => {
		let confirmed = false;
		const onConfirm = () => {
			confirmed = true;
			modal.hide();
		};
		confirmBtn.addEventListener('click', onConfirm, { once: true });
		modalEl.addEventListener('hidden.bs.modal', () => {
			confirmBtn.removeEventListener('click', onConfirm);
			resolve(confirmed);
		}, { once: true });
		modal.show();
	});
}

// Delete Recipient
async function deleteRecipient(recipientId, recipientName) {
	if (!(await confirmDeleteRecipient(recipientName))) {
		return;
	}

	try {
		// Use apiFetch for proper auth/CSRF handling
		const response = await window.apiFetch(`/recipients/${recipientId}`, { method: 'DELETE' });
		
		if (response.ok) {
			loadRecipients();
			showToast('Recipient deleted', 'success');
		} else {
			showToast('Failed to delete recipient', 'danger');
		}
	} catch (error) {
		console.error('Delete recipient error:', error);
		showToast('Network error. Please check your connection.', 'danger');
	}
}

// Toggle Recipient Enabled/Disabled
async function toggleRecipientEnabled(recipientId, currentlyEnabled) {
	try {
		const response = await window.apiFetch(`/recipients/${recipientId}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ is_enabled: !currentlyEnabled }),
		});
		
		if (response.ok) {
			loadRecipients();
			showToast(currentlyEnabled ? 'Recipient paused' : 'Recipient enabled', 'success');
		} else {
			showToast('Failed to update recipient', 'danger');
		}
	} catch (error) {
		console.error('Toggle recipient error:', error);
		showToast('Network error. Please check your connection.', 'danger');
	}
}

// Utility: Show Toast with appropriate icon (XSS-safe)
function showToast(message, type = 'success') {
	// Use global toast system if available (consistent with rest of app)
	if (window.justUpToast) {
		const method = type === 'danger' ? 'error' : type;
		if (window.justUpToast[method]) {
			window.justUpToast[method](message);
			return;
		}
	}

	// Fallback: local toast implementation
	const toastEl = document.getElementById('settings-toast');
	if (!toastEl) return;
	
	const toastBody = toastEl.querySelector('.toast-body');
	if (!toastBody) return;
	
	// Map type to icon
	const icons = {
		success: 'check_circle',
		warning: 'warning',
		danger: 'error'
	};
	const icon = icons[type] || 'info';
	
	// XSS-safe: build DOM nodes instead of innerHTML
	// Normalize: info -> success (no cyan toasts)
	const normalizedType = (type === 'info') ? 'success' : type;
	toastEl.className = `toast align-items-center text-bg-${normalizedType} border-0`;
	toastBody.innerHTML = '';  // Clear previous content
	
	const iconSpan = document.createElement('span');
	iconSpan.className = 'material-icons me-2';
	iconSpan.textContent = icon;
	
	const textSpan = document.createElement('span');
	textSpan.textContent = message;  // Safe: uses textContent
	
	toastBody.appendChild(iconSpan);
	toastBody.appendChild(textSpan);
	
	const toast = new bootstrap.Toast(toastEl);
	toast.show();
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
	// Load recipients when HTMX settles the recipients fragment
	document.body.addEventListener('htmx:afterSettle', (event) => {
		if (event.target.id === 'recipients-pane' || event.target.closest('#recipients-pane')) {
			loadRecipients();
		}
	});
	
	// Add recipient button
	const addRecipientBtn = document.getElementById('add-recipient-btn');
	if (addRecipientBtn) {
		addRecipientBtn.addEventListener('click', addRecipient);
	}
	
	// Edit recipient button
	const editRecipientBtn = document.getElementById('edit-recipient-btn');
	if (editRecipientBtn) {
		editRecipientBtn.addEventListener('click', saveRecipient);
	}
	
	// Test message buttons and unsaved-change detection in the edit dialog
	for (const btn of document.querySelectorAll('[data-test-channel]')) {
		btn.addEventListener('click', () => sendTestFromDialog(btn.dataset.testChannel));
	}
	for (const id of ['phone', 'email', 'signal-enabled', 'email-enabled']) {
		const input = document.getElementById(`edit-recipient-${id}`);
		if (input) {
			input.addEventListener('input', updateEditTestButtons);
			input.addEventListener('change', updateEditTestButtons);
		}
	}
	// Form submit prevention
	const addRecipientForm = document.getElementById('add-recipient-form');
	if (addRecipientForm) {
		addRecipientForm.addEventListener('submit', (e) => {
			e.preventDefault();
			addRecipient();
		});
	}
	
	const editRecipientForm = document.getElementById('edit-recipient-form');
	if (editRecipientForm) {
		editRecipientForm.addEventListener('submit', (e) => {
			e.preventDefault();
			saveRecipient();
		});
	}
});

// Export for global access (namespaced)
window.SettingsPage = window.SettingsPage || {};
window.SettingsPage.Recipients = {
	loadRecipients,
	addRecipient,
	editRecipient,
	saveRecipient,
	deleteRecipient,
	sendRecipientTest,
	updateEditTestButtons,
};
