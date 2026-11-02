//
// app/static/js/target-detail/pdf-export.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * PDF report request and job-status handling.
 * Depends on:
 *   - appState       → appState.range (from target-detail.js)
 *   - apiFetch       → global auth-aware fetch wrapper (from base.html)
 *   - justUpPdfJobs  → global PDF job poller (from base.html)
 * @module target-detail/pdf-export
 */

// Reentrancy guard: prevents double-click from spawning parallel requests
let _pdfRequestInFlight = false;

/**
 * Get PDF export button elements (centralized DOM lookup).
 * @private
 * @returns {{btn: HTMLElement|null, icon: HTMLElement|null, spinner: HTMLElement|null}}
 */
function _getPdfElements() {
  return {
    btn: document.getElementById("export-pdf-btn"),
    icon: document.getElementById("pdf-icon"),
    spinner: document.getElementById("pdf-spinner"),
  };
}

/**
 * Short inline status next to the button; empty text hides it. Results arrive as notifications.
 * @private
 * @param {string} text
 */
function _setPdfStatus(text) {
  const el = document.getElementById("pdf-export-status");
  if (!el) return;
  el.textContent = text;
  el.dataset.state = text ? "error" : "";
  el.hidden = !text;
}
/**
 * Reset the PDF export button to its idle state.
 */
function resetPdfButton() {
  _pdfRequestInFlight = false;

  const { btn, icon, spinner } = _getPdfElements();

  if (spinner) spinner.classList.add("d-none");
  if (icon) icon.classList.remove("d-none");
  if (btn) btn.disabled = false;
}

/**
 * Set the PDF export button to its loading/busy state.
 * @private
 */
function _setPdfButtonBusy() {
  const { btn, icon, spinner } = _getPdfElements();

  if (icon) icon.classList.add("d-none");
  if (spinner) spinner.classList.remove("d-none");
  if (btn) btn.disabled = true;
}

/**
 * Request async PDF report generation from the backend.
 * Registers the job with the global poller for cross-page tracking.
 *
 * Button remains in busy state until 'pdf-job-completed' event fires
 * or safety timeout (5min) expires.
 *
 * @param {number} targetId
 * @param {string} pdfPageSize - Page size ('a4' or 'letter')
 * @returns {Promise<number|undefined>} Safety timeout handle, or undefined if request fails early
 */
async function downloadPdfReport(targetId, pdfPageSize) {
  if (!targetId || typeof targetId !== "number") {
    console.error("downloadPdfReport: invalid targetId", targetId);
    return;
  }

  const validSizes = ["a4", "letter"];
  const size = validSizes.includes(pdfPageSize) ? pdfPageSize : "a4";

  // Reentrancy guard (prevent double-click)
  if (_pdfRequestInFlight) return;
  _pdfRequestInFlight = true;

  _setPdfStatus("");
  _setPdfButtonBusy();

  // Safety timeout: reset button if job completion event never arrives
  const safetyTimeout = setTimeout(
    () => {
      console.warn(
        "PDF export: safety timeout reached (5min), resetting button",
      );
      resetPdfButton();
    },
    5 * 60 * 1000,
  ); // 5 minutes

  try {
    const since = appState.range.from ? appState.range.from.toISOString() : "";
    const until = appState.range.to ? appState.range.to.toISOString() : "";

    const params = new URLSearchParams({ page_size: size });
    if (since) params.set("since", since);
    if (until) params.set("until", until);

    const response = await apiFetch(
      `/reports/targets/${targetId}/pdf/request?${params}`,
      { method: "POST" },
    );

    if (!response.ok) {
      let errorMsg = `Server error: ${response.status}`;
      // A body can be read only once: take the text, then try it as JSON.
      const text = await response.text().catch(() => "");
      try {
        const errData = JSON.parse(text);
        if (typeof errData.detail === "string") {
          errorMsg = errData.detail;
        } else if (Array.isArray(errData.detail)) {
          errorMsg = errData.detail
            .map((e) => e.msg || JSON.stringify(e))
            .join(", ");
        }
      } catch {
        // Fallback: plaintext
        if (text) errorMsg += ` – ${text.substring(0, 200)}`;
      }
      throw new Error(errorMsg);
    }

    const data = await response.json();
    const jobId = data.job_id;

    // Register with global poller (survives navigation)
    window.justUpPdfJobs?.add(jobId, targetId);

    // Warn if poller unavailable (button will reset via safety timeout)
    if (!window.justUpPdfJobs) {
      console.warn(
        "PDF job poller not available, button will reset after timeout",
      );
    }

    return safetyTimeout;
  } catch (err) {
    clearTimeout(safetyTimeout);
    _pdfRequestInFlight = false;
    console.error("PDF request failed:", err);
    _setPdfStatus("PDF failed: " + err.message);
    resetPdfButton();
  }
}

/**
 * Listen for job-completed events from the global PDF poller.
 * Resets the button when the completed job matches the current target.
 *
 * @param {number} targetId - Target ID to watch for
 * @returns {{cleanup: Function, setSafetyTimeout: Function}} Object with cleanup function and timeout setter
 */
function initPdfJobListener(targetId) {
  let safetyTimeout = null;

  function onJobCompleted(e) {
    if (Number(e.detail?.targetId) === Number(targetId)) {
      if (safetyTimeout) clearTimeout(safetyTimeout);
      resetPdfButton();
      if (e.detail?.success === false)
        _setPdfStatus(e.detail.error || "PDF failed");
    }
  }

  window.addEventListener("pdf-job-completed", onJobCompleted);

  // Return cleanup object with timeout integration
  return {
    cleanup: () => {
      window.removeEventListener("pdf-job-completed", onJobCompleted);
      if (safetyTimeout) clearTimeout(safetyTimeout);
    },
    setSafetyTimeout: (t) => {
      safetyTimeout = t;
    },
  };
}

/**
 * Check if there is already a pending PDF job for this target
 * (e.g. started before navigation) and show the spinner accordingly.
 *
 * @param {number} targetId
 */
function checkPendingPdfJob(targetId) {
  if (!targetId) return;

  const pendingJobs = window.justUpPdfJobs?.getPending() || [];
  const tid = Number(targetId);
  const hasPending = pendingJobs.some((j) => Number(j.targetId) === tid);

  if (hasPending) {
    _setPdfButtonBusy();
  }
}
