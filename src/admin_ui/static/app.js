/**
 * Admin UI — minimal HTMX configuration.
 *
 * Responsibilities:
 *  - Configure HTMX global defaults (error handling, HX-Redirect interception)
 *  - Surface HTMX network errors as a dismissable toast
 */

(function () {
  "use strict";

  // ── HTMX config ─────────────────────────────────────────────────────────────
  // Use HX-Redirect header for session-expired redirects (set by server).
  document.addEventListener("htmx:responseError", function (evt) {
    const xhr = evt.detail.xhr;
    const redirect = xhr && xhr.getResponseHeader("HX-Redirect");
    if (redirect) {
      window.location.href = redirect;
      return;
    }
    showToast("Request failed (" + (xhr ? xhr.status : "network") + ")", "error");
  });

  document.addEventListener("htmx:sendError", function () {
    showToast("Network error — check connectivity", "error");
  });

  // ── Toast helper ─────────────────────────────────────────────────────────────
  function showToast(message, type) {
    var container = document.getElementById("toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "toast-container";
      container.className =
        "fixed bottom-4 right-4 z-50 flex flex-col gap-2 pointer-events-none";
      document.body.appendChild(container);
    }

    var toast = document.createElement("div");
    var bg = type === "error" ? "bg-red-600" : "bg-gray-800";
    toast.className =
      "pointer-events-auto " + bg + " text-white text-sm rounded-lg px-4 py-2 shadow-lg " +
      "transition-opacity duration-300";
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(function () {
      toast.style.opacity = "0";
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 300);
    }, 4000);
  }
})();
