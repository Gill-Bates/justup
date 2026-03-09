//
// app/static/js/settings.users.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// User Management and SMTP Functions for Settings Page

// Constants for error messages (prevent backend leakage)
const ERROR_MESSAGES = {
	SMTP_TEST_FAILED: 'SMTP test failed. Please check your configuration.',
	LOAD_USERS_FAILED: 'Failed to load users',
	CREATE_USER_FAILED: 'Failed to create user',
	UPDATE_USER_FAILED: 'Failed to update user',
	TOGGLE_USER_FAILED: 'Failed to change user status',
	DELETE_USER_FAILED: 'Failed to delete user',
	NETWORK_ERROR: 'Network error. Please check your connection.',
	REQUIRED_FIELDS: 'Please fill in all required fields',
	PASSWORD_TOO_SHORT: 'Password must be at least 8 characters',
	PASSWORDS_DONT_MATCH: 'Passwords do not match',
};

let _usersCache = [];

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
			input.classList.remove('is-invalid');
			// Remove validation on input
			if (!input.dataset.smtpValidation) {
				input.dataset.smtpValidation = '1';
				input.addEventListener('input', () => input.classList.remove('is-invalid'));
			}
		}
	}

	// Check required fields
	for (const fieldId of SMTP_REQUIRED_FIELDS) {
		const input = document.getElementById(fieldId);
		if (!input?.value?.trim()) {
			missing.push(fieldId);
			input?.classList.add('is-invalid');
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
		window.justUpToast?.warning?.('Please fill in the required SMTP fields (Host, From Address)');
		document.getElementById(missingFields[0])?.focus();
		return;
	}

	// Validate recipient
	const recipient = recipientInput?.value?.trim();
	if (!recipient) {
		window.justUpToast?.warning?.('Please enter a test recipient email address');
		recipientInput?.classList.add('is-invalid');
		recipientInput?.focus();
		return;
	}
	recipientInput?.classList.remove('is-invalid');

	btn.disabled = true;
	const originalText = btn.innerHTML;
	btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Queuing...';

	try {
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
			const errorMsg = result.detail || result.error || ERROR_MESSAGES.SMTP_TEST_FAILED;
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
		recipientInput.addEventListener('input', () => recipientInput.classList.remove('is-invalid'));
	}
}

