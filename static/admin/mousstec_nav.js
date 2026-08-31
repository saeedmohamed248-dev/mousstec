/* =====================================================================
 * Mouss Tec — Smart Sidebar (Command Center)
 * ---------------------------------------------------------------------
 * تحويل القائمة الجانبية الطويلة إلى نظام كروت مرتّب + بحث فوري.
 * Progressive enhancement فوق Jazzmin/AdminLTE — لو الـ DOM اتغيّر
 * السكربت بيوقف بهدوء والقائمة الأصلية بتفضل شغّالة زي ما هي.
 * ===================================================================== */
(function () {
  "use strict";

  var LS_KEY = "mtNavOpenGroups";

  /* ---------------------------------------------------------------
   * خريطة التصنيفات الكبرى: كل قسم = كارت يجمع أكتر من تطبيق.
   * المطابقة بتتم عن طريق وجود "/app_label/" داخل أي رابط جوّه المجموعة،
   * فمش بنعتمد على أي بادئة ثابتة للأدمن.
   * ------------------------------------------------------------- */
  var CATEGORIES = [
    { id: "ops",      icon: "🗂️", title: "العمليات اليومية",       color: "#8b5cf6",
      apps: ["inventory"] },
    { id: "hr",       icon: "👥", title: "الموارد البشرية",         color: "#0ea5e9",
      apps: ["hr"] },
    { id: "print",    icon: "🖨️", title: "الطباعة والتصميم",        color: "#ec4899",
      apps: ["printing", "design_store"] },
    { id: "diag",     icon: "🔧", title: "التشخيص الذكي",           color: "#14b8a6",
      apps: ["smart_diagnostics", "diagnostics_catalog", "workshop"] },
    { id: "ecu",      icon: "🚗", title: "منظومة BMW / Mini ECU",   color: "#f59e0b",
      apps: ["bmw_ecu"] },
    { id: "atlas",    icon: "🛠️", title: "أطلس الإصلاح والضفائر",   color: "#ef4444",
      apps: ["repair_atlas"] },
    { id: "market",   icon: "🛒", title: "الأسواق والمزادات",       color: "#22c55e",
      apps: ["marketplace_b2b", "marketplace_c2c", "billing"] },
    { id: "rooms",    icon: "🧠", title: "الغرف الذكية والدعم",     color: "#a855f7",
      apps: ["ai_rooms", "support", "messenger_bot"] },
    { id: "platform", icon: "🏢", title: "الإدارة والمنصة",         color: "#64748b",
      apps: ["clients", "tenancy", "auth"] },
    { id: "other",    icon: "🔗", title: "أدوات أخرى",              color: "#94a3b8",
      apps: [] } // fallback
  ];

  function normalize(s) {
    // إزالة التشكيل + توحيد الألف/الياء/التاء المربوطة عشان البحث العربي يبقى سمح
    return (s || "")
      .toString()
      .toLowerCase()
      .replace(/[ً-ْـ]/g, "")
      .replace(/[أإآ]/g, "ا")
      .replace(/ى/g, "ي")
      .replace(/ة/g, "ه")
      .trim();
  }

  function categoryForItem(li) {
    var links = li.querySelectorAll("a[href]");
    for (var c = 0; c < CATEGORIES.length; c++) {
      var apps = CATEGORIES[c].apps;
      for (var a = 0; a < apps.length; a++) {
        var needle = "/" + apps[a] + "/";
        for (var i = 0; i < links.length; i++) {
          var href = links[i].getAttribute("href") || "";
          if (href.indexOf(needle) !== -1) return CATEGORIES[c].id;
        }
      }
    }
    return "other";
  }

  function loadOpen() {
    try { return JSON.parse(localStorage.getItem(LS_KEY)) || {}; }
    catch (e) { return {}; }
  }
  function saveOpen(state) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (e) {}
  }

  function build() {
    var sidebar = document.querySelector(".nav-sidebar");
    if (!sidebar || sidebar.getAttribute("data-mt-enhanced") === "1") return;

    // المجموعات = عناصر المستوى الأول اللي جوّها treeview (تطبيقات ليها موديلات)
    var topItems = [];
    var child = sidebar.firstElementChild;
    while (child) {
      if (child.classList && child.classList.contains("nav-item")) topItems.push(child);
      child = child.nextElementSibling;
    }
    var groups = topItems.filter(function (li) {
      return li.querySelector(".nav-treeview");
    });
    // لو مفيش مجموعات كفاية، سيب القائمة زي ما هي (مفيش قيمة من التصنيف)
    if (groups.length < 3) return;

    sidebar.setAttribute("data-mt-enhanced", "1");

    var openState = loadOpen();
    var activeCatId = null;

    /* ---- شريط البحث الفوري ---- */
    var searchLi = document.createElement("li");
    searchLi.className = "nav-item mt-search-item";
    searchLi.innerHTML =
      '<div class="mt-search">' +
        '<i class="fas fa-search mt-search-ico"></i>' +
        '<input type="text" id="mtNavSearch" autocomplete="off" spellcheck="false" ' +
        'placeholder="ابحث في القائمة… (Ctrl+/)">' +
        '<button type="button" class="mt-search-clear" aria-label="مسح">&times;</button>' +
      '</div>';
    sidebar.insertBefore(searchLi, sidebar.firstChild);

    /* ---- بناء الكروت وتوزيع المجموعات ---- */
    var buckets = {};
    CATEGORIES.forEach(function (cat) {
      var wrap = document.createElement("li");
      wrap.className = "nav-item mt-cat";
      wrap.setAttribute("data-cat", cat.id);
      wrap.style.setProperty("--mt-cat", cat.color);

      var header = document.createElement("a");
      header.href = "#";
      header.className = "nav-link mt-cat-head";
      header.innerHTML =
        '<span class="mt-cat-emoji">' + cat.icon + '</span>' +
        '<p class="mt-cat-title">' + cat.title +
          '<span class="mt-cat-count"></span>' +
          '<i class="right fas fa-angle-left mt-cat-caret"></i>' +
        '</p>';

      var body = document.createElement("ul");
      body.className = "nav nav-treeview mt-cat-body";

      header.addEventListener("click", function (e) {
        e.preventDefault();
        toggleCat(wrap);
      });

      wrap.appendChild(header);
      wrap.appendChild(body);
      buckets[cat.id] = { wrap: wrap, body: body, count: 0 };
      sidebar.appendChild(wrap);
    });

    groups.forEach(function (li) {
      var catId = categoryForItem(li);
      var bucket = buckets[catId] || buckets.other;
      if (li.querySelector(".nav-link.active") || li.classList.contains("menu-open")) {
        activeCatId = catId;
      }
      bucket.body.appendChild(li);
      bucket.count++;
    });

    /* ---- تنظيف الكروت الفاضية + ضبط العدّادات ---- */
    CATEGORIES.forEach(function (cat) {
      var b = buckets[cat.id];
      if (b.count === 0) { b.wrap.parentNode.removeChild(b.wrap); return; }
      b.wrap.querySelector(".mt-cat-count").textContent = b.count;
      var shouldOpen = (cat.id === activeCatId) ||
                       (openState[cat.id] === true && activeCatId === null);
      setCat(b.wrap, shouldOpen);
    });

    // لو مفيش كارت مفتوح افتراضيًا، افتح الأول
    if (activeCatId === null && !Object.keys(openState).length) {
      var first = sidebar.querySelector(".mt-cat");
      if (first) setCat(first, true);
    }

    wireSearch();
  }

  function setCat(wrap, open) {
    if (open) wrap.classList.add("mt-open");
    else wrap.classList.remove("mt-open");
  }

  function toggleCat(wrap) {
    var open = !wrap.classList.contains("mt-open");
    setCat(wrap, open);
    var state = loadOpen();
    state[wrap.getAttribute("data-cat")] = open;
    saveOpen(state);
  }

  /* ------------------------- البحث الفوري ------------------------- */
  function wireSearch() {
    var input = document.getElementById("mtNavSearch");
    var clearBtn = document.querySelector(".mt-search-clear");
    if (!input) return;

    function apply() {
      var q = normalize(input.value);
      var sidebar = document.querySelector(".nav-sidebar");
      var cats = sidebar.querySelectorAll(".mt-cat");
      document.body.classList.toggle("mt-searching", q.length > 0);

      cats.forEach(function (cat) {
        var links = cat.querySelectorAll(".mt-cat-body .nav-link");
        var hits = 0;
        links.forEach(function (link) {
          var host = link.closest(".nav-item") || link;
          if (link.classList.contains("mt-cat-head")) return;
          var txt = normalize(link.textContent);
          var show = !q || txt.indexOf(q) !== -1;
          host.style.display = show ? "" : "none";
          if (show && q) hits++;
        });
        // أثناء البحث: أظهر الكروت اللي فيها نتائج وافتحها + افتح المجموعات جوّاها
        if (q) {
          cat.style.display = hits ? "" : "none";
          setCat(cat, hits > 0);
          cat.querySelectorAll(".mt-cat-body > .nav-item").forEach(function (app) {
            if (app.style.display === "none") return;
            app.classList.add("menu-open");
            var tv = app.querySelector(":scope > .nav-treeview");
            if (tv) tv.style.display = "block";
          });
        } else {
          cat.style.display = "";
        }
      });

      if (!q) {
        // رجوع للحالة الطبيعية: قفل المجموعات المفتوحة بالبحث
        sidebar.querySelectorAll(".mt-cat-body > .nav-item").forEach(function (app) {
          if (!app.querySelector(".nav-link.active")) {
            app.classList.remove("menu-open");
            var tv = app.querySelector(":scope > .nav-treeview");
            if (tv) tv.style.display = "";
          }
        });
        cats.forEach(function (cat) {
          var isActive = cat.classList.contains("mt-open") &&
                         cat.querySelector(".nav-link.active");
          if (!isActive) {
            var st = loadOpen();
            setCat(cat, st[cat.getAttribute("data-cat")] === true);
          }
        });
      }
    }

    input.addEventListener("input", apply);
    clearBtn.addEventListener("click", function () {
      input.value = ""; apply(); input.focus();
    });
    document.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "/") { e.preventDefault(); input.focus(); }
      if (e.key === "Escape" && document.activeElement === input) {
        input.value = ""; apply(); input.blur();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
