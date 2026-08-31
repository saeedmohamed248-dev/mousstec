/* =====================================================================
 * Mouss Tec — Smart Sidebar (Command Center)
 * ---------------------------------------------------------------------
 * تحويل القائمة الجانبية الطويلة إلى نظام كروت مرتّب + بحث فوري.
 * Progressive enhancement فوق Jazzmin/AdminLTE — بيتعامل مع الشكل
 * المسطّح (navigation_expanded=True: nav-header + nav-item) وكمان مع
 * الشكل المتفرّع (nav-treeview). لو الـ DOM اتغيّر بيوقف بهدوء والقائمة
 * الأصلية بتفضل شغّالة زي ما هي من غير أي كسر.
 * ===================================================================== */
(function () {
  "use strict";

  var LS_KEY = "mtNavOpenGroups";

  /* خريطة التصنيفات الكبرى: كل قسم = كارت يجمع أكتر من تطبيق.
     المطابقة عن طريق وجود "/app_label/" داخل أي رابط، فمش بنعتمد على
     أي بادئة ثابتة للأدمن. */
  var CATEGORIES = [
    { id: "ops",      icon: "🗂️", title: "العمليات اليومية",       color: "#8b5cf6", apps: ["inventory"] },
    { id: "hr",       icon: "👥", title: "الموارد البشرية",         color: "#0ea5e9", apps: ["hr"] },
    { id: "print",    icon: "🖨️", title: "الطباعة والتصميم",        color: "#ec4899", apps: ["printing", "design_store"] },
    { id: "diag",     icon: "🔧", title: "التشخيص الذكي",           color: "#14b8a6", apps: ["smart_diagnostics", "diagnostics_catalog", "workshop"] },
    { id: "ecu",      icon: "🚗", title: "منظومة BMW / Mini ECU",   color: "#f59e0b", apps: ["bmw_ecu"] },
    { id: "atlas",    icon: "🛠️", title: "أطلس الإصلاح والضفائر",   color: "#ef4444", apps: ["repair_atlas"] },
    { id: "market",   icon: "🛒", title: "الأسواق والمزادات",       color: "#22c55e", apps: ["marketplace_b2b", "marketplace_c2c", "billing"] },
    { id: "rooms",    icon: "🧠", title: "الغرف الذكية والدعم",     color: "#a855f7", apps: ["ai_rooms", "support", "messenger_bot"] },
    { id: "platform", icon: "🏢", title: "الإدارة والمنصة",         color: "#64748b", apps: ["clients", "tenancy", "auth"] },
    { id: "other",    icon: "🔗", title: "أدوات أخرى",              color: "#94a3b8", apps: [] } // fallback
  ];

  function normalize(s) {
    return (s || "").toString().toLowerCase()
      .replace(/[ً-ْـ]/g, "")   // تشكيل + تطويل
      .replace(/[أإآ]/g, "ا") // الهمزات → ا
      .replace(/ى/g, "ي")               // ى → ي
      .replace(/ة/g, "ه")               // ة → ه
      .trim();
  }

  function categoryForLinks(links) {
    for (var c = 0; c < CATEGORIES.length; c++) {
      var apps = CATEGORIES[c].apps;
      for (var a = 0; a < apps.length; a++) {
        var needle = "/" + apps[a] + "/";
        for (var i = 0; i < links.length; i++) {
          if ((links[i].getAttribute("href") || "").indexOf(needle) !== -1) return CATEGORIES[c].id;
        }
      }
    }
    return "other";
  }

  function loadOpen() {
    try { return JSON.parse(localStorage.getItem(LS_KEY)) || {}; } catch (e) { return {}; }
  }
  function saveOpen(state) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (e) {}
  }

  /* تقسيم القائمة إلى مجموعات: كل nav-header يبدأ مجموعة، والعناصر اللي
     بعده لحد الـ nav-header اللي بعده بتبقى تبعه. العناصر قبل أول header
     (زي Dashboard) بتفضل مثبّتة في الأعلى. */
  function segment(sidebar) {
    var groups = [], pinned = [], current = null;
    var li = sidebar.firstElementChild;
    while (li) {
      var next = li.nextElementSibling;
      if (li.classList) {
        if (li.classList.contains("nav-header")) {
          current = { header: li, items: [], treeview: false };
          groups.push(current);
        } else if (li.classList.contains("nav-item")) {
          var tv = li.querySelector(".nav-treeview");
          if (tv) { // شكل متفرّع: التطبيق نفسه عنصر واحد جوّه treeview
            groups.push({ header: li, items: [], treeview: true });
            current = null;
          } else if (current) {
            current.items.push(li);
          } else {
            pinned.push(li);
          }
        }
      }
      li = next;
    }
    return { groups: groups, pinned: pinned };
  }

  function build() {
    var sidebar = document.querySelector(".nav-sidebar");
    if (!sidebar || sidebar.getAttribute("data-mt-enhanced") === "1") return;

    var seg = segment(sidebar);
    if (seg.groups.length < 3) return; // مفيش قيمة من التصنيف

    sidebar.setAttribute("data-mt-enhanced", "1");
    var openState = loadOpen();
    var activeCatId = null;

    /* ---- شريط البحث الفوري ---- */
    var searchLi = document.createElement("li");
    searchLi.className = "nav-item mt-search-item";
    searchLi.innerHTML =
      '<div class="mt-search">' +
        '<i class="fas fa-search mt-search-ico"></i>' +
        '<input type="text" id="mtNavSearch" autocomplete="off" spellcheck="false" placeholder="ابحث في القائمة… (Ctrl+/)">' +
        '<button type="button" class="mt-search-clear" aria-label="مسح">&times;</button>' +
      '</div>';

    /* العناصر المثبّتة (Dashboard) تفضل فوق، وبعدها البحث */
    var anchorNode = seg.pinned.length ? seg.pinned[seg.pinned.length - 1].nextSibling : sidebar.firstChild;
    sidebar.insertBefore(searchLi, anchorNode);

    /* ---- تجهيز الكروت ---- */
    var buckets = {};
    CATEGORIES.forEach(function (cat) {
      var wrap = document.createElement("li");
      wrap.className = "nav-item mt-cat";
      wrap.setAttribute("data-cat", cat.id);
      wrap.style.setProperty("--mt-cat", cat.color);
      wrap.innerHTML =
        '<a href="#" class="nav-link mt-cat-head">' +
          '<span class="mt-cat-emoji">' + cat.icon + '</span>' +
          '<p class="mt-cat-title">' + cat.title +
            '<span class="mt-cat-count"></span>' +
            '<i class="right fas fa-angle-left mt-cat-caret"></i>' +
          '</p>' +
        '</a>' +
        '<ul class="nav mt-cat-body"></ul>';
      wrap.querySelector(".mt-cat-head").addEventListener("click", function (e) {
        e.preventDefault(); toggleCat(wrap);
      });
      buckets[cat.id] = { wrap: wrap, body: wrap.querySelector(".mt-cat-body"), items: 0, groups: 0 };
      sidebar.appendChild(wrap);
    });

    /* ---- توزيع المجموعات على الكروت ---- */
    seg.groups.forEach(function (g) {
      var links = [];
      if (g.header && g.header.querySelectorAll) [].push.apply(links, g.header.querySelectorAll("a[href]"));
      g.items.forEach(function (it) { [].push.apply(links, it.querySelectorAll("a[href]")); });

      var catId = categoryForLinks(links);
      var b = buckets[catId] || buckets.other;

      // عنوان القسم الأصلي (اسم التطبيق) كسطر فرعي رفيع جوّه الكارت
      if (g.header && !g.treeview) {
        g.header.classList.add("mt-subhead");
        b.body.appendChild(g.header);
        b.groups++;
      }
      var host = g.treeview ? [g.header] : g.items;
      host.forEach(function (it) {
        if (it.querySelector(".nav-link.active") || it.classList.contains("menu-open")) activeCatId = catId;
        b.body.appendChild(it);
        b.items++;
      });
    });

    /* ---- تنظيف الفاضي + العدّادات + الحالة الأولية ---- */
    CATEGORIES.forEach(function (cat) {
      var b = buckets[cat.id];
      if (b.items === 0) { b.wrap.parentNode.removeChild(b.wrap); return; }
      b.wrap.querySelector(".mt-cat-count").textContent = b.items;
      // لو الكارت فيه قسم واحد بس، مفيش داعي نكرّر عنوانه الفرعي
      if (b.groups <= 1) {
        var sh = b.body.querySelector(".mt-subhead");
        if (sh) sh.style.display = "none";
      }
      var shouldOpen = (cat.id === activeCatId) || (openState[cat.id] === true && activeCatId === null);
      setCat(b.wrap, shouldOpen);
    });

    if (activeCatId === null && !Object.keys(openState).length) {
      var first = sidebar.querySelector(".mt-cat");
      if (first) setCat(first, true);
    }

    wireSearch();
  }

  function setCat(wrap, open) {
    if (open) wrap.classList.add("mt-open"); else wrap.classList.remove("mt-open");
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
      document.body.classList.toggle("mt-searching", q.length > 0);

      sidebar.querySelectorAll(".mt-cat").forEach(function (cat) {
        var hits = 0;
        cat.querySelectorAll(".mt-cat-body > .nav-item").forEach(function (item) {
          var txt = normalize(item.textContent);
          var show = !q || txt.indexOf(q) !== -1;
          item.style.display = show ? "" : "none";
          if (show && q) hits++;
        });
        // العناوين الفرعية: تبان بس لو تحتها عنصر ظاهر
        cat.querySelectorAll(".mt-cat-body > .mt-subhead").forEach(function (sh) {
          if (!q) { sh.style.display = ""; return; }
          var visible = false, n = sh.nextElementSibling;
          while (n && !n.classList.contains("mt-subhead")) {
            if (n.classList.contains("nav-item") && n.style.display !== "none") { visible = true; break; }
            n = n.nextElementSibling;
          }
          sh.style.display = visible ? "" : "none";
        });

        if (q) { cat.style.display = hits ? "" : "none"; setCat(cat, hits > 0); }
        else   { cat.style.display = ""; }
      });

      if (!q) {
        // رجوع للحالة المحفوظة بعد ما نمسح البحث
        var st = loadOpen();
        sidebar.querySelectorAll(".mt-cat").forEach(function (cat) {
          if (cat.classList.contains("mt-open") && cat.querySelector(".nav-link.active")) return;
          setCat(cat, st[cat.getAttribute("data-cat")] === true);
        });
        // إعادة إظهار العناوين الفرعية المتعددة فقط
        sidebar.querySelectorAll(".mt-cat").forEach(function (cat) {
          var subs = cat.querySelectorAll(".mt-cat-body > .mt-subhead");
          subs.forEach(function (sh) { sh.style.display = (subs.length <= 1) ? "none" : ""; });
        });
      }
    }

    input.addEventListener("input", apply);
    clearBtn.addEventListener("click", function () { input.value = ""; apply(); input.focus(); });
    document.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "/") { e.preventDefault(); input.focus(); }
      if (e.key === "Escape" && document.activeElement === input) { input.value = ""; apply(); input.blur(); }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
