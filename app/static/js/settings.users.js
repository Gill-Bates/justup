//
// app/static/js/settings.users.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// User Management and SMTP Functions for Settings Page

// Constants for error messages (prevent backend leakage)
// The password limit is the inputs' minlength attribute (rendered from the server constant).
function passwordMinLength(inputId) {
	const length = document.getElementById(inputId)?.minLength;
	return length > 0 ? length : 8;
}
function passwordTooShort(inputId) {
	return 'Password must be at least ' + passwordMinLength(inputId) + ' characters';
}
const ERROR_MESSAGES = {
	SMTP_TEST_FAILED: 'SMTP test failed. Please check your configuration.',
	LOAD_USERS_FAILED: 'Failed to load users',
	CREATE_USER_FAILED: 'Failed to create user',
	UPDATE_USER_FAILED: 'Failed to update user',
	TOGGLE_USER_FAILED: 'Failed to change user status',
	DELETE_USER_FAILED: 'Failed to delete user',
	REVOKE_SESSIONS_FAILED: 'Failed to revoke sessions',
	NETWORK_ERROR: 'Network error. Please check your connection.',
	REQUIRED_FIELDS: 'Please fill in all required fields',
	PASSWORDS_DONT_MATCH: 'Passwords do not match',
};

let _usersCache = [];

// ─── Notification channels of the user's recipient (create and edit dialogs) ─────────────────
// Field ids are `<prefix>-signal-enabled|phone|email-enabled|email|recipient-enabled|all-targets`.
// The channel switches reuse the recipient dialog's behaviour (window.SettingsPage.Recipients.syncChannelFields).

function channelValues(prefix) {
	const byId = (suffix) => document.getElementById(`${prefix}-${suffix}`);
	return {
		signalEnabled: Boolean(byId('signal-enabled')?.checked),
		emailEnabled: Boolean(byId('email-enabled')?.checked),
		phone: byId('phone')?.value?.trim() ?? '',
		email: byId('email')?.value?.trim() ?? '',
		recipientEnabled: Boolean(byId('recipient-enabled')?.checked),
		allTargets: Boolean(byId('all-targets')?.checked),
	};
}

// Request part sent in the same call as the user; a switched-off channel is null, and a recipient without any
// channel is sent as disabled (the server refuses "enabled" without a channel).
function recipientConfig(prefix) {
	const v = channelValues(prefix);
	const phone = v.signalEnabled ? v.phone : null;
	const email = v.emailEnabled ? v.email : null;
	return {
		phone,
		email,
		is_enabled: Boolean(phone || email) && v.recipientEnabled,
		enable_for_all_targets: v.allTargets,
	};
}

function channelFieldErrors(prefix) {
	const v = channelValues(prefix);
	const errors = [];
	if (v.signalEnabled && !v.phone) errors.push({ id: `${prefix}-phone`, message: 'Please enter a Signal phone number' });
	if (v.emailEnabled && !v.email) errors.push({ id: `${prefix}-email`, message: 'Please enter an email address' });
	return errors;
}

// Show/hide the channel fields and keep "Recipient Enabled" consistent: without an active channel it is off and
// disabled; it comes back on when a channel is switched on, but only if this code had switched it off.
function syncUserChannels(modalEl, prefix) {
	if (!modalEl) return;
	window.SettingsPage?.Recipients?.syncChannelFields?.(modalEl);
	const enabled = document.getElementById(`${prefix}-recipient-enabled`);
	if (!enabled) return;
	const anyChannel = channelValues(prefix).signalEnabled || channelValues(prefix).emailEnabled;
	if (!anyChannel) {
		if (enabled.checked) enabled.dataset.autoOff = '1';
		enabled.checked = false;
		enabled.disabled = true;
	} else {
		enabled.disabled = false;
		if (enabled.dataset.autoOff === '1') enabled.checked = true;
		delete enabled.dataset.autoOff;
	}
}

