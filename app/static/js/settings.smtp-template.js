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
  var LOGO_URL_PREVIEW = '/static/img/justup_email_logo.png';

  // NOTE: Must match the placeholder syntax of the server-side renderer
  // (app/services/notifications/email_template.py). The stored template keeps the
  // {{logo_url}} placeholder; a rendered cid: reference is also mapped so a saved,
  // already-rendered template still previews the logo.
  var LOGO_PLACEHOLDER_RE = /\{\{logo_url\}\}/g;
  var LOGO_CID_RE = /cid:justup-logo/g;

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
      @font-face {
        font-family: 'IBM Plex Sans';
        src: url('/static/fonts/IBM-Plex-Sans-Variable.woff2') format('woff2');
        font-weight: 100 900;
        font-style: normal;
        font-display: swap;
      }
      body {
        font-family: 'IBM Plex Sans', sans-serif;
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

  if (doc.body) {
    doc.body.contentEditable = 'true';
      
      // Load initial content from source textarea
      const source = document.getElementById('email_html_source');
      if (source) {
        doc.body.innerHTML = sanitizeTemplateDocument(source.value || '').body.innerHTML;
      }
    }

    // Bind toolbar buttons
    bindToolbar();
    bindLinkRow();

    wysiwygInitialized = true;
  }

  // Link address row (replaces the native prompt): a blocked or empty address is a field error, nothing is inserted.
  let linkSelection = null;

  function closeLinkRow(returnFocus) {
    const row = document.getElementById('wysiwyg-link-row');
    const input = document.getElementById('wysiwyg-link-url');
    if (!row) return;
    if (window.JuFieldErrors && input) window.JuFieldErrors.clear(input);
    row.hidden = true;
    linkSelection = null;
    if (returnFocus) document.querySelector('.wysiwyg-toolbar [data-cmd="createLink"]')?.focus();
  }

  function openLinkRow(doc) {
    const row = document.getElementById('wysiwyg-link-row');
    const input = document.getElementById('wysiwyg-link-url');
    if (!row || !input) return;
    // The editor selection survives in its own document; keep a copy to restore it when inserting.
    const selection = doc.getSelection && doc.getSelection();
    linkSelection = selection && selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
    input.value = 'https://';
    if (window.JuFieldErrors) window.JuFieldErrors.clear(input);
    row.hidden = false;
    input.focus();
    input.select();
  }

  function applyLink() {
    const input = document.getElementById('wysiwyg-link-url');
    const doc = getWysiwygDocument();
    if (!input || !doc) return;
    const result = window.JuLinkUrl.validateLinkUrl(input.value);
    if (!result.ok) {
      window.JuFieldErrors.show(document.getElementById('wysiwyg-link-row'), [{ id: 'wysiwyg-link-url', message: result.error }]);
      return;
    }
    doc.body?.focus();
    const selection = doc.getSelection && doc.getSelection();
    if (selection && linkSelection) {
      selection.removeAllRanges();
      selection.addRange(linkSelection);
    }
    doc.execCommand('createLink', false, result.url);
    closeLinkRow(false);
    doc.body?.focus();
  }

  function bindLinkRow() {
    const input = document.getElementById('wysiwyg-link-url');
    if (!input || input.dataset.bound) return;
    input.dataset.bound = '1';
    document.getElementById('wysiwyg-link-apply')?.addEventListener('click', applyLink);
    document.getElementById('wysiwyg-link-cancel')?.addEventListener('click', () => closeLinkRow(true));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        applyLink();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeLinkRow(true);
      }
    });
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
          openLinkRow(doc);
          return;
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
        
        sel.value = '';
        doc.body?.focus();
      });
    });
  }

  function syncWysiwygToSource() {
    const doc = getWysiwygDocument();
    const source = document.getElementById('email_html_source');
    if (doc?.body && source) {
      source.value = mergeBodyIntoSource(source.value || '', doc.body.innerHTML);
    }
  }

  // The editor only holds the body; keep the stored template's doctype, <html> and <head>.
  function mergeBodyIntoSource(sourceHtml, bodyHtml) {
    if (!/<(!doctype|html|head|body)\b/i.test(sourceHtml)) return bodyHtml;
    const original = new DOMParser().parseFromString(sourceHtml, 'text/html');
    original.body.innerHTML = bodyHtml;
    const doctype = original.doctype ? new XMLSerializer().serializeToString(original.doctype) + '\n' : '';
    return doctype + original.documentElement.outerHTML;
  }

  function syncSourceToWysiwyg() {
    const doc = getWysiwygDocument();
    const source = document.getElementById('email_html_source');
    if (doc?.body && source) {
      doc.body.innerHTML = sanitizeTemplateDocument(source.value || '').body.innerHTML;
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
   * Parse template HTML and strip scripts and event handlers; returns the document.
   * NOTE: Server-side sanitization is still required before sending emails
   */
  function sanitizeTemplateDocument(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    
    // Remove script tags and embedding elements
    doc.querySelectorAll('script, iframe, object, embed').forEach(function (el) {
      el.remove();
    });
    
    // Remove event handler attributes (onclick, onerror, etc.) and javascript: URLs
    doc.querySelectorAll('*').forEach(function (el) {
      Array.from(el.attributes).forEach(function (attr) {
        // Browsers ignore whitespace/control chars inside URL schemes
        // eslint-disable-next-line no-control-regex
        var value = attr.value.replace(/[\s\u0000-\u001f]/g, '').toLowerCase();
        if (attr.name.startsWith('on') ||
            (/^(href|src|xlink:href|action|formaction)$/.test(attr.name) && value.startsWith('javascript:'))) {
          el.removeAttribute(attr.name);
        }
      });
    });
    
    return doc;
  }

  // Full document so the template's <head> styles render in the preview.
  function sanitizeForPreview(html) {
    return sanitizeTemplateDocument(html).documentElement.outerHTML;
  }

  // Current preview sample variant ("down" | "resolved").
  let previewVariant = 'down';
  let previewGeneration = 0;
  let previewController = null;

  // Local fallback: swap logo references so the raw template at least shows the logo.
  function localLogoPreview(html) {
    return (html || '')
      .replace(LOGO_PLACEHOLDER_RE, LOGO_URL_PREVIEW)
      .replace(LOGO_CID_RE, LOGO_URL_PREVIEW);
  }

  // Render the preview server-side with sample data so no {{placeholder}} remains and
  // the result matches what the real send path produces. Falls back to a local logo
  // swap if the request fails (e.g. offline), which still renders the layout.
  async function updatePreview() {
    const generation = ++previewGeneration;
    previewController?.abort();
    const controller = new AbortController();
    previewController = controller;
    const previewFrame = document.getElementById('email_preview_frame');
    if (!previewFrame) return;

    const html = getCurrentTemplateHtml();
    const subject = (document.getElementById('email_subject')?.value || '');

    try {
      const resp = await window.apiFetch('/settings/smtp/template/preview', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject, html, variant: previewVariant }),
      });
      if (resp.ok) {
        const data = await resp.json();
        if (generation !== previewGeneration) return;
        previewFrame.srcdoc = sanitizeForPreview(data.html || '');
        return;
      }
    } catch (e) {
      if (generation !== previewGeneration) return;
      console.warn('Preview render failed, falling back to local preview:', e);
    }
    if (generation !== previewGeneration) return;
    previewFrame.srcdoc = sanitizeForPreview(localLogoPreview(html));
  }

  function setPreviewVariant(variant) {
    previewVariant = variant === 'resolved' ? 'resolved' : 'down';
    const btnDown = document.getElementById('btn-preview-down');
    const btnResolved = document.getElementById('btn-preview-resolved');
    btnDown?.classList.toggle('active', previewVariant === 'down');
    btnResolved?.classList.toggle('active', previewVariant === 'resolved');
    if (currentMode === 'preview') updatePreview();
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

    if (wysiwygContainer) wysiwygContainer.style.display = 'none';
    if (htmlContainer) htmlContainer.style.display = 'none';
    if (previewContainer) previewContainer.style.display = 'none';

    btnEdit?.classList.remove('active');
    btnHtml?.classList.remove('active');
    btnPreview?.classList.remove('active');

    currentMode = mode;

    if (mode === 'edit') {
      // WYSIWYG editor
      if (wysiwygContainer) wysiwygContainer.style.display = '';
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
      if (htmlContainer) htmlContainer.style.display = '';
      btnHtml?.classList.add('active');
      document.getElementById('email_html_source')?.focus();
    } else {
      // Preview mode (default)
      if (previewContainer) previewContainer.style.display = '';
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
    if (!(await window.confirmAction({ title: 'Reset email template', message: 'The current template is overwritten with the default.', confirmLabel: 'Reset' }))) {
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

    const btnPreviewDown = scope.querySelector('#btn-preview-down');
    const btnPreviewResolved = scope.querySelector('#btn-preview-resolved');
    if (btnPreviewDown && !btnPreviewDown.dataset.bound) {
      btnPreviewDown.dataset.bound = '1';
      btnPreviewDown.addEventListener('click', () => setPreviewVariant('down'));
    }
    if (btnPreviewResolved && !btnPreviewResolved.dataset.bound) {
      btnPreviewResolved.dataset.bound = '1';
      btnPreviewResolved.addEventListener('click', () => setPreviewVariant('resolved'));
    }
  }

  // Expose mode switcher for other modules (optional)
  window.justUpSmtpTemplate = window.justUpSmtpTemplate || {};
  window.justUpSmtpTemplate.switchEditorMode = switchEditorMode;

  function init() {
    // Clear previous bindings when resetting state
    document.querySelectorAll('[data-bound]').forEach(function (el) {
      if (['btn-mode-edit', 'btn-mode-html', 'btn-mode-preview',
           'save-template-btn', 'reset-template-btn',
           'btn-preview-down', 'btn-preview-resolved'].includes(el.id)) {
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
      // Re-init reloads the body from the textarea, so keep pending edits
      if (currentMode === 'edit') syncWysiwygToSource();
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
