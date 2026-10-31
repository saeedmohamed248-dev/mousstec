/* =====================================================================
 * 🆘 Mouss Tec — زرار «مشكلة؟ كلّمنا» في كل صفحات الشركة
 * ---------------------------------------------------------------------
 * العميل يكتب المشكلة ويبعت معاها صور/فيديو من جهازه، أو يصوّر الشاشة،
 * أو يسجّل فيديو للشاشة، أو يسجّل صوته — والبلاغ بيوصل لوحة المالك
 * (/superadmin/support/) فوراً. الزرار بيظهر بس لمستخدم داخل على شركة
 * (السيرفر بيرد enabled=false على الدومين الرئيسي).
 * ===================================================================== */
(function () {
  "use strict";
  if (window.__mtSupportWidget) return;
  window.__mtSupportWidget = true;

  var EN = (document.documentElement.lang || "").toLowerCase().indexOf("en") === 0;
  var T = EN ? {
    fab: "Problem? Contact us", title: "Tell us what's wrong", sub: "Write it, or send a photo, a video or your voice — we see it right away.",
    ph: "What happened? What were you trying to do?", urgent: "Urgent — work is stopped",
    files: "Photo / video", shot: "Screenshot", rec: "Record screen", voice: "Record voice",
    stop: "Stop", send: "Send", sending: "Sending…", close: "Close", max: "Max 6 files",
    empty: "Write the problem or attach something.", micErr: "Microphone not available — allow it from the browser.",
    scrErr: "Screen capture was cancelled or isn't supported on this device.", netErr: "Couldn't send — check the internet and try again.",
    voiceLbl: "Voice note", shotLbl: "Screenshot", recLbl: "Screen video",
    mine: "My tickets", follow: "Follow up on it", newReply: "new reply"
  } : {
    fab: "مشكلة؟ كلّمنا", title: "قولّنا إيه المشكلة", sub: "اكتبها، أو ابعت صورة أو فيديو أو سجّل بصوتك — بتوصلنا في لحظتها.",
    ph: "إيه اللي حصل؟ كنت بتحاول تعمل إيه؟", urgent: "عاجل — الشغل واقف",
    files: "صورة / فيديو", shot: "صوّر الشاشة", rec: "سجّل الشاشة فيديو", voice: "سجّل بصوتك",
    stop: "إيقاف", send: "ابعت", sending: "بيتبعت…", close: "إغلاق", max: "أقصى حاجة 6 ملفات",
    empty: "اكتب المشكلة أو ابعت مرفق.", micErr: "المايك مش متاح — اسمح بيه من المتصفح.",
    scrErr: "تصوير الشاشة اتلغى أو مش مدعوم على الجهاز ده.", netErr: "ماتبعتش — اتأكد من النت وجرّب تاني.",
    voiceLbl: "تسجيل صوتي", shotLbl: "صورة الشاشة", recLbl: "فيديو الشاشة",
    mine: "تذاكري", follow: "تابع البلاغ", newReply: "رد جديد"
  };
  var MAX_FILES = 6, csrf = "", unread = 0, ticketsUrl = "/support/tickets/";
  var items = [];          // {blob, name, kind}
  var recorder = null, recTimer = null, recStart = 0;

  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) e.setAttribute(k, attrs[k]);
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; }); }

  var CSS = "" +
    ".mt-sw-fab{position:fixed;bottom:18px;left:18px;z-index:2147483000;display:flex;align-items:center;gap:8px;" +
    "background:linear-gradient(135deg,#e11d48,#f97316);color:#fff;border:0;border-radius:999px;padding:11px 16px;" +
    "font:800 13px Cairo,Tahoma,sans-serif;box-shadow:0 8px 24px rgba(225,29,72,.35);cursor:pointer}" +
    ".mt-sw-fab:hover{filter:brightness(1.08)}" +
    ".mt-sw-dot{background:#fff;color:#e11d48;border-radius:999px;padding:0 7px;font-size:11px;font-weight:900}" +
    ".mt-sw-mine{display:inline-block;float:left;color:#93c5fd;font-size:12px;font-weight:800;text-decoration:none;margin-top:2px}" +
    "[dir=ltr] .mt-sw-mine{float:right}" +
    ".mt-sw-msg a{color:#93c5fd;font-weight:800;margin-inline-start:6px}" +
    ".mt-sw-back{position:fixed;inset:0;z-index:2147483001;background:rgba(2,6,23,.7);display:none;align-items:flex-end;justify-content:center}" +
    ".mt-sw-back.open{display:flex}" +
    "@media(min-width:640px){.mt-sw-back{align-items:center}}" +
    ".mt-sw-box{width:100%;max-width:520px;max-height:92vh;overflow:auto;background:#0f172a;color:#e2e8f0;border:1px solid #334155;" +
    "border-radius:18px 18px 0 0;padding:18px;font-family:Cairo,Tahoma,sans-serif;box-sizing:border-box}" +
    "@media(min-width:640px){.mt-sw-box{border-radius:18px}}" +
    ".mt-sw-box h3{margin:0 0 4px;font-size:17px;font-weight:900;color:#fda4af}" +
    ".mt-sw-box p.sub{margin:0 0 12px;font-size:12px;color:#94a3b8}" +
    ".mt-sw-box textarea{width:100%;min-height:96px;box-sizing:border-box;background:#020617;color:#e2e8f0;border:1px solid #475569;" +
    "border-radius:10px;padding:10px;font:14px Cairo,Tahoma,sans-serif;resize:vertical}" +
    ".mt-sw-tools{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}" +
    ".mt-sw-tools button{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:10px;padding:10px 8px;" +
    "font:700 12px Cairo,Tahoma,sans-serif;cursor:pointer}" +
    ".mt-sw-tools button.rec{background:#7f1d1d;border-color:#ef4444;color:#fecaca}" +
    ".mt-sw-tools button:disabled{opacity:.45;cursor:not-allowed}" +
    ".mt-sw-list{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}" +
    ".mt-sw-item{position:relative;width:76px;height:76px;border-radius:10px;overflow:hidden;background:#1e293b;border:1px solid #334155;" +
    "display:flex;align-items:center;justify-content:center;font-size:11px;text-align:center;color:#cbd5e1}" +
    ".mt-sw-item img,.mt-sw-item video{width:100%;height:100%;object-fit:cover}" +
    ".mt-sw-item button{position:absolute;top:2px;left:2px;width:22px;height:22px;border-radius:50%;border:0;background:rgba(0,0,0,.7);" +
    "color:#fff;cursor:pointer;font-size:12px;line-height:22px;padding:0}" +
    ".mt-sw-urgent{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:#fca5a5;margin:6px 0}" +
    ".mt-sw-msg{font-size:12px;margin:8px 0;min-height:16px}" +
    ".mt-sw-msg.err{color:#fca5a5}.mt-sw-msg.ok{color:#6ee7b7;font-weight:800}" +
    ".mt-sw-bar{height:6px;background:#1e293b;border-radius:6px;overflow:hidden;display:none;margin:6px 0}" +
    ".mt-sw-bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#e11d48,#f97316)}" +
    ".mt-sw-actions{display:flex;gap:8px;margin-top:10px}" +
    ".mt-sw-actions button{flex:1;border:0;border-radius:10px;padding:12px;font:800 14px Cairo,Tahoma,sans-serif;cursor:pointer}" +
    ".mt-sw-send{background:linear-gradient(135deg,#e11d48,#f97316);color:#fff}" +
    ".mt-sw-close{background:#334155;color:#e2e8f0;flex:0 0 auto!important;padding:12px 18px!important}";

  function build() {
    var style = el("style", {}, CSS);
    document.head.appendChild(style);
    var dir = EN ? "ltr" : "rtl";
    var fab = el("button", { type: "button", "class": "mt-sw-fab", "aria-label": T.fab },
                 "🆘 <span>" + esc(T.fab) + "</span>" + (unread ? ' <span class="mt-sw-dot" title="' + esc(T.newReply) + '">' + unread + "</span>" : ""));
    var back = el("div", { "class": "mt-sw-back", dir: dir, role: "dialog", "aria-modal": "true" });
    back.innerHTML =
      '<div class="mt-sw-box">' +
      '<a class="mt-sw-mine" href="' + esc(ticketsUrl) + '">🎫 ' + esc(T.mine) + (unread ? " (" + unread + " " + esc(T.newReply) + ")" : "") + "</a>" +
      "<h3>🆘 " + esc(T.title) + '</h3><p class="sub">' + esc(T.sub) + "</p>" +
      '<textarea maxlength="5000" placeholder="' + esc(T.ph) + '"></textarea>' +
      '<label class="mt-sw-urgent"><input type="checkbox" class="mt-sw-urg"> ' + esc(T.urgent) + "</label>" +
      '<div class="mt-sw-tools">' +
      '<button type="button" data-a="files">📷 ' + esc(T.files) + "</button>" +
      '<button type="button" data-a="voice">🎙️ ' + esc(T.voice) + "</button>" +
      '<button type="button" data-a="shot">🖥️ ' + esc(T.shot) + "</button>" +
      '<button type="button" data-a="rec">🎥 ' + esc(T.rec) + "</button>" +
      "</div>" +
      '<input type="file" class="mt-sw-file" accept="image/*,video/*" multiple hidden>' +
      '<div class="mt-sw-list"></div>' +
      '<div class="mt-sw-bar"><i></i></div><div class="mt-sw-msg"></div>' +
      '<div class="mt-sw-actions"><button type="button" class="mt-sw-send">' + esc(T.send) + "</button>" +
      '<button type="button" class="mt-sw-close">' + esc(T.close) + "</button></div>" +
      "</div>";
    document.body.appendChild(fab);
    document.body.appendChild(back);

    var $ = function (s) { return back.querySelector(s); };
    var msg = $(".mt-sw-msg"), list = $(".mt-sw-list"), fileIn = $(".mt-sw-file");
    var hasScreen = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
    var hasMic = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
    if (!hasScreen) { $('[data-a="shot"]').style.display = "none"; $('[data-a="rec"]').style.display = "none"; }
    if (!hasMic) $('[data-a="voice"]').style.display = "none";

    function say(text, kind) { msg.textContent = text || ""; msg.className = "mt-sw-msg " + (kind || ""); }
    function open() { back.classList.add("open"); say(""); setTimeout(function () { $("textarea").focus(); }, 50); }
    function close() { if (recorder) stopRec(); back.classList.remove("open"); }

    function render() {
      list.innerHTML = "";
      items.forEach(function (it, i) {
        var box = el("div", { "class": "mt-sw-item", title: it.name });
        var url = URL.createObjectURL(it.blob);
        if (it.kind === "image") box.appendChild(el("img", { src: url, alt: "" }));
        else if (it.kind === "video") box.appendChild(el("video", { src: url, muted: "" }));
        else box.innerHTML = "🎙️<br>" + esc(it.label || T.voiceLbl);
        var x = el("button", { type: "button", "aria-label": "x" }, "✕");
        x.onclick = function () { items.splice(i, 1); render(); };
        box.appendChild(x);
        list.appendChild(box);
      });
    }
    function add(blob, name, kind, label) {
      if (items.length >= MAX_FILES) { say(T.max, "err"); return; }
      items.push({ blob: blob, name: name, kind: kind, label: label });
      render();
    }

    fileIn.onchange = function () {
      Array.prototype.forEach.call(fileIn.files, function (f) {
        var kind = (f.type || "").split("/")[0];
        if (kind === "image" || kind === "video") add(f, f.name, kind);
      });
      fileIn.value = "";
    };

    function pickType(cands) {
      for (var i = 0; i < cands.length; i++) if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(cands[i])) return cands[i];
      return "";
    }
    function startRec(stream, kind, btn) {
      var type = kind === "audio" ? pickType(["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"])
                                  : pickType(["video/webm;codecs=vp9", "video/webm", "video/mp4"]);
      var chunks = [];
      recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        var mime = (recorder && recorder.mimeType) || type || (kind + "/webm");
        var ext = mime.indexOf("mp4") > -1 ? "mp4" : (mime.indexOf("ogg") > -1 ? "ogg" : "webm");
        if (chunks.length) add(new Blob(chunks, { type: mime.split(";")[0] }), kind + "-note." + ext, kind,
                               kind === "audio" ? T.voiceLbl : T.recLbl);
        recorder = null; clearInterval(recTimer);
        back.querySelectorAll(".mt-sw-tools button").forEach(function (b) { b.disabled = false; b.classList.remove("rec"); });
        $('[data-a="voice"]').innerHTML = "🎙️ " + esc(T.voice);
        $('[data-a="rec"]').innerHTML = "🎥 " + esc(T.rec);
        back.classList.add("open");
      };
      // المشاركة اتقفلت من شريط المتصفح ⇒ نوقف التسجيل
      stream.getTracks().forEach(function (t) { t.onended = function () { if (recorder) stopRec(); }; });
      recorder.start(1000);
      recStart = Date.now();
      back.querySelectorAll(".mt-sw-tools button").forEach(function (b) { if (b !== btn) b.disabled = true; });
      btn.classList.add("rec");
      var maxSec = kind === "audio" ? 180 : 90;
      recTimer = setInterval(function () {
        var sec = Math.floor((Date.now() - recStart) / 1000);
        btn.innerHTML = "⏹️ " + esc(T.stop) + " " + Math.floor(sec / 60) + ":" + ("0" + (sec % 60)).slice(-2);
        if (sec >= maxSec) stopRec();
      }, 500);
    }
    function stopRec() { if (recorder && recorder.state !== "inactive") recorder.stop(); }

    back.querySelector(".mt-sw-tools").addEventListener("click", function (ev) {
      var btn = ev.target.closest("button");
      if (!btn) return;
      var a = btn.getAttribute("data-a");
      if (recorder) { stopRec(); return; }
      if (items.length >= MAX_FILES) { say(T.max, "err"); return; }
      say("");
      if (a === "files") fileIn.click();
      else if (a === "voice") {
        navigator.mediaDevices.getUserMedia({ audio: true })
          .then(function (s) { startRec(s, "audio", btn); })
          .catch(function () { say(T.micErr, "err"); });
      } else if (a === "shot" || a === "rec") {
        back.classList.remove("open");      // المودال مايظهرش في الصورة
        navigator.mediaDevices.getDisplayMedia({ video: true, audio: false }).then(function (stream) {
          if (a === "rec") { startRec(stream, "video", btn); return; }
          var v = el("video");
          v.srcObject = stream; v.muted = true;
          v.onloadedmetadata = function () {
            v.play();
            setTimeout(function () {
              var c = el("canvas");
              c.width = v.videoWidth; c.height = v.videoHeight;
              c.getContext("2d").drawImage(v, 0, 0);
              stream.getTracks().forEach(function (t) { t.stop(); });
              c.toBlob(function (b) { if (b) add(b, "screenshot.png", "image", T.shotLbl); back.classList.add("open"); }, "image/png");
            }, 400);
          };
        }).catch(function () { back.classList.add("open"); say(T.scrErr, "err"); });
      }
    });

    $(".mt-sw-send").onclick = function () {
      var text = $("textarea").value.trim();
      if (!text && !items.length) { say(T.empty, "err"); return; }
      var fd = new FormData();
      fd.append("message", text);
      fd.append("page_url", location.href);
      if ($(".mt-sw-urg").checked) fd.append("urgent", "1");
      items.forEach(function (it) { fd.append(it.kind === "audio" ? "voice" : "files", it.blob, it.name); });
      var sendBtn = $(".mt-sw-send"), bar = $(".mt-sw-bar"), fill = bar.querySelector("i");
      sendBtn.disabled = true; sendBtn.textContent = T.sending; bar.style.display = "block"; fill.style.width = "0";
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/support/report/");
      xhr.setRequestHeader("X-CSRFToken", csrf);
      xhr.upload.onprogress = function (e) { if (e.lengthComputable) fill.style.width = Math.round(e.loaded * 100 / e.total) + "%"; };
      xhr.onload = function () {
        var data = {};
        try { data = JSON.parse(xhr.responseText); } catch (e) { /* رد مش JSON */ }
        sendBtn.disabled = false; sendBtn.textContent = T.send; bar.style.display = "none";
        if (xhr.status === 200 && data.ok) {
          items = []; render(); $("textarea").value = ""; $(".mt-sw-urg").checked = false;
          say(data.message, "ok");
          if (data.ticket_url) msg.insertAdjacentHTML("beforeend", '<a href="' + esc(data.ticket_url) + '">' + esc(T.follow) + " ←</a>");
        } else {
          say(data.error || T.netErr, "err");
        }
      };
      xhr.onerror = function () { sendBtn.disabled = false; sendBtn.textContent = T.send; bar.style.display = "none"; say(T.netErr, "err"); };
      xhr.send(fd);
    };

    fab.onclick = open;
    $(".mt-sw-close").onclick = close;
    back.addEventListener("click", function (e) { if (e.target === back) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && back.classList.contains("open")) close(); });
  }

  function boot() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/support/report/");
    xhr.onload = function () {
      try {
        var d = JSON.parse(xhr.responseText);
        if (d.enabled) { csrf = d.csrf || ""; unread = d.unread || 0; ticketsUrl = d.tickets_url || ticketsUrl; build(); }
      } catch (e) { /* مش صفحة شركة */ }
    };
    xhr.send();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