function fillChannels(prefix, recipient) {
	const set = (suffix, apply) => {
		const el = document.getElementById(`${prefix}-${suffix}`);
		if (el) apply(el);
	};
	const r = recipient || {};
	set('signal-enabled', (el) => { el.checked = Boolean(r.phone); });
	set('phone', (el) => { el.value = r.phone || ''; });
	set('email-enabled', (el) => { el.checked = Boolean(r.email); });
	set('email', (el) => { el.value = r.email || ''; });
	set('recipient-enabled', (el) => { el.checked = Boolean(r.is_enabled); delete el.dataset.autoOff; });
	set('all-targets', (el) => { el.checked = Boolean(r.enable_for_all_targets); });
}

// A phone conflict from the server belongs to the phone field, everything else to the banner.
function showUserSaveError(modalId, prefix, errorDiv, message) {
	if (/phone/i.test(message) && document.getElementById(`${prefix}-phone`)) {
		const shown = window.JuFieldErrors.show(document.getElementById(modalId), [{ id: `${prefix}-phone`, message }]);
		if (shown) return;
	}
	if (errorDiv) {
		errorDiv.textContent = message;
		errorDiv.classList.remove('d-none');
	}
}

// Saved state of the edit dialog; switching "Enable for all Targets" off removes every assignment.
let _editUserSaved = { allTargets: false };

// Required SMTP fields for test validation
const SMTP_REQUIRED_FIELDS = ['smtp_host', 'smtp_from'];

/**
 * Validate SMTP configuration fields before test.
 * Highlights missing fields with is-invalid class.
 * @returns {string[]} Array of missing field IDs (empty if valid)
 */
function validateSmtpFields() {
	const missing = [];

	// Clear previous validation state
	for (const fieldId of SMTP_REQUIRED_FIELDS) {
		const input = document.getElementById(fieldId);
		if (input) {
			window.JuFieldErrors.clear(input);
			// Remove validation on input
			if (!input.dataset.smtpValidation) {
				input.dataset.smtpValidation = '1';
				input.addEventListener('input', () => window.JuFieldErrors.clear(input));
			}
		}
	}

	for (const fieldId of SMTP_REQUIRED_FIELDS) {
		const input = document.getElementById(fieldId);
		if (!input?.value?.trim()) {
			missing.push(fieldId);
			if (input) window.JuFieldErrors.set(input, 'This field is required.');
		}
	}

	return missing;
}

// Test SMTP Configuration
async function testSmtp() {
	const btn = document.getElementById('test-smtp-btn');
	const recipientInput = document.getElementById('smtp_test_recipient');

	if (!btn) return;

	// Validate required SMTP config fields first
	const missingFields = validateSmtpFields();
	if (missingFields.length > 0) {
		document.getElementById(missingFields[0])?.focus();
		return;
	}

	const recipient = recipientInput?.value?.trim();
	if (!recipient) {
		window.JuFieldErrors.show(recipientInput.closest('form') || document.body, [
			{ id: 'smtp_test_recipient', message: 'Please enter a test recipient email address' },
		]);
		return;
	}
	window.JuFieldErrors.clear(recipientInput);

	btn.disabled = true;
	const originalText = btn.innerHTML;
	btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Queuing...';

	try {
		// A failed save means the test would otherwise use stale server-side settings.
		const saveResults = await window.justUpSettingsSavePending;
		if (Array.isArray(saveResults) && saveResults.some((saved) => saved === false)) {
			window.justUpToast?.error?.('Could not save settings. Test email was not sent.');
			return;
		}

		const response = await window.apiFetch('/settings/smtp/test-mail', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ recipient }),
		});

		// Safe JSON parsing (backend might return HTML on 500/502)
		let result = {};
		try {
			result = await response.json();
		} catch (parseError) {
			console.warn('Failed to parse SMTP test response:', parseError);
		}

		if (response.ok && result.ok !== false) {
			window.justUpToast?.success?.(result.message || 'Test email queued');
		} else {
			const errorMsg = window.JuApiError.fromBody(result, ERROR_MESSAGES.SMTP_TEST_FAILED);
			window.justUpToast?.error?.(errorMsg);
		}
	} catch (error) {
		console.error('SMTP test error:', error);
		window.justUpToast?.error?.(ERROR_MESSAGES.NETWORK_ERROR);
	} finally {
		btn.disabled = false;
		btn.innerHTML = originalText;
	}
}

