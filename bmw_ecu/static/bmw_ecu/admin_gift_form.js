/* Admin Gift form controller — vanilla JS, no framework.
 *
 * Posts to POST /api/admin/entitlements/gift, revokes via
 * POST /api/admin/entitlements/gift/<pk>/revoke. Dynamic field show/hide
 * driven by grant_type select.
 */
(function () {
  "use strict";

  const cfg = window.BMW_ECU_ADMIN_CFG || {};

  const form = document.getElementById("gift-form");
  const grantType = document.getElementById("grant-type");
  const creditsField = document.getElementById("credits-field");
  const creditsInput = document.getElementById("credits-input");
  const validUntilField = document.getElementById("valid-until-field");
  const validUntilInput = document.getElementById("valid-until-input");
  const validUntilRequired = document.getElementById("valid-until-required");
  const flash = document.getElementById("ag-flash");
  const submitBtn = form.querySelector(".ag-submit");
  const tbody = document.querySelector("#gifts-table tbody");

  // --- Dynamic field rules ---------------------------------------------
  function applyGrantTypeRules() {
    const gt = grantType.value;
    const isSubscription = gt === "subscription_window";

    if (isSubscription) {
      creditsField.classList.add("is-hidden");
      creditsInput.required = false;
      validUntilInput.required = true;
      validUntilRequired.classList.remove("is-hidden");
    } else {
      creditsField.classList.remove("is-hidden");
      creditsInput.required = true;
      validUntilInput.required = false;
      validUntilRequired.classList.add("is-hidden");
    }
  }

  // --- Flash --------------------------------------------------------------
  function showFlash(kind, text) {
    flash.className = "ag-flash " + kind;
    flash.textContent = text;
    flash.hidden = false;
  }
  function clearFlash() { flash.hidden = true; flash.textContent = ""; }

  // --- Submit -------------------------------------------------------------
  async function submitGrant(e) {
    e.preventDefault();
    clearFlash();

    const tenant = document.getElementById("tenant-select").value;
    if (!tenant) {
      showFlash("error", "اختار tenant الأول.");
      return;
    }

    const gt = grantType.value;
    const payload = {
      tenant_schema: tenant,
      grant_type: gt,
      note: document.getElementById("note-input").value.trim(),
      allow_stack: document.getElementById("allow-stack").checked,
    };
    if (gt === "subscription_window") {
      const vu = validUntilInput.value;
      if (!vu) {
        showFlash("error", "subscription_window محتاجة valid_until.");
        return;
      }
      payload.valid_until = vu;
    } else {
      const n = parseInt(creditsInput.value, 10);
      if (!n || n <= 0) {
        showFlash("error", "credits لازم يكون رقم > 0.");
        return;
      }
      payload.credits = n;
      // Optional cap for credit-types.
      if (validUntilInput.value) payload.valid_until = validUntilInput.value;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = "⏳ جاري الإصدار…";
    try {
      const res = await fetch(cfg.grantUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   "X-CSRFToken": cfg.csrfToken },
        credentials: "same-origin",
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));

      if (res.status === 201) {
        showFlash("ok",
          "✅ تم الإصدار. Gift #" + body.pk + " لـ " + body.tenant_schema +
          ". الـ tenant يقدر يستخدمه فوراً.");
        prependRow(body);
        form.reset();
        applyGrantTypeRules();
      } else if (res.status === 409) {
        showFlash("warn",
          "⚠️ في gift فعّال من نفس النوع (PK " + body.existing_pk +
          "). فعّل allow_stack لو عايز تركّب فوقه.");
      } else if (res.status === 403) {
        showFlash("error",
          "🔒 محتاج صلاحية super-admin. سجل دخول بـ staff user.");
      } else {
        showFlash("error",
          (body.detail || "حصل خطأ") + " (HTTP " + res.status + ")");
      }
    } catch (err) {
      showFlash("error", "Network error: " + err.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "🎁 Issue gift";
    }
  }

  // --- Revoke -------------------------------------------------------------
  async function revokeGift(pk, btn) {
    if (!confirm("Revoke gift #" + pk + "? الـ tenant مش هيقدر يستخدمه تاني.")) {
      return;
    }
    const url = cfg.revokeUrlTemplate.replace(/\/0\//, "/" + pk + "/");
    btn.disabled = true;
    btn.textContent = "⏳";
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": cfg.csrfToken },
        credentials: "same-origin",
      });
      if (res.ok) {
        const tr = btn.closest("tr");
        if (tr) {
          tr.dataset.status = "revoked";
          const statusCell = tr.querySelector(".ag-status");
          if (statusCell) {
            statusCell.className = "ag-status ag-status-revoked";
            statusCell.textContent = "revoked";
          }
          btn.remove();
        }
      } else {
        const body = await res.json().catch(() => ({}));
        alert(body.detail || ("HTTP " + res.status));
        btn.disabled = false;
        btn.textContent = "Revoke";
      }
    } catch (err) {
      alert("Network error: " + err.message);
      btn.disabled = false;
      btn.textContent = "Revoke";
    }
  }

  // --- Row builder --------------------------------------------------------
  function prependRow(g) {
    const empty = tbody.querySelector(".ag-empty");
    if (empty) empty.parentElement.remove();

    const tr = document.createElement("tr");
    tr.dataset.pk = g.pk;
    tr.dataset.status = g.status;
    tr.innerHTML =
      "<td>" + esc(g.tenant_schema) + "</td>" +
      '<td><span class="ag-chip ag-chip-' + esc(g.grant_type) + '">' +
        esc(g.grant_type) + "</span></td>" +
      "<td>" + (
        g.grant_type === "subscription_window" ? "—"
        : (g.credits_remaining + "/" + g.credits_total)
      ) + "</td>" +
      "<td>" + (g.valid_until ? fmtDate(g.valid_until) : "—") + "</td>" +
      '<td><span class="ag-status ag-status-' + esc(g.status) + '">' +
        esc(g.status) + "</span></td>" +
      "<td><small>" + esc(g.granted_by || "—") + "</small></td>" +
      '<td><button class="ag-revoke" data-pk="' + g.pk + '" type="button">Revoke</button></td>';

    tbody.insertBefore(tr, tbody.firstChild);
    tr.querySelector(".ag-revoke")
      .addEventListener("click", (e) => revokeGift(g.pk, e.currentTarget));
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    })[c]);
  }
  function fmtDate(iso) {
    try { return new Date(iso).toISOString().slice(0, 16).replace("T", " "); }
    catch { return iso; }
  }

  // --- Wire up -----------------------------------------------------------
  function init() {
    applyGrantTypeRules();
    grantType.addEventListener("change", applyGrantTypeRules);
    form.addEventListener("submit", submitGrant);

    // Wire revoke buttons that exist server-side at first render.
    document.querySelectorAll(".ag-revoke").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const pk = parseInt(btn.dataset.pk, 10);
        if (pk) revokeGift(pk, btn);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
