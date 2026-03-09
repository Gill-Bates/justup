//
// app/static/js/settings.smtp-template.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
  'use strict';

  // ─────────────────────────────────────────────────────────────────────────────
  // WARNING: This WYSIWYG editor uses document.execCommand, which is DEPRECATED.
  // Chrome/Firefox/Safari are phasing it out. Migration to a modern editing
  // library (Trix, Quill, ProseMirror, TinyMCE) is required before browser removal.
  // Track: https://developer.chrome.com/blog/deprecating-document-execcommand/
  // ─────────────────────────────────────────────────────────────────────────────

  // Preview placeholder URL (matches LOGO_URL_PREVIEW from backend)
  var LOGO_URL_PREVIEW = '/static/justup_1c.png';
  
  // NOTE: Must match the placeholder syntax used by the server-side
  // email renderer in app/utils/email_template.py or similar
  var LOGO_PLACEHOLDER_RE = /\{\{logo_url\}\}/g;

  // Track current mode and WYSIWYG initialization
  let currentMode = 'preview';
  let wysiwygInitialized = false;

  function setStatus(text, kind) {
    const status = document.getElementById('template-status');
    if (!status) return;

    status.classList.remove('text-success', 'text-danger', 'text-warning', 'text-info');
    if (kind) status.classList.add(kind);
    status.textContent = text || '';
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // WYSIWYG Editor
  // ─────────────────────────────────────────────────────────────────────────────

  function getWysiwygDocument() {
    const frame = document.getElementById('wysiwyg_frame');
    return frame?.contentDocument || frame?.contentWindow?.document;
  }

  function initWysiwyg() {
    if (wysiwygInitialized) return;

    const frame = document.getElementById('wysiwyg_frame');
    if (!frame) return;

    // Wait for iframe to load then initialize
    if (frame.contentDocument?.readyState !== 'complete') {
      frame.addEventListener('load', initWysiwyg, { once: true });
      return;
    }

    const doc = getWysiwygDocument();
    if (!doc) return;

    // Set up contenteditable document using srcdoc (already in HTML)
    // Add styles to head
    if (!doc.querySelector('style[data-wysiwyg]')) {
      const style = doc.createElement('style');
      style.setAttribute('data-wysiwyg', '1');

    // Respect current theme for editor background
    var isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    style.textContent = `
      html, body {
        background-color: ${isDark ? '#212529' : '#ffffff'} !important;
        color: ${isDark ? '#dee2e6' : '#212529'} !important;
      }
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 14px;
        line-height: 1.5;
        padding: 12px;
        margin: 0;
        min-height: 100%;
      }
      body:focus { outline: none; }
      a { color: #0d6efd; }
      img { max-width: 100%; height: auto; }
      table { border-collapse: collapse; width: 100%; }
      td, th { border: 1px solid #ddd; padding: 8px; }
    `;
    doc.head.appendChild(style);
  }

  // Make body contenteditable
  if (doc.body) {
    doc.body.contentEditable = 'true';
      
      // Load initial content from source textarea
      const source = document.getElementById('email_html_source');
      if (source) {
        doc.body.innerHTML = source.value || '';
      }
    }

    // Bind toolbar buttons
    bindToolbar();

    wysiwygInitialized = true;
  }

  function bindToolbar() {
    const toolbar = document.querySelector('.wysiwyg-toolbar');
    if (!toolbar || toolbar.dataset.bound) return;
    toolbar.dataset.bound = '1';

    // Button commands
    toolbar.querySelectorAll('button[data-cmd]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cmd = btn.dataset.cmd;
        const doc = getWysiwygDocument();
        if (!doc) return;

        if (cmd === 'createLink') {
          var url = prompt('Enter URL:', 'https://');
          if (url) {
            // Block javascript: and data: URLs to prevent XSS
            var normalized = url.trim().toLowerCase();
            if (normalized.startsWith('javascript:') || normalized.startsWith('data:')) {
              alert('Invalid URL protocol');
              return;
            }
            doc.execCommand(cmd, false, url);
          }
        } else {
          doc.execCommand(cmd, false, null);
        }
        
        // Keep focus in editor
        doc.body?.focus();
      });
    });

    // Select commands (formatBlock)
    toolbar.querySelectorAll('select[data-cmd]').forEach(sel => {
      sel.addEventListener('change', () => {
        const cmd = sel.dataset.cmd;
        const value = sel.value;
        const doc = getWysiwygDocument();
        if (!doc) return;

        if (value) {
          doc.execCommand(cmd, false, `<${value}>`);
        } else {
          doc.execCommand('removeFormat', false, null);
        }
        
        sel.value = ''; // Reset select
        doc.body?.focus();
      });
    });
  }

  function syncWysiwygToSource() {
    const doc = getWysiwygDocument();
    const source = document.getElementById('email_html_source');
    if (doc?.body && source) {
      source.value = doc.body.innerHTML;
    }
  }

  function syncSourceToWysiwyg() {
    const doc = getWysiwygDocument();
    const source = document.getElementById('email_html_source');
    if (doc?.body && source) {
      doc.body.innerHTML = source.value || '';
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Template HTML Getters/Setters
  // ─────────────────────────────────────────────────────────────────────────────

  function getCurrentTemplateHtml() {
    // Always sync from WYSIWYG if it was active
    if (currentMode === 'edit') {
      syncWysiwygToSource();
    }
    const htmlSource = document.getElementById('email_html_source');
    return htmlSource ? (htmlSource.value || '') : '';
  }

  function setTemplateHtml(html) {
    const htmlSource = document.getElementById('email_html_source');
    if (htmlSource) htmlSource.value = html || '';
    
    // Also update WYSIWYG if initialized
    if (wysiwygInitialized) {
      syncSourceToWysiwyg();
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Preview
  // ─────────────────────────────────────────────────────────────────────────────

  /**
   * Sanitize HTML for preview - strip scripts and event handlers
   * NOTE: Server-side sanitization is still required before sending emails
   */
  function sanitizeForPreview(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    
    // Remove all script tags
    doc.querySelectorAll('script').forEach(function (el) {
      el.remove();
    });
    
    // Remove all event handler attributes (onclick, onerror, etc.)
    doc.querySelectorAll('*').forEach(function (el) {
      Array.from(el.attributes).forEach(function (attr) {
        if (attr.name.startsWith('on')) {
          el.removeAttribute(attr.name);
        }
      });
    });
    
    return doc.body.innerHTML;
  }

  function updatePreview() {
    const previewFrame = document.getElementById('email_preview_frame');
    if (!previewFrame) return;

    var html = getCurrentTemplateHtml();
    // Replace logo_url placeholder with preview URL
    var renderedHtml = html.replace(LOGO_PLACEHOLDER_RE, LOGO_URL_PREVIEW);
    
    // Sanitize and use srcdoc for preview (defense in depth)
    previewFrame.srcdoc = sanitizeForPreview(renderedHtml);
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Mode Switching
  // ─────────────────────────────────────────────────────────────────────────────

  function switchEditorMode(mode) {
    const wysiwygContainer = document.getElementById('wysiwyg-container');
    const htmlContainer = document.getElementById('html-source-container');
    const previewContainer = document.getElementById('preview-container');
    const btnEdit = document.getElementById('btn-mode-edit');
    const btnHtml = document.getElementById('btn-mode-html');
    const btnPreview = document.getElementById('btn-mode-preview');

    // Sync before switching away from a mode
    if (currentMode === 'edit' && mode !== 'edit') {
      syncWysiwygToSource();
    }

    // Hide all containers first
    if (wysiwygContainer) wysiwygContainer.style.display = 'none';
    if (htmlContainer) htmlContainer.style.display = 'none';
    if (previewContainer) previewContainer.style.display = 'none';

    // Remove active from all buttons
    btnEdit?.classList.remove('active');
    btnHtml?.classList.remove('active');
    btnPreview?.classList.remove('active');

    currentMode = mode;

    if (mode === 'edit') {
      // WYSIWYG editor
      if (wysiwygContainer) wysiwygContainer.style.display = 'block';
      btnEdit?.classList.add('active');
      
      // Initialize WYSIWYG on first use
      if (!wysiwygInitialized) {
        initWysiwyg();
      } else {
        // Sync source to WYSIWYG
        syncSourceToWysiwyg();
      }
    } else if (mode === 'html') {
      // Source mode
      if (htmlContainer) htmlContainer.style.display = 'block';
      btnHtml?.classList.add('active');
      document.getElementById('email_html_source')?.focus();
    } else {
      // Preview mode (default)
      if (previewContainer) previewContainer.style.display = 'block';
      btnPreview?.classList.add('active');
      updatePreview();
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Save / Reset
  // ─────────────────────────────────────────────────────────────────────────────

  async function saveTemplate() {
    const saveBtn = document.getElementById('save-template-btn');
    if (saveBtn && saveBtn.disabled) return;
    
    const subjectInput = document.getElementById('email_subject');
    if (!subjectInput) return;

    const subject = (subjectInput.value || '').trim();
    const html = getCurrentTemplateHtml();

    if (!subject) {
      setStatus('Subject line cannot be empty', 'text-warning');
      window.justUpToast?.error?.('Subject line cannot be empty');
      return;
    }

    if (saveBtn) saveBtn.disabled = true;
    setStatus('Saving…', 'text-info');
    try {
      const resp = await window.apiFetch('/settings/smtp/template', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject, html }),
      });

      if (!resp.ok) {
        setStatus('Failed to save template', 'text-danger');
        window.justUpToast?.error?.('Failed to save template');
        return;
      }

      setStatus('Saved', 'text-success');
      window.justUpToast?.success?.('Template saved');
    } catch (e) {
      console.warn('Template save failed:', e);
      setStatus('Error saving template', 'text-danger');
      window.justUpToast?.error?.('Error saving template');
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function resetTemplate() {
    if (!confirm('Reset email template to default? This will overwrite your current template.')) {
      return;
    }

    const resetBtn = document.getElementById('reset-template-btn');
    if (resetBtn && resetBtn.disabled) return;
    
    if (resetBtn) resetBtn.disabled = true;
    setStatus('Resetting…', 'text-info');
    try {
      const resp = await window.apiFetch('/settings/smtp/template/reset', { method: 'POST' });
      if (!resp.ok) {
        setStatus('Failed to reset template', 'text-danger');
        window.justUpToast?.error?.('Failed to reset template');
        return;
      }

      let data = null;
      try {
        data = await resp.json();
      } catch (e) {
        console.warn('Failed to parse reset response:', e);
        data = null;
      }

      const subjectInput = document.getElementById('email_subject');
      if (data?.subject && subjectInput) subjectInput.value = data.subject;
      if (typeof data?.html === 'string') setTemplateHtml(data.html);

      // Update preview if in preview mode
      if (currentMode === 'preview') {
        updatePreview();
      }

      setStatus('Reset to default', 'text-success');
      window.justUpToast?.success?.('Template reset');
    } catch (e) {
      console.warn('Template reset failed:', e);
      setStatus('Error resetting template', 'text-danger');
      window.justUpToast?.error?.('Error resetting template');
    } finally {
      if (resetBtn) resetBtn.disabled = false;
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Initialization
  // ─────────────────────────────────────────────────────────────────────────────

  function bind(root) {
    const scope = root || document;
    const btnEdit = scope.querySelector('#btn-mode-edit');
    const btnHtml = scope.querySelector('#btn-mode-html');
    const btnPreview = scope.querySelector('#btn-mode-preview');
    const saveBtn = scope.querySelector('#save-template-btn');
    const resetBtn = scope.querySelector('#reset-template-btn');

    if (btnEdit && !btnEdit.dataset.bound) {
      btnEdit.dataset.bound = '1';
      btnEdit.addEventListener('click', () => switchEditorMode('edit'));
    }

    if (btnHtml && !btnHtml.dataset.bound) {
      btnHtml.dataset.bound = '1';
      btnHtml.addEventListener('click', () => switchEditorMode('html'));
    }

    if (btnPreview && !btnPreview.dataset.bound) {
      btnPreview.dataset.bound = '1';
      btnPreview.addEventListener('click', () => switchEditorMode('preview'));
    }

    if (saveBtn && !saveBtn.dataset.bound) {
      saveBtn.dataset.bound = '1';
      saveBtn.addEventListener('click', saveTemplate);
    }

    if (resetBtn && !resetBtn.dataset.bound) {
      resetBtn.dataset.bound = '1';
      resetBtn.addEventListener('click', resetTemplate);
    }
  }

  // Expose mode switcher for other modules (optional)
  window.justUpSmtpTemplate = window.justUpSmtpTemplate || {};
  window.justUpSmtpTemplate.switchEditorMode = switchEditorMode;

  function init() {
    // Clear previous bindings when resetting state
    document.querySelectorAll('[data-bound]').forEach(function (el) {
      if (['btn-mode-edit', 'btn-mode-html', 'btn-mode-preview',
           'save-template-btn', 'reset-template-btn'].includes(el.id)) {
        delete el.dataset.bound;
      }
    });
    var toolbar = document.querySelector('.wysiwyg-toolbar');
    if (toolbar) delete toolbar.dataset.bound;

    // Reset state for fresh load
    wysiwygInitialized = false;
    currentMode = 'preview';
    
    bind(document);
    updatePreview();
  }

  // Named function for HTMX event handler (allows cleanup)
  function onHtmxSettle(e) {
    bind(e.target);
    // Reset and update preview when SMTP tab is loaded via HTMX
    if (e.target.querySelector('#email_preview_frame')) {
      wysiwygInitialized = false;
      currentMode = 'preview';
      updatePreview();
    }
  }

  // Listen for theme changes to update WYSIWYG editor
  window.addEventListener('theme-changed', function () {
    if (wysiwygInitialized) {
      var doc = getWysiwygDocument();
      var existingStyle = doc?.querySelector('style[data-wysiwyg]');
      if (existingStyle) existingStyle.remove();
      wysiwygInitialized = false;
      initWysiwyg();
    }
  });

  document.addEventListener('DOMContentLoaded', init);
  document.body.addEventListener('htmx:afterSettle', onHtmxSettle);

  // Clean up when leaving the page via HTMX navigation
  document.body.addEventListener('htmx:beforeSwap', function cleanup(evt) {
    var target = evt.detail.target;
    if (target === document.body || target.id === 'main-content') {
      document.body.removeEventListener('htmx:afterSettle', onHtmxSettle);
      document.body.removeEventListener('htmx:beforeSwap', cleanup);
    }
  });
})();