function initSmtpTestHandlers(root) {
	const scope = root || document;
	const btn = scope.querySelector('#test-smtp-btn');
	if (!btn || btn.dataset.bound) return;
	btn.dataset.bound = '1';
	btn.addEventListener('click', (e) => {
		e.preventDefault();
		testSmtp();
	});

	// Clear validation on recipient input
	const recipientInput = scope.querySelector('#smtp_test_recipient');
	if (recipientInput && !recipientInput.dataset.smtpValidation) {
		recipientInput.dataset.smtpValidation = '1';
		recipientInput.addEventListener('input', () => window.JuFieldErrors.clear(recipientInput));
	}
}

// Load Users List; a newer call supersedes the responses of older ones
let usersGeneration = 0;

async function loadUsers() {
	const generation = ++usersGeneration;
	const container = document.getElementById('users-list');

	// Guard: container might not exist on page
	if (!container) {
		console.warn('users-list container not found, skipping loadUsers');
		return;
	}

	try {
		const response = await window.apiFetch('/users');
		if (!response.ok) throw new Error('HTTP ' + response.status);

		const users = await response.json();
		if (generation !== usersGeneration) return;
		_usersCache = Array.isArray(users) ? users : [];

		if (_usersCache.length === 0) {
			container.innerHTML = `
				<div class="text-center text-body-secondary py-4">
					<span class="material-icons mb-2" style="font-size: 48px;">person_off</span>
					<p class="mb-0">No users configured yet.</p>
				</div>
			`;
			return;
		}

		let html = `
			<table class="table table-sm table-hover align-middle rtable ju-data-table mb-0">
				<thead class="table-light">
					<tr>
						<th>Username</th>
						<th>Role</th>
						<th class="text-center">Close Alerts</th>
						<th class="text-center">Status</th>
						<th>Last Login</th>
						<th class="text-end"><span class="visually-hidden">Actions</span></th>
					</tr>
				</thead>
				<tbody>
		`;

		for (const user of _usersCache) {
			// Role badge (DC-BADGE-5: neutral count-style badge)
			const roleBadge = user.is_admin
				? '<span class="badge bg-secondary">Admin</span>'
				: '<span class="badge bg-secondary">User</span>';

			// Can close alerts indicator
			const canCloseAlerts = user.is_admin || user.can_close_alerts;
			const closeAlertsIcon = canCloseAlerts
				? '<span class="material-icons text-body-secondary" role="img" aria-label="Can close alerts" title="Can close alerts">check_circle</span>'
				: '<span class="material-icons text-body-secondary" role="img" aria-label="Cannot close alerts" title="Cannot close alerts">remove_circle_outline</span>';

			const isActive = user.is_active !== false;
			const statusBadge = isActive
				? JuStatusDot.html('success', 'Enabled')
				: JuStatusDot.html('neutral', 'Disabled');

			const disableToggle = !!user.is_admin;
			const disableDelete = !!user.is_admin;

			// Last login - format timestamp and IP or show dash
			let lastLoginCell;
			if (user.last_login_at) {
				const timeSpan = `<span class="small local-time" data-utc="${escapeAttr(user.last_login_at)}">${escapeHtml(user.last_login_at.substring(0, 16).replace('T', ' '))} UTC</span>`;
				const ipSpan = user.last_login_ip
					? `<br class="user-login-br"><span class="small text-body-secondary user-login-ip">${escapeHtml(user.last_login_ip)}</span>`
					: '';
				lastLoginCell = timeSpan + ipSpan;
			} else {
				lastLoginCell = '<span class="text-body-secondary">—</span>';
			}



			html += `
				<tr data-user-id="${user.id}">
					<td data-label="Username" class="user-cell ju-cell-text">
						<span class="d-flex align-items-center gap-2">
							<span class="material-icons" style="font-size: 20px;" aria-hidden="true">person</span>
							<strong>${escapeHtml(user.username)}</strong>
						</span>
					</td>
					<td data-label="Role" class="user-role">${roleBadge}</td>
					<td data-label="Close Alerts" class="text-center user-close">${closeAlertsIcon}</td>
					<td data-label="Status" class="text-center user-status">${statusBadge}</td>
					<td data-label="Last Login" class="user-login">${lastLoginCell}</td>
					<td data-label="" class="text-end actions-cell user-actions">
						<div class="d-flex justify-content-end gap-1 flex-nowrap user-action-buttons">
							<button class="btn btn-sm btn-icon btn-outline-secondary toggle-user-btn"
								data-user-id="${user.id}"
								data-is-active="${isActive ? '1' : '0'}"
								${disableToggle ? 'disabled' : ''}
								title="${disableToggle ? 'Admin users cannot be disabled' : (isActive ? 'Disable user' : 'Enable user')}">
								<span class="material-icons">${isActive ? 'pause_circle' : 'play_circle'}</span>
							</button>
							<button class="btn btn-sm btn-icon btn-outline-secondary edit-user-btn"
								data-user-id="${user.id}"
								title="Edit user">
								<span class="material-icons">edit</span>
							</button>
							<button class="btn btn-sm btn-icon btn-outline-warning revoke-sessions-btn"
								data-user-id="${user.id}"
								data-username="${escapeHtml(user.username)}"
								title="Revoke all sessions"
								aria-label="Revoke all sessions of ${escapeHtml(user.username)}">
								<span class="material-icons" aria-hidden="true">logout</span>
							</button>
							<button class="btn btn-sm btn-icon btn-outline-danger delete-user-btn"
								data-user-id="${user.id}"
								data-username="${escapeHtml(user.username)}"
								${disableDelete ? 'disabled' : ''}
								title="${disableDelete ? 'Admin users cannot be deleted' : 'Delete user'}">
								<span class="material-icons">delete</span>
							</button>
						</div>
					</td>
				</tr>
			`;
		}

		html += '</tbody></table>';
		container.innerHTML = html;

		// Format timestamps with the shared rule (honours the UTC display setting)
		window.JuLocalTime?.init(container);

	} catch (error) {
		if (generation !== usersGeneration) return;
		console.error('Load users error:', error);
		container.innerHTML = '<div class="text-center text-danger py-4">' + ERROR_MESSAGES.LOAD_USERS_FAILED + '</div>';
	}
}