// Load Users List
async function loadUsers() {
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
		_usersCache = Array.isArray(users) ? users : [];

		if (_usersCache.length === 0) {
			container.innerHTML = `
				<div class="text-center text-muted py-4">
					<span class="material-icons mb-2" style="font-size: 48px;">person_off</span>
					<p class="mb-0">No users configured yet.</p>
				</div>
			`;
			return;
		}

		let html = `
			<table class="table table-hover align-middle mb-0">
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
			// Role badge - use warning color for Admin (better dark mode visibility)
			const roleBadge = user.is_admin
				? '<span class="badge bg-warning text-dark">Admin</span>'
				: '<span class="badge bg-secondary">User</span>';

			// Can close alerts indicator
			const canCloseAlerts = user.is_admin || user.can_close_alerts;
			const closeAlertsIcon = canCloseAlerts
				? '<span class="material-icons text-success" title="Can close alerts">check_circle</span>'
				: '<span class="material-icons text-muted" title="Cannot close alerts">remove_circle_outline</span>';

			const isActive = user.is_active !== false;
			const statusBadge = isActive
				? '<span class="badge bg-success">Enabled</span>'
				: '<span class="badge bg-secondary">Disabled</span>';

			const disableToggle = !!user.is_admin;
			const disableDelete = !!user.is_admin;

			// Last login - format timestamp and IP or show dash
			let lastLoginCell;
			if (user.last_login_at) {
				const timeSpan = `<span class="small local-time" data-utc="${user.last_login_at}">${user.last_login_at.substring(0, 16).replace('T', ' ')} UTC</span>`;
				const ipSpan = user.last_login_ip
					? `<br><span class="small text-muted">${escapeHtml(user.last_login_ip)}</span>`
					: '';
				lastLoginCell = timeSpan + ipSpan;
			} else {
				lastLoginCell = '<span class="text-muted">—</span>';
			}



			html += `
				<tr data-user-id="${user.id}">
					<td data-label="Username">
						<span class="d-flex align-items-center gap-2">
							<span class="material-icons" style="font-size: 20px;">person</span>
							<strong>${escapeHtml(user.username)}</strong>
						</span>
					</td>
					<td data-label="Role">${roleBadge}</td>
					<td data-label="Close Alerts" class="text-center">${closeAlertsIcon}</td>
					<td data-label="Status" class="text-center">${statusBadge}</td>
					<td data-label="Last Login">${lastLoginCell}</td>
					<td class="text-end">
						<div class="d-flex justify-content-end gap-1 flex-nowrap">
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

		// Convert local-time elements
		container.querySelectorAll('.local-time').forEach(el => {
			const utc = el.dataset.utc;
			if (utc) {
				try {
					const d = new Date(utc);
					el.textContent = d.toLocaleString();
				} catch (e) { }
			}
		});

	} catch (error) {
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

	if (!userId || !username) return;

	if (newPassword || confirmPassword) {
		if ((newPassword || '').length < 8) {
			if (errorDiv) {
				errorDiv.textContent = ERROR_MESSAGES.PASSWORD_TOO_SHORT;
				errorDiv.classList.remove('d-none');
			}
			return;
		}
		if (newPassword !== confirmPassword) {
			if (errorDiv) {
				errorDiv.textContent = ERROR_MESSAGES.PASSWORDS_DONT_MATCH;
				errorDiv.classList.remove('d-none');
			}
			return;
		}
	}

	if (btn) btn.disabled = true;
	if (errorDiv) errorDiv.classList.add('d-none');

	try {
		const body = {
			username,
			is_admin: role === 'admin',
			can_close_alerts: canCloseAlerts || false,
		};
		if (newPassword) body.new_password = newPassword;

		const response = await window.apiFetch(`/users/${userId}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		});

		if (response.ok) {
			const modalEl = document.getElementById('editUserModal');
			if (modalEl) {
				const modal = bootstrap.Modal.getInstance(modalEl);
				if (modal) modal.hide();
			}
			await loadUsers();
			showToast('User updated', 'success');
		} else {
			let errorMsg = ERROR_MESSAGES.UPDATE_USER_FAILED;
			try {
				const data = await response.json();
				errorMsg = data.detail || data.error || errorMsg;
			} catch (e) { }
			if (errorDiv) {
				errorDiv.textContent = errorMsg;
				errorDiv.classList.remove('d-none');
			}
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
			showToast(isActive ? 'User activated' : 'User deactivated', 'success');
		} else {
			let msg = ERROR_MESSAGES.TOGGLE_USER_FAILED;
			try {
				const data = await response.json();
				msg = data.detail || data.error || msg;
			} catch (e) { }
			showToast(msg, 'danger');
		}
	} catch (error) {
		console.error('Toggle user error:', error);
		showToast(ERROR_MESSAGES.NETWORK_ERROR, 'danger');
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

	const username = document.getElementById('new-user-username').value.trim();
	const password = document.getElementById('new-user-password').value;
	const isAdmin = document.getElementById('new-user-is-admin').checked;
	const canCloseAlerts = document.getElementById('new-user-can-close-alerts')?.checked || false;

	// Validation
	if (!username || !password) {
		errorDiv.textContent = ERROR_MESSAGES.REQUIRED_FIELDS;
		errorDiv.classList.remove('d-none');
		return;
	}

	// Frontend password strength check
	if (password.length < 8) {
		errorDiv.textContent = ERROR_MESSAGES.PASSWORD_TOO_SHORT;
		errorDiv.classList.remove('d-none');
		return;
	}

	btn.disabled = true;
	errorDiv.classList.add('d-none');

	try {
		const response = await window.apiFetch('/users', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ username, password, is_admin: isAdmin, can_close_alerts: canCloseAlerts }),
		});

		if (response.ok) {
			const modal = document.getElementById('createUserModal');
			if (modal) {
				const bsModal = bootstrap.Modal.getInstance(modal);
				if (bsModal) bsModal.hide();
			}
			form.reset();
			loadUsers();
			showToast('User created successfully', 'success');
		} else {
			// Map backend errors to user-friendly messages
			let errorMsg = ERROR_MESSAGES.CREATE_USER_FAILED;
			try {
				const error = await response.json();
				if (error.detail && error.detail.includes('already exists')) {
					errorMsg = 'Username already exists';
				}
			} catch (parseError) {
				console.warn('Failed to parse error response');
			}
			errorDiv.textContent = errorMsg;
			errorDiv.classList.remove('d-none');
		}
	} catch (error) {
		console.error('Create user error:', error);
		errorDiv.textContent = ERROR_MESSAGES.NETWORK_ERROR;
		errorDiv.classList.remove('d-none');
	} finally {
		btn.disabled = false;
	}
}

// Delete User
async function deleteUser(userId, username) {
	// TODO: Replace confirm() with Bootstrap modal for better UX consistency
	if (!confirm(`Delete user "${username}"? This cannot be undone.`)) {
		return;
	}

	try {
		const response = await window.apiFetch(`/users/${userId}`, { method: 'DELETE' });

		if (response.ok) {
			loadUsers();
			showToast('User deleted', 'success');
		} else {
			showToast(ERROR_MESSAGES.DELETE_USER_FAILED, 'danger');
		}
	} catch (error) {
		console.error('Delete user error:', error);
		showToast(ERROR_MESSAGES.NETWORK_ERROR, 'danger');
	}
}

// Utility: Escape HTML
function escapeHtml(text) {
	const div = document.createElement('div');
	div.textContent = text;
	return div.innerHTML;
}

// Utility: Show Toast with appropriate icon
// Uses DOM APIs to prevent XSS from backend messages
function showToast(message, type = 'success') {
	const toastEl = document.getElementById('settings-toast');
	if (!toastEl) return;

	const toastBody = toastEl.querySelector('.toast-body');
	if (!toastBody) return;

	// Icon based on type
	const iconName = type === 'success' ? 'check_circle' : 'error';

	// Normalize: info -> success (no cyan toasts)
	const normalizedType = (type === 'info') ? 'success' : type;
	toastEl.className = `toast align-items-center text-bg-${normalizedType} border-0`;

	// Build content safely using DOM APIs (prevents XSS)
	toastBody.innerHTML = '';
	const iconEl = document.createElement('span');
	iconEl.className = 'material-icons me-2';
	iconEl.textContent = iconName;

	const textEl = document.createElement('span');
	textEl.textContent = message;

	toastBody.append(iconEl, textEl);

	const toast = new bootstrap.Toast(toastEl);
	toast.show();
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
	// Load users on page load (only if container exists)
	if (document.getElementById('users-list')) {
		initUsersHandlers();
		loadUsers();
	}

	// Create user button
	const createUserBtn = document.getElementById('create-user-btn');
	if (createUserBtn) {
		createUserBtn.addEventListener('click', createUser);
	}

	// Form submit prevention
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

// Initialize password change handler after HTMX loads the users fragment
document.body.addEventListener('htmx:afterSettle', (e) => {
	// Check if the users pane was just loaded
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
};
