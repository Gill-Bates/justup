//
// app/static/js/users.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

let usersTable;
let userForm;
let userModal;
let userSaveBtn;
let editingUserId = null;

document.addEventListener('DOMContentLoaded', () => {
    usersTable = document.getElementById('users-body');
    userForm = document.getElementById('user-form');
    userSaveBtn = document.getElementById('saveUserBtn');

    const modalEl = document.getElementById('userModal');
    if (modalEl) userModal = new bootstrap.Modal(modalEl);

    const addBtn = document.getElementById('addUserBtn');
    if (addBtn) addBtn.addEventListener('click', () => openUserModal());

    if (userSaveBtn) userSaveBtn.addEventListener('click', saveUser);

    // Event delegation for edit and delete buttons
    document.addEventListener('click', (e) => {
        const editBtn = e.target.closest('.edit-user');
        if (editBtn) {
            e.preventDefault();
            const id = parseInt(editBtn.dataset.id);
            if (id) editUser(id);
            return;
        }

        const deleteBtn = e.target.closest('.delete-user');
        if (deleteBtn) {
            e.preventDefault();
            const id = parseInt(deleteBtn.dataset.id);
            const username = deleteBtn.dataset.username;
            if (id) deleteUser(id, username);
            return;
        }
    });

    loadUsers();
});

function openUserModal(user = null) {
    editingUserId = user ? user.id : null;
    const title = document.getElementById('userModalTitle');
    title.textContent = user ? 'Edit User' : 'Add User';

    // Get current user ID from page data attribute
    const pageEl = document.querySelector('.ju-page');
    const currentUserId = pageEl?.dataset?.currentUserId ? parseInt(pageEl.dataset.currentUserId) : null;

    // Show change password section only when editing own user
    const changePasswordSection = document.getElementById('change-password-section');
    const isEditingOwnUser = user && currentUserId && user.id === currentUserId;

    if (changePasswordSection) {
        if (isEditingOwnUser) {
            changePasswordSection.classList.remove('d-none');
        } else {
            changePasswordSection.classList.add('d-none');
        }
    }

    document.getElementById('user-username').value = user?.username || '';
    document.getElementById('user-password').value = '';
    document.getElementById('user-is-admin').checked = user ? user.is_admin : false;

    // Clear password change fields
    const currentPwField = document.getElementById('user-current-password');
    const newPwField = document.getElementById('user-new-password');
    if (currentPwField) currentPwField.value = '';
    if (newPwField) newPwField.value = '';

    const pwField = document.getElementById('user-password');
    pwField.required = !user;
    pwField.placeholder = user ? '(leave empty to keep)' : 'Password';

    userModal.show();
}

async function loadUsers() {
    try {
        const users = await api('GET', '/api/users');
        renderUsers(users || []);
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

function renderUsers(users) {
    if (!usersTable) return;

    if (users.length === 0) {
        usersTable.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">No users found.</td></tr>';
        return;
    }

    usersTable.innerHTML = users.map(u => {
        const adminBadge = u.is_admin
            ? '<span class="badge bg-warning text-dark">Admin</span>'
            : '<span class="badge bg-secondary">User</span>';

        const statusBadge = u.is_active
            ? '<span class="badge bg-success">Active</span>'
            : '<span class="badge bg-secondary">Inactive</span>';

        const mfaBadge = u.otp_enabled
            ? '<span class="badge bg-info">Enabled</span>'
            : '<span class="badge bg-secondary">Disabled</span>';

        const lastLogin = u.last_login_at || '–';

        // Admin user (ID 1) cannot be deleted
        const isSystemAdmin = u.id === 1;
        const deleteBtn = isSystemAdmin
            ? '<button class="btn btn-sm btn-outline-secondary" disabled title="System admin cannot be deleted"><span class="material-icons icon-sm">delete</span></button>'
            : `<button class="btn btn-sm btn-outline-danger delete-user" data-id="${u.id}" data-username="${escapeHtml(u.username)}" title="Delete"><span class="material-icons icon-sm">delete</span></button>`;

        return `<tr>
            <td>${escapeHtml(u.username)}</td>
            <td>${adminBadge}</td>
            <td>${statusBadge}</td>
            <td>${mfaBadge}</td>
            <td>${lastLogin}</td>
            <td>
                <button class="btn btn-sm btn-outline-primary edit-user" data-id="${u.id}" title="Edit">
                    <span class="material-icons icon-sm">edit</span>
                </button>
                ${deleteBtn}
            </td>
        </tr>`;
    }).join('');
}

function escapeHtml(text) {
    const el = document.createElement('span');
    el.textContent = text;
    return el.innerHTML;
}

async function saveUser() {
    const username = document.getElementById('user-username').value.trim();
    const password = document.getElementById('user-password').value;
    const is_admin = document.getElementById('user-is-admin').checked;

    // Get password change fields
    const currentPassword = document.getElementById('user-current-password')?.value || '';
    const newPassword = document.getElementById('user-new-password')?.value || '';

    if (!username) {
        juToast('Username is required', 'warning');
        return;
    }

    try {
        if (editingUserId) {
            const data = { username, is_admin };
            if (password) data.password = password;

            // Handle password change if current user is editing their own account
            const pageEl = document.querySelector('.ju-page');
            const currentUserId = pageEl?.dataset?.currentUserId ? parseInt(pageEl.dataset.currentUserId) : null;
            const isEditingOwnUser = currentUserId && editingUserId === currentUserId;

            if (isEditingOwnUser && currentPassword && newPassword) {
                // Validate new password length
                if (newPassword.length < 8) {
                    juToast('New password must be at least 8 characters', 'warning');
                    return;
                }

                // Change password first
                try {
                    await api('POST', `/api/users/${editingUserId}/change-password`, {
                        current_password: currentPassword,
                        new_password: newPassword,
                    });
                    juToast('Password changed successfully', 'success');
                } catch (err) {
                    juToast(`Password change failed: ${err.message}`, 'danger');
                    return;
                }
            }

            await api('PUT', `/api/users/${editingUserId}`, data);
            juToast('User updated', 'success');
        } else {
            if (!password) {
                juToast('Password is required for new users', 'warning');
                return;
            }
            await api('POST', '/api/users', { username, password, is_admin });
            juToast('User created', 'success');
        }
        userModal.hide();
        await loadUsers();
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

async function editUser(id) {
    try {
        const users = await api('GET', '/api/users');
        const user = (users || []).find(u => u.id === id);
        if (!user) {
            juToast('User not found', 'danger');
            return;
        }
        openUserModal(user);
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

async function deleteUser(id, username) {
    const confirmed = await juConfirm(`Delete user "${username}"?`, 'danger');
    if (!confirmed) return;

    try {
        await api('DELETE', `/api/users/${id}`);
        juToast('User deleted', 'success');
        await loadUsers();
    } catch (err) {
        juToast(err.message, 'danger');
    }
}
