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


/* =====================================================================
 * وحدة الشريط العلوي: تبديل اللغة (عربي/إنجليزي) + زر المساعدة
 * ===================================================================== */
(function () {
  "use strict";

  function getCookie(name) {
    var m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? decodeURIComponent(m.pop()) : "";
  }
  function csrfToken() {
    var el = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (el && el.value) return el.value;
    return getCookie("mt_csrf") || getCookie("csrftoken");
  }
  function currentLang() {
    var l = (document.documentElement.getAttribute("lang") || "").slice(0, 2).toLowerCase();
    return l === "en" ? "en" : "ar";
  }

  function switchLang(target) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = "/i18n/setlang/";
    form.style.display = "none";
    function add(n, v) {
      var i = document.createElement("input");
      i.type = "hidden"; i.name = n; i.value = v; form.appendChild(i);
    }
    add("csrfmiddlewaretoken", csrfToken());
    add("language", target);
    add("next", window.location.pathname + window.location.search);
    document.body.appendChild(form);
    form.submit();
  }

  function navRoot() {
    return document.querySelector(".main-header .navbar-nav.ml-auto") ||
           (function () {
             var lists = document.querySelectorAll(".main-header .navbar-nav");
             return lists.length ? lists[lists.length - 1] : null;
           })();
  }

  function build() {
    var root = navRoot();
    if (!root || root.getAttribute("data-mt-nav") === "1") return;
    root.setAttribute("data-mt-nav", "1");

    var lang = currentLang();
    var other = lang === "ar" ? "en" : "ar";
    var otherLabel = lang === "ar" ? "EN" : "ع";
    var otherTitle = lang === "ar" ? "التبديل للإنجليزية" : "Switch to Arabic";

    // زر المساعدة
    var help = document.createElement("li");
    help.className = "nav-item mt-nav-btn mt-help";
    help.innerHTML = '<a class="nav-link" href="#" title="كيف أستخدم النظام؟">' +
                     '<i class="fas fa-circle-question"></i><span class="mt-nav-lbl">مساعدة</span></a>';
    help.querySelector("a").addEventListener("click", function (e) {
      e.preventDefault();
      if (window.MTOnboarding) window.MTOnboarding.open();
    });

    // زر تبديل اللغة
    var langBtn = document.createElement("li");
    langBtn.className = "nav-item mt-nav-btn mt-lang";
    langBtn.innerHTML = '<a class="nav-link" href="#" title="' + otherTitle + '">' +
                        '<i class="fas fa-globe"></i><span class="mt-lang-code">' + otherLabel + '</span></a>';
    langBtn.querySelector("a").addEventListener("click", function (e) {
      e.preventDefault(); switchLang(other);
    });

    root.insertBefore(help, root.firstChild);
    root.insertBefore(langBtn, root.firstChild);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();


/* =====================================================================
 * وحدة التعريف بالنظام (Onboarding) — نافذة ترحيب بخطوات مبسّطة + فيديو
 * ---------------------------------------------------------------------
 * ✏️ لإضافة الفيديو التعريفي: حطّ الرابط في MT_VIDEO_URL تحت (رابط
 *    embed من يوتيوب مثلاً: https://www.youtube.com/embed/XXXX).
 * ===================================================================== */
(function () {
  "use strict";

  var MT_VIDEO_URL = ""; // 👈 ضع رابط فيديو الشرح هنا (embed)
  var SEEN_KEY = "mtSeenIntro_v1";

  var SLIDES = [
    { icon: "🔍", title: "ابحث في ثانية",
      body: "اكتب اسم أي شاشة في خانة البحث أعلى القائمة الجانبية، أو اضغط <b>Ctrl + K</b> للبحث الشامل في كل النظام." },
    { icon: "🗂️", title: "كل شيء في كروت مرتّبة",
      body: "التطبيقات اتجمّعت في كروت حسب المجال (العمليات، الموارد البشرية، الطباعة، التشخيص…). دوس على الكارت عشان يفتح أو يقفل — وهو بيفتكر آخر حالة." },
    { icon: "🌐", title: "بدّل اللغة بضغطة",
      body: "من زر <i class='fas fa-globe'></i> أعلى الصفحة تقدر تحوّل بين <b>العربية</b> و<b>English</b> في أي وقت." },
    { icon: "🚀", title: "الوصول السريع",
      body: "أهم الإجراءات (أمر شغل، توريد، السوق، التقارير…) موجودة كأزرار سريعة في أعلى لوحة التحكم عشان توصلها فورًا." }
  ];

  var overlay, idx = 0;

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function render() {
    var s = SLIDES[idx];
    var isVideo = idx === SLIDES.length - 1 && MT_VIDEO_URL;
    var media = isVideo
      ? '<div class="mt-ob-video"><iframe src="' + MT_VIDEO_URL + '" title="شرح" allowfullscreen frameborder="0"></iframe></div>'
      : '<div class="mt-ob-icon">' + s.icon + '</div>';
    overlay.querySelector(".mt-ob-media").innerHTML = media;
    overlay.querySelector(".mt-ob-title").innerHTML = s.title;
    overlay.querySelector(".mt-ob-body").innerHTML = s.body;

    var dots = overlay.querySelector(".mt-ob-dots");
    dots.innerHTML = "";
    SLIDES.forEach(function (_, i) {
      var d = el("span", "mt-ob-dot" + (i === idx ? " on" : ""));
      d.addEventListener("click", function () { idx = i; render(); });
      dots.appendChild(d);
    });

    overlay.querySelector(".mt-ob-back").style.visibility = idx === 0 ? "hidden" : "visible";
    overlay.querySelector(".mt-ob-next").textContent = idx === SLIDES.length - 1 ? "يلا نبدأ ✅" : "التالي ›";
  }

  function close() {
    if (overlay) overlay.classList.remove("show");
    try { localStorage.setItem(SEEN_KEY, "1"); } catch (e) {}
  }
  function open() {
    if (!overlay) create();
    idx = 0; render();
    overlay.classList.add("show");
  }

  function create() {
    overlay = el("div", "mt-ob-overlay");
    overlay.innerHTML =
      '<div class="mt-ob-modal" role="dialog" aria-modal="true">' +
        '<button class="mt-ob-x" aria-label="إغلاق">&times;</button>' +
        '<div class="mt-ob-media"></div>' +
        '<h3 class="mt-ob-title"></h3>' +
        '<p class="mt-ob-body"></p>' +
        '<div class="mt-ob-dots"></div>' +
        '<div class="mt-ob-actions">' +
          '<button class="mt-ob-back">‹ السابق</button>' +
          '<button class="mt-ob-skip">تخطّي</button>' +
          '<button class="mt-ob-next">التالي ›</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);

    overlay.querySelector(".mt-ob-x").addEventListener("click", close);
    overlay.querySelector(".mt-ob-skip").addEventListener("click", close);
    overlay.querySelector(".mt-ob-back").addEventListener("click", function () {
      if (idx > 0) { idx--; render(); }
    });
    overlay.querySelector(".mt-ob-next").addEventListener("click", function () {
      if (idx < SLIDES.length - 1) { idx++; render(); } else close();
    });
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.addEventListener("keydown", function (e) {
      if (!overlay.classList.contains("show")) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") overlay.querySelector(".mt-ob-back").click();
      else if (e.key === "ArrowRight") overlay.querySelector(".mt-ob-next").click();
    });
  }

  window.MTOnboarding = { open: open };

  function boot() {
    var seen = "1";
    try { seen = localStorage.getItem(SEEN_KEY); } catch (e) {}
    if (seen !== "1") setTimeout(open, 900); // ترحيب أول مرة فقط
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
