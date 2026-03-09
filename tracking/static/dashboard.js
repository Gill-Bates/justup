//
// tracking/static/dashboard.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Dashboard interactions (CSP-compliant, no inline scripts).
 */
(function () {
    'use strict';

    const modal = document.getElementById('purge-modal');
    if (!modal) return;

    const openBtn = document.getElementById('purge-open-btn');
    const cancelBtn = document.getElementById('purge-cancel-btn');
    const purgeForm = modal.querySelector('form');
    const purgeSubmitBtn = purgeForm ? purgeForm.querySelector('button[type="submit"]') : null;

    let isPurging = false;

    /**
     * Open modal with focus management
     */
    function openModal() {
        modal.classList.add('active');

        // Focus first focusable element (cancel button)
        const firstFocusable = modal.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        if (firstFocusable) {
            firstFocusable.focus();
        }
    }

    /**
     * Close modal and restore focus
     */
    function closeModal() {
        modal.classList.remove('active');

        // Return focus to trigger button
        if (openBtn) {
            openBtn.focus();
        }
    }

    // Open modal button
    if (openBtn) {
        openBtn.addEventListener('click', openModal);
    }

    // Cancel button
    if (cancelBtn) {
        cancelBtn.addEventListener('click', closeModal);
    }

    // Close on backdrop click
    modal.addEventListener('click', function (e) {
        if (e.target === modal) {
            closeModal();
        }
    });

    // Close on Escape key (only when modal is active)
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && modal.classList.contains('active')) {
            closeModal();
        }
    });

    // Prevent double-submit on purge form
    if (purgeForm) {
        purgeForm.addEventListener('submit', function (e) {
            if (isPurging) {
                e.preventDefault();
                return false;
            }

            isPurging = true;

            // Disable submit button and show feedback
            if (purgeSubmitBtn) {
                purgeSubmitBtn.disabled = true;
                purgeSubmitBtn.textContent = '⏳ Deleting...';
            }
        });
    }
})();