function initUsersHandlers() {
	const container = document.getElementById('users-list');
	if (!container || container.dataset.handlersInitialized) return;
	container.dataset.handlersInitialized = 'true';

	container.addEventListener('click', (e) => {
		const editBtn = e.target.closest('.edit-user-btn');
		if (editBtn) {
			openEditUserModal(editBtn.dataset.userId);
			return;
		}

		const toggleBtn = e.target.closest('.toggle-user-btn');
		if (toggleBtn) {
			const userId = toggleBtn.dataset.userId;
			const isActive = toggleBtn.dataset.isActive === '1';
			toggleUserActive(userId, !isActive, toggleBtn);
			return;
		}

		const revokeBtn = e.target.closest('.revoke-sessions-btn');
		if (revokeBtn) {
			revokeUserSessions(revokeBtn.dataset.userId, revokeBtn.dataset.username);
			return;
		}

		const deleteBtn = e.target.closest('.delete-user-btn');
		if (deleteBtn) {
			deleteUser(deleteBtn.dataset.userId, deleteBtn.dataset.username);
		}
	});

	const editUserForm = document.getElementById('edit-user-form');
	if (editUserForm && !editUserForm.dataset.handlersInitialized) {
		editUserForm.dataset.handlersInitialized = 'true';
		editUserForm.addEventListener('submit', (e) => {
			e.preventDefault();
			saveEditedUser();
		});
	}
}

function openEditUserModal(userId) {
	const modalEl = document.getElementById('editUserModal');
	if (!modalEl) return;

	const user = _usersCache.find(u => String(u.id) === String(userId));
	if (!user) return;

	document.getElementById('edit-user-id').value = user.id;
	document.getElementById('edit-user-username').value = user.username || '';
	document.getElementById('edit-user-role').value = user.is_admin ? 'admin' : 'user';
	document.getElementById('edit-user-can-close-alerts').checked = user.is_admin || user.can_close_alerts;
	document.getElementById('edit-user-password').value = '';
	document.getElementById('edit-user-password-confirm').value = '';
	fillChannels('edit-user', user.recipient);
	_editUserSaved = { allTargets: Boolean(user.recipient?.enable_for_all_targets) };

	// Show/disable can_close_alerts toggle based on admin status
	const closeAlertsContainer = document.getElementById('edit-user-close-alerts-container');
	const closeAlertsCheckbox = document.getElementById('edit-user-can-close-alerts');
	if (closeAlertsContainer && closeAlertsCheckbox) {
		// Always show, but disable for admins (they always have this permission)
		closeAlertsContainer.style.display = 'block';
		closeAlertsCheckbox.disabled = user.is_admin;
		if (user.is_admin) {
			closeAlertsCheckbox.checked = true;
		}
	}

	const errorDiv = document.getElementById('edit-user-error');
	if (errorDiv) errorDiv.classList.add('d-none');

	syncUserChannels(modalEl, 'edit-user');
	const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
	modal.show();
}

