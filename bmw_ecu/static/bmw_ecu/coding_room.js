/* Coding & Retrofit Room controller — vanilla JS, no framework.
 *
 * Reads `x_options` from the existing /api/ecu/execute Coding response
 * (action: list_features), renders cards with toggles, and POSTs the
 * chosen set back via action: apply_features (batch).
 *
 * Chatbot JSON shape is the one already used by the ISN flow — no schema
 * change. We just consume `chatbot_message`, `input_schema.x_options`,
 * `severity`, and `outcome`.
 */
(function () {
  "use strict";

  const cfg = window.BMW_ECU_CFG || {};
  const body = document.body;

  // --- State -------------------------------------------------------------
  const state = {
    features: [],                 // x_options array from server
    selected: new Map(),          // feature_id → enable (bool)
  };

  // --- Helpers -----------------------------------------------------------
  function vin()     { return document.getElementById("vin-input").value.trim(); }
  function profile() { return document.getElementById("profile-input").value; }
  function chassis() { return document.getElementById("chassis-input").value; }

  function pushBubble(text, kind /* "bot" | "user" | "error" */) {
    const log = document.getElementById("chat-log");
    const div = document.createElement("div");
    div.className = "cr-bubble " + (kind || "bot");
    // Split bilingual lines on \n — Arabic first, English on a second line.
    const lines = text.split("\n");
    if (lines.length >= 2) {
      div.appendChild(document.createTextNode(lines[0]));
      const en = document.createElement("span");
      en.className = "en";
      en.textContent = lines.slice(1).join(" ");
      div.appendChild(en);
    } else {
      div.textContent = text;
    }
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  async function callExecute(codingRequest) {
    const payload = {
      vin: vin(), profile_name: profile(),
      operation_type: "coding",
      transport: { kind: "doip", host: "169.254.255.0" },
      capabilities: { has_enet_cable: true, technician_skill_level: 2 },
      coding_request: codingRequest,
    };
    const res = await fetch(cfg.executeUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": cfg.csrfToken,
      },
      credentials: "same-origin",
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = data.chatbot_message ||
                  data.detail || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return data;
  }

  // --- Rendering ---------------------------------------------------------
  function renderCards() {
    const grid = document.getElementById("feature-grid");
    grid.innerHTML = "";

    const filterCat = document.getElementById("category-filter").value;
    const list = state.features.filter(f =>
      !filterCat || f.category === filterCat);

    if (list.length === 0) {
      const empty = document.createElement("div");
      empty.className = "cr-empty";
      empty.textContent = "مفيش features في الفلتر ده.";
      grid.appendChild(empty);
      return;
    }

    list.forEach(f => grid.appendChild(buildCard(f)));
    updateCounter();
  }

  function buildCard(f) {
    const card = document.createElement("article");
    card.className = "cr-card" + (f.warning ? " warn" : "");
    card.dataset.featureId = f.id;
    if (state.selected.has(f.id)) card.classList.add("selected");

    // Top: title + category chip
    const top = document.createElement("div"); top.className = "cr-card-top";
    const title = document.createElement("div"); title.className = "cr-card-title";
    title.appendChild(document.createTextNode(f.label_ar));
    const en = document.createElement("span"); en.className = "en";
    en.textContent = f.label_en;
    title.appendChild(en);
    const cat = document.createElement("span");
    cat.className = "cr-card-cat " + f.category;
    cat.textContent = catLabel(f.category);
    top.appendChild(title); top.appendChild(cat);
    card.appendChild(top);

    // Description
    if (f.description_ar) {
      const desc = document.createElement("div"); desc.className = "cr-card-desc";
      desc.appendChild(document.createTextNode(f.description_ar));
      const dEn = document.createElement("span"); dEn.className = "en";
      dEn.textContent = f.description_en || "";
      desc.appendChild(dEn);
      card.appendChild(desc);
    }

    // Safety warning banner
    if (f.warning) {
      const w = document.createElement("div"); w.className = "cr-card-warning";
      w.textContent = "⚠️ " + f.warning;
      card.appendChild(w);
    }

    // Toggle (Enable / Disable)
    const toggle = document.createElement("div"); toggle.className = "cr-toggle";
    const sw = document.createElement("div"); sw.className = "cr-toggle-switch";
    const lbl = document.createElement("span"); lbl.className = "cr-toggle-label";
    toggle.appendChild(sw); toggle.appendChild(lbl);
    card.appendChild(toggle);

    const enable = state.selected.has(f.id) ? state.selected.get(f.id) : true;
    syncToggle(toggle, lbl, enable, state.selected.has(f.id));

    card.addEventListener("click", (e) => {
      // Click card body = select with current toggle.
      // Click toggle switch = flip enable/disable AND select.
      const onSwitch = e.target.closest(".cr-toggle");
      const wasSelected = state.selected.has(f.id);
      const currentEnable = wasSelected ? state.selected.get(f.id) : true;

      if (onSwitch && wasSelected) {
        state.selected.set(f.id, !currentEnable);
      } else if (wasSelected) {
        state.selected.delete(f.id);
      } else {
        state.selected.set(f.id, onSwitch ? !currentEnable : true);
      }
      renderCards();
    });

    return card;
  }

  function syncToggle(toggle, lbl, enable, selected) {
    toggle.classList.toggle("on", enable);
    lbl.textContent = !selected ? "غير محدد · not selected"
                                : enable ? "تفعيل · enable"
                                         : "إيقاف · disable";
  }

  function catLabel(c) {
    return ({
      comfort: "راحة", lighting: "إضاءة", kombi: "عداد",
      safety_disable: "⚠️ أمان", performance_display: "أداء",
    })[c] || c;
  }

  function updateCounter() {
    const n = state.selected.size;
    document.getElementById("selected-count").textContent =
      n + " selected";
    document.getElementById("apply-btn").disabled = (n === 0);
  }

  // --- Guided connect / pinout rendering ---------------------------------
  // Fallback: if the guidance panel DOM is missing (e.g. a stale cached
  // page), still hand the technician the wiring + steps as a chat bubble so
  // the procedure is never lost behind a cache problem.
  function renderGuidanceBubble(g, locked) {
    const lines = [];
    lines.push(locked ? "🔒 الكنترول مقفول — إجراءات الـ bench:"
                      : "✅ الكنترول مفتوح — جاهز للتكويد.");
    (g.wiring || []).forEach((w) => {
      lines.push("• OBD " + w.obd_pin + " → ECU " + w.ecu_pin
                 + " (" + (w.label_ar || w.function) + ")");
    });
    (g.steps || []).forEach((s) => {
      lines.push((s.n != null ? s.n + ". " : "• ") + s.ar);
    });
    pushBubble(lines.join("\n"), "bot");
  }

  function renderGuidance(g, locked, state) {
    const disconnected = state === "disconnected";
    const panel = document.getElementById("guidance-panel");
    // No panel in the DOM → stale/old page. Use the chat-bubble fallback.
    if (!panel) {
      renderGuidanceBubble(g, locked);
      return;
    }
    const badge = document.getElementById("guidance-badge");
    const title = document.getElementById("guidance-title");
    const steps = document.getElementById("guidance-steps");
    const fig = document.getElementById("guidance-pinout");
    const img = document.getElementById("guidance-pinout-img");
    const callouts = document.getElementById("guidance-callouts");
    const wiring = document.getElementById("guidance-wiring");
    const wiringBody = document.getElementById("guidance-wiring-body");

    if (badge) {
      badge.textContent = disconnected
        ? "❌ NOT CONNECTED"
        : (locked ? "🔒 LOCKED" : "✅ OPEN");
      badge.className = "cr-guidance-badge "
        + (disconnected ? "disconnected" : (locked ? "locked" : "open"));
    }
    if (title) {
      title.textContent = disconnected
        ? "الكنترول مردّش — راجع التوصيل / No reply — check the connection"
        : (locked
          ? "الكنترول مقفول — اتبع الخطوات / Locked — follow the steps"
          : "الكنترول مفتوح — جاهز للتكويد / Open — ready to code");
    }

    if (steps) {
      steps.innerHTML = "";
      (g.steps || []).forEach((s) => {
        const li = document.createElement("li");
        li.appendChild(document.createTextNode(s.ar));
        const en = document.createElement("span");
        en.className = "en";
        en.textContent = s.en;
        li.appendChild(en);
        steps.appendChild(li);
      });
    }

    if (fig && img) {
      if (g.pinout_diagram_url) {
        img.src = g.pinout_diagram_url;
        if (callouts) {
          callouts.innerHTML = "";
          (g.pinout_callouts || []).forEach((c) => {
            const chip = document.createElement("span");
            chip.className = "cr-callout";
            const dot = document.createElement("span");
            dot.className = "cr-callout-dot";
            dot.style.background = c.color || "#999";
            chip.appendChild(dot);
            chip.appendChild(document.createTextNode(
              "Pin " + c.pin + " · " + c.label));
            callouts.appendChild(chip);
          });
        }
        fig.hidden = false;
      } else {
        fig.hidden = true;
      }
    }

    // Explicit OBD-pin → ECU-pin wiring map (bench/locked only).
    if (wiring && wiringBody) {
      wiringBody.innerHTML = "";
      const wires = g.wiring || [];
      if (wires.length > 0) {
        wires.forEach((w) => {
          const row = document.createElement("div");
          row.className = "cr-wire";
          const dot = document.createElement("span");
          dot.className = "cr-callout-dot";
          dot.style.background = w.color || "#999";
          const obd = document.createElement("span");
          obd.className = "cr-wire-pin obd";
          obd.textContent = "OBD " + w.obd_pin;
          const arrow = document.createElement("span");
          arrow.className = "cr-wire-arrow";
          arrow.textContent = "→";
          const ecu = document.createElement("span");
          ecu.className = "cr-wire-pin ecu";
          ecu.textContent = "ECU " + w.ecu_pin;
          const lbl = document.createElement("span");
          lbl.className = "cr-wire-label";
          lbl.textContent = w.label_ar + " · " + w.label_en;
          row.appendChild(dot);
          row.appendChild(obd);
          row.appendChild(arrow);
          row.appendChild(ecu);
          row.appendChild(lbl);
          wiringBody.appendChild(row);
        });
        wiring.hidden = false;
      } else {
        wiring.hidden = true;
      }
    }

    panel.hidden = false;
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function connectRead() {
    pushBubble("🔌 ببدأ الاتصال بالسيارة وقراءة الكنترول…\n"
             + "Connecting and reading the module…", "user");
    try {
      const data = await callExecute({
        action: "connect_read", chassis: chassis(),
      });
      pushBubble(data.chatbot_message || "تم", "bot");
      const diag = (data.diagnostics) || {};
      const g = diag.guidance || {};
      const reachable = diag.reachable !== false;
      const locked = !!diag.locked;

      // ECU didn't answer → don't claim open/locked; show how to reconnect
      // and keep Load features disabled.
      if (!reachable) {
        renderGuidance(g, false, "disconnected");
        document.getElementById("load-features-btn").disabled = true;
        return;
      }

      renderGuidance(g, locked);
      // OPEN module → unlock the Load features button; LOCKED → keep gated
      // until the technician finishes the bench procedure.
      document.getElementById("load-features-btn").disabled = locked;
      if (diag.vin) {
        const el = document.getElementById("vin-input");
        if (el && !el.value.trim()) el.value = diag.vin;
      }
    } catch (e) {
      pushBubble("خطأ في الاتصال: " + e.message, "error");
    }
  }

  // --- Actions -----------------------------------------------------------
  async function loadFeatures() {
    pushBubble("⏳ بحمّل قائمة الميزات للـ " + chassis() + "…", "user");
    try {
      const data = await callExecute({
        action: "list_features", chassis: chassis(),
      });
      pushBubble(data.chatbot_message || "تم", "bot");
      const opts = (data.input_schema && data.input_schema.x_options) || [];
      state.features = opts;
      state.selected.clear();
      renderCards();
    } catch (e) {
      pushBubble("خطأ: " + e.message, "error");
    }
  }

  async function applySelected() {
    const items = Array.from(state.selected.entries()).map(
      ([feature_id, enable]) => ({ feature_id, enable }));
    if (items.length === 0) return;

    // Safety check: if any selected feature has a warning, require modal.
    const danger = items
      .map(it => state.features.find(f => f.id === it.feature_id))
      .filter(f => f && f.warning);
    if (danger.length > 0) {
      const ok = await openSafetyModal(danger);
      if (!ok) return;
    }

    pushBubble("⏳ بنفّذ " + items.length + " feature…", "user");
    try {
      const data = await callExecute({
        action: "apply_features", items: items,
      });
      pushBubble(data.chatbot_message || "تم", "bot");
      // Reset selection on full success.
      if (data.outcome === "success") {
        state.selected.clear();
        renderCards();
      }
    } catch (e) {
      pushBubble("خطأ: " + e.message, "error");
    }
  }

  // --- Safety modal ------------------------------------------------------
  function openSafetyModal(danger) {
    return new Promise((resolve) => {
      const modal = document.getElementById("safety-modal");
      document.getElementById("safety-modal-text").textContent =
        "أنت بتعطّل ميزة أمان مصرّحة، لازم العميل يكون عارف:";
      const list = document.getElementById("safety-modal-list");
      list.innerHTML = "";
      danger.forEach(f => {
        const li = document.createElement("li");
        li.textContent = f.label_ar + " — " + f.label_en;
        list.appendChild(li);
      });
      const consent = document.getElementById("safety-consent");
      const confirmBtn = document.getElementById("safety-confirm");
      consent.checked = false;
      confirmBtn.disabled = true;
      consent.onchange = () => { confirmBtn.disabled = !consent.checked; };
      const close = (ok) => {
        modal.hidden = true; consent.onchange = null;
        confirmBtn.onclick = null;
        document.getElementById("safety-cancel").onclick = null;
        resolve(ok);
      };
      confirmBtn.onclick = () => close(true);
      document.getElementById("safety-cancel").onclick = () => close(false);
      modal.hidden = false;
    });
  }

  // --- Wire up -----------------------------------------------------------
  function init() {
    pushBubble("مرحباً 👋 وصّل كابل الـ ENET في فيشة OBD، حدّد البروفايل "
             + "والشاسيه، واضغط «🔌 Connect & Read» علشان نقرا الكنترول "
             + "ونقولك تعمل إيه بالظبط.\n"
             + "Welcome — plug the ENET cable into OBD, set profile/chassis, "
             + "then press “🔌 Connect & Read”.", "bot");

    document.getElementById("connect-btn")
            .addEventListener("click", connectRead);
    document.getElementById("guidance-close")
            .addEventListener("click", () => {
              document.getElementById("guidance-panel").hidden = true;
            });
    document.getElementById("load-features-btn")
            .addEventListener("click", loadFeatures);
    document.getElementById("apply-btn")
            .addEventListener("click", applySelected);
    document.getElementById("category-filter")
            .addEventListener("change", renderCards);

    document.getElementById("chat-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const txt = document.getElementById("chat-input").value.trim();
      if (!txt) return;
      pushBubble(txt, "user");
      document.getElementById("chat-input").value = "";
      pushBubble("لسة مش متصل بـ AI free-text channel — استخدم الأزرار "
               + "على اليمين دلوقتي.", "bot");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
