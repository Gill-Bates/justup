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
    usersTable = document.getElementById('users-tbody');
    userForm = document.getElementById('user-form');
    userSaveBtn = document.getElementById('user-save-btn');

    const modalEl = document.getElementById('userModal');
    if (modalEl) userModal = new bootstrap.Modal(modalEl);

    const addBtn = document.getElementById('add-user-btn');
    if (addBtn) addBtn.addEventListener('click', () => openUserModal());

    if (userSaveBtn) userSaveBtn.addEventListener('click', saveUser);

    loadUsers();
});

function openUserModal(user = null) {
    editingUserId = user ? user.id : null;
    const title = document.getElementById('userModalLabel');
    title.textContent = user ? 'Edit User' : 'Add User';

    document.getElementById('user-username').value = user?.username || '';
    document.getElementById('user-password').value = '';
    document.getElementById('user-is-admin').checked = user ? user.is_admin : false;

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
        usersTable.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">No users found.</td></tr>';
        return;
    }

    usersTable.innerHTML = users.map(u => {
        const adminBadge = u.is_admin
            ? '<span class="badge bg-warning text-dark">Admin</span>'
            : '<span class="badge bg-secondary">User</span>';

        const otpBadge = u.otp_enabled
            ? '<span class="badge bg-success">OTP</span>'
            : '';

        return `<tr>
            <td>${escapeHtml(u.username)}</td>
            <td>${adminBadge} ${otpBadge}</td>
            <td>${u.created_at || '-'}</td>
            <td>
                <button class="btn btn-sm btn-outline-primary me-1" onclick="editUser(${u.id})" title="Edit">
                    <span class="material-icons" style="font-size:16px">edit</span>
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="deleteUser(${u.id}, '${escapeHtml(u.username)}')" title="Delete">
                    <span class="material-icons" style="font-size:16px">delete</span>
                </button>
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

    if (!username) {
        juToast('Username is required', 'warning');
        return;
    }

    try {
        if (editingUserId) {
            const data = { username, is_admin };
            if (password) data.password = password;
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