async function saveEditedUser() {
	const btn = document.getElementById('edit-user-btn');
	const errorDiv = document.getElementById('edit-user-error');
	const userId = document.getElementById('edit-user-id')?.value;
	const username = document.getElementById('edit-user-username')?.value?.trim();
	const role = document.getElementById('edit-user-role')?.value;
	const canCloseAlerts = document.getElementById('edit-user-can-close-alerts')?.checked;
	const newPassword = document.getElementById('edit-user-password')?.value;
	const confirmPassword = document.getElementById('edit-user-password-confirm')?.value;

	if (!userId) return;
	if (errorDiv) errorDiv.classList.add('d-none');
	const fieldErrors = [];
	if (!username) fieldErrors.push({ id: 'edit-user-username', message: 'Please enter a username' });
	if (newPassword || confirmPassword) {
		if ((newPassword || '').length < passwordMinLength('edit-user-password')) {
			fieldErrors.push({ id: 'edit-user-password', message: passwordTooShort('edit-user-password') });
		} else if (newPassword !== confirmPassword) {
			fieldErrors.push({ id: 'edit-user-password-confirm', message: ERROR_MESSAGES.PASSWORDS_DONT_MATCH });
		}
	}
	fieldErrors.push(...channelFieldErrors('edit-user'));
	if (window.JuFieldErrors.show(document.getElementById('editUserModal'), fieldErrors)) return;

	if (btn) btn.disabled = true;
	if (errorDiv) errorDiv.classList.add('d-none');

	try {
		const recipient = recipientConfig('edit-user');
		if (_editUserSaved.allTargets && !recipient.enable_for_all_targets) {
			// Same rule and wording as the recipient dialog: the server removes every assignment of the recipient.
			const confirmed = await window.confirmAction({
				title: 'Remove recipient from all targets?',
				message: 'Saving with "Enable for all Targets" off removes this user\'s recipient from all targets, including manually assigned targets.',
				confirmLabel: 'Remove from all targets',
			});
			if (!confirmed) return;
		}
		const body = {
			username,
			is_admin: role === 'admin',
			can_close_alerts: canCloseAlerts || false,
			recipient,
		};
		if (newPassword) body.new_password = newPassword;

		const response = await window.apiFetch(`/users/${userId}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		});

		if (response.ok) {
			bootstrap.Modal.getInstance(document.getElementById('editUserModal'))?.hide();
			await loadUsers();
			window.justUpToast.show('User updated', 'success');
		} else {
			const errorMsg = await window.JuApiError.fromResponse(response, ERROR_MESSAGES.UPDATE_USER_FAILED);
			showUserSaveError('editUserModal', 'edit-user', errorDiv, errorMsg);
		}
	} catch (error) {
		console.error('Update user error:', error);
		if (errorDiv) {
			errorDiv.textContent = ERROR_MESSAGES.NETWORK_ERROR;
			errorDiv.classList.remove('d-none');
		}
	} finally {
		if (btn) btn.disabled = false;
	}
}

async function toggleUserActive(userId, isActive, toggleBtn = null) {
	// Find the button if not provided (for busy guard)
	if (!toggleBtn) {
		toggleBtn = document.querySelector(`.toggle-user-btn[data-user-id="${userId}"]`);
	}

	// Busy guard: prevent double-clicks and race conditions
	if (toggleBtn) {
		if (toggleBtn.disabled) return;
		toggleBtn.disabled = true;
	}

	try {
		const response = await window.apiFetch(`/users/${userId}/active`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ is_active: !!isActive }),
		});
		if (response.ok) {
			await loadUsers();
			window.justUpToast.show(isActive ? 'User activated' : 'User deactivated', 'success');
		} else {
			const msg = await window.JuApiError.fromResponse(response, ERROR_MESSAGES.TOGGLE_USER_FAILED);
			window.justUpToast.show(msg, 'danger');
		}
	} catch (error) {
		console.error('Toggle user error:', error);
		window.justUpToast.show(ERROR_MESSAGES.NETWORK_ERROR, 'danger');
	} finally {
		// Re-enable button (loadUsers will rebuild the DOM anyway)
		if (toggleBtn) toggleBtn.disabled = false;
	}
}

// Create User
async function createUser() {
	const btn = document.getElementById('create-user-btn');
	const errorDiv = document.getElementById('create-user-error');
	const form = document.getElementById('create-user-form');

	if (!btn || !errorDiv || !form) return;
	if (btn.disabled) return;

	const username = document.getElementById('new-user-username').value.trim();
	const password = document.getElementById('new-user-password').value;
	const isAdmin = document.getElementById('new-user-is-admin').checked;
	const canCloseAlerts = document.getElementById('new-user-can-close-alerts')?.checked || false;

	errorDiv.classList.add('d-none');
	const fieldErrors = [];
	if (!username) fieldErrors.push({ id: 'new-user-username', message: 'Please enter a username' });
	if (!password) fieldErrors.push({ id: 'new-user-password', message: 'Please enter a password' });
	else if (password.length < passwordMinLength('new-user-password')) fieldErrors.push({ id: 'new-user-password', message: passwordTooShort('new-user-password') });
	fieldErrors.push(...channelFieldErrors('new-user'));
	if (window.JuFieldErrors.show(form, fieldErrors)) return;

	btn.disabled = true;
	errorDiv.classList.add('d-none');

	try {
		const response = await window.apiFetch('/users', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				username,
				password,
				is_admin: isAdmin,
				can_close_alerts: canCloseAlerts,
				recipient: recipientConfig('new-user'),
			}),
		});

		if (response.ok) {
			bootstrap.Modal.getInstance(document.getElementById('createUserModal'))?.hide();
			form.reset();
			loadUsers();
			window.justUpToast.show('User created successfully', 'success');
		} else {
			// Map backend errors to user-friendly messages
			const errorMsg = await window.JuApiError.fromResponse(response, ERROR_MESSAGES.CREATE_USER_FAILED);
			showUserSaveError('createUserModal', 'new-user', errorDiv, errorMsg);
		}
	} catch (error) {
		console.error('Create user error:', error);
		errorDiv.textContent = ERROR_MESSAGES.NETWORK_ERROR;
		errorDiv.classList.remove('d-none');
	} finally {
		btn.disabled = false;
	}
}

// Ask for confirmation in a Bootstrap modal; resolves true only on explicit confirm.
function confirmDeleteUser(username) {
	return window.confirmViaModal({ modalId: 'deleteUserModal', confirmId: 'confirm-delete-user-btn', nameId: 'delete-user-name', name: username });
}

// Ask for confirmation before ending all sessions of a user; resolves true only on explicit confirm.
function confirmRevokeSessions(username) {
	return window.confirmViaModal({ modalId: 'revokeSessionsModal', confirmId: 'confirm-revoke-sessions-btn', nameId: 'revoke-sessions-user-name', name: username });
}

async function revokeUserSessions(userId, username) {
	if (!(await confirmRevokeSessions(username))) {
		return;
	}
	try {
		const response = await window.apiFetch(`/users/${userId}/revoke-sessions`, { method: 'POST' });
		if (response.ok) {
			const { revoked } = await response.json();
			window.justUpToast.show(`Revoked ${revoked} session(s)`, 'success');
		} else {
			window.justUpToast.show(await window.JuApiError.fromResponse(response, ERROR_MESSAGES.REVOKE_SESSIONS_FAILED), 'danger');
		}
	} catch (error) {
		console.error('Revoke sessions error:', error);
		window.justUpToast.show(ERROR_MESSAGES.NETWORK_ERROR, 'danger');
	}
}

// Delete User
async function deleteUser(userId, username) {
	if (!(await confirmDeleteUser(username))) {
		return;
	}

	try {
		const response = await window.apiFetch(`/users/${userId}`, { method: 'DELETE' });

		if (response.ok) {
			loadUsers();
			window.justUpToast.show('User deleted', 'success');
		} else {
			window.justUpToast.show(ERROR_MESSAGES.DELETE_USER_FAILED, 'danger');
		}
	} catch (error) {
		console.error('Delete user error:', error);
		window.justUpToast.show(ERROR_MESSAGES.NETWORK_ERROR, 'danger');
	}
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
	// Load users on page load (only if container exists)
	if (document.getElementById('users-list')) {
		initUsersHandlers();
		loadUsers();
	}

	// The submit button is type="submit" form=..., so the form submit is the only entry point
	const createUserForm = document.getElementById('create-user-form');
	if (createUserForm) {
		createUserForm.addEventListener('submit', (e) => {
			e.preventDefault();
			createUser();
		});
	}

	// Toggle can_close_alerts visibility based on admin checkbox (Create Modal)
	const newUserIsAdmin = document.getElementById('new-user-is-admin');
	const newUserCloseAlertsContainer = document.getElementById('new-user-close-alerts-container');
	if (newUserIsAdmin && newUserCloseAlertsContainer) {
		newUserIsAdmin.addEventListener('change', () => {
			newUserCloseAlertsContainer.style.display = newUserIsAdmin.checked ? 'none' : 'block';
			if (newUserIsAdmin.checked) {
				document.getElementById('new-user-can-close-alerts').checked = true;
			}
		});
	}

	// Ensure Create User modal resets permission toggle state on close/cancel
	const createUserModalEl = document.getElementById('createUserModal');
	if (createUserModalEl) {
		createUserModalEl.addEventListener('hidden.bs.modal', () => {
			const form = document.getElementById('create-user-form');
			form?.reset?.();

			// After reset, ensure the permission toggle is visible again
			if (newUserCloseAlertsContainer) {
				newUserCloseAlertsContainer.style.display = 'block';
			}
			const closeAlertsCheckbox = document.getElementById('new-user-can-close-alerts');
			if (closeAlertsCheckbox) {
				closeAlertsCheckbox.checked = false;
			}
			delete document.getElementById('new-user-recipient-enabled')?.dataset.autoOff;
			syncUserChannels(createUserModalEl, 'new-user');
		});
	}

	// Channel switches of both dialogs: show/hide the fields and keep "Recipient Enabled" consistent.
	for (const [modalId, prefix] of [['createUserModal', 'new-user'], ['editUserModal', 'edit-user']]) {
		const dialog = document.getElementById(modalId);
		if (!dialog) continue;
		dialog.addEventListener('show.bs.modal', () => syncUserChannels(dialog, prefix));
		dialog.addEventListener('change', (e) => {
			if (e.target.matches('.ju-channel input[role="switch"]')) syncUserChannels(dialog, prefix);
		});
	}

	// Toggle can_close_alerts state based on role select (Edit Modal)
	const editUserRole = document.getElementById('edit-user-role');
	const editUserCloseAlertsCheckbox = document.getElementById('edit-user-can-close-alerts');
	if (editUserRole && editUserCloseAlertsCheckbox) {
		editUserRole.addEventListener('change', () => {
			const isAdmin = editUserRole.value === 'admin';
			editUserCloseAlertsCheckbox.disabled = isAdmin;
			if (isAdmin) {
				editUserCloseAlertsCheckbox.checked = true;
			}
		});
	}

	// SMTP pane may already be present (non-HTMX or cached)
	initSmtpTestHandlers(document);
});

// Bind the users and SMTP handlers once HTMX settles their panes
document.body.addEventListener('htmx:afterSettle', (e) => {
	if (e.target.id === 'users-pane' || e.target.closest('#users-pane')) {
		initUsersHandlers();
		loadUsers();
	}
	if (e.target.id === 'smtp-pane' || e.target.closest('#smtp-pane')) {
		initSmtpTestHandlers(e.target);
	}
});

// Export for global access (namespaced to avoid pollution)
window.SettingsPage = window.SettingsPage || {};
window.SettingsPage.Users = {
	testSmtp,
	loadUsers,
	createUser,
	deleteUser,
	toggleUserActive,
	openEditUserModal,
	saveEditedUser,
	recipientConfig,
	syncUserChannels,
};
