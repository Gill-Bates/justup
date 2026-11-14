//
// app/static/js/groups.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/** Groups page: create, edit and delete dialogs. */
(function () {
	'use strict';
	document.getElementById('newGroupButton')?.addEventListener('click', () => openGroupModal());
	document.getElementById('groupForm')?.addEventListener('submit', (event) => {
		event.preventDefault();
		saveGroup();
	});
	document.getElementById('groups-table')?.addEventListener('click', (event) => {
		const row = event.target.closest('tr[data-group-id]');
		if (!row) return;
		if (event.target.closest('.js-edit-group')) {
			openGroupModal(row.dataset.groupId, row.dataset.groupName, row.dataset.groupDescription);
		} else if (event.target.closest('.js-delete-group')) {
			const name = row.dataset.groupName;
			window.confirmAction({ title: 'Delete group', message: `Group '${name}' is deleted.`, confirmLabel: 'Delete' }).then((ok) => { if (ok) deleteGroup(row.dataset.groupId); });
		}
	});

	function openGroupModal(id = null, name = '', description = '') {
		document.getElementById('groupId').value = id || '';
		document.getElementById('groupName').value = name;
		document.getElementById('groupDescription').value = description;
		document.getElementById('groupModalTitle').textContent = id ? 'Edit Group' : 'New Group';
		showModalError('');
	}
	function showModalError(message) {
		const box = document.getElementById('groupModalError');
		if (!box) return;
		box.textContent = message;
		box.classList.toggle('d-none', !message);
	}

	async function saveGroup() {
		const saveButton = document.getElementById('saveGroupButton');
		if (saveButton?.disabled) return;
		const id = document.getElementById('groupId').value;
		const name = document.getElementById('groupName').value.trim();
		const description = document.getElementById('groupDescription').value.trim();

		showModalError('');
		if (window.JuFieldErrors.show(document.getElementById('groupModal'), name ? [] : [{ id: 'groupName', message: 'Please enter a group name' }])) {
			return;
		}

		const method = id ? 'PATCH' : 'POST';
		const url = id ? `/groups/${id}` : '/groups';

		if (saveButton) saveButton.disabled = true;
		try {
			const res = await window.apiFetch(url, {
				method: method,
				headers: {
					'Content-Type': 'application/json'
				},
				body: JSON.stringify({ name, description: description || null })
			});

			if (!res.ok) {
				showModalError(await window.JuApiError.fromResponse(res, 'Failed to save group'));
				return;
			}

			location.reload();
		} catch (e) {
			showModalError('Error saving group: ' + e.message);
		} finally {
			if (saveButton) saveButton.disabled = false;
		}
	}

	async function deleteGroup(id) {
		try {
			const res = await window.apiFetch(`/groups/${id}`, {
				method: 'DELETE',
			});
			if (!res.ok) {
				window.justUpToast?.error?.(await window.JuApiError.fromResponse(res, 'Failed to delete group'));
				return;
			}
			location.reload();
		} catch (e) {
			window.justUpToast?.error?.('Error deleting group: ' + e.message);
		}
	}
})();
