/* 🎥 تسجيل شاشة الجلسة (rrweb) — عشان فريق الدعم يشوف المشكلة زي ما حصلت بالظبط.
 * - كلمات السر مابتتسجّلش، وأي عنصر عليه class="rr-block" أو "rr-mask" بيتخفى.
 * - الجلسة (sid) والعداد (seq) في sessionStorage ⇒ التنقّل بين الصفحات = جلسة واحدة.
 * - اللقطة الكاملة بتتبعت فوراً، والباقي كل ثانيتين، وآخر دفعة بـ sendBeacon (مقسّمة) وقت قفل الصفحة.
 */
(function () {
    'use strict';
    if (!window.rrwebRecord || window.__mtScreenRec) return;
    window.__mtScreenRec = true;

    var URL = '/screen-rec/ingest/';
    var SID_KEY = 'mt_rec_sid', SEQ_KEY = 'mt_rec_seq', NOTICE_KEY = 'mt_rec_notice_v1';
    var stopped = false, buf = [], stopFn = null;

    function store(k, v) { try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch (e) { return null; } }
    function uuid4() {
        if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            var r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }
    var sid = store(SID_KEY);
    if (!sid) { sid = uuid4(); store(SID_KEY, sid); }

    function nextSeq() {
        var n = parseInt(store(SEQ_KEY) || '0', 10) || 0;
        store(SEQ_KEY, String(n + 1));
        return n;
    }

    function stop() {
        stopped = true; buf = [];
        if (stopFn) { try { stopFn(); } catch (e) { /* ignore */ } }
    }

    var BEACON_MAX = 60000;   // sendBeacon/keepalive مابيبعتوش أكتر من ٦٤KB

    function beaconAll(events) {
        // 🐛 [FIX]: الدفعة الأخيرة وقت قفل الصفحة كانت بتضيع لو أكبر من ٦٤KB — بنقسّمها
        var part = [], size = 0;
        function send() {
            if (!part.length) return;
            var body = JSON.stringify({ sid: sid, seq: nextSeq(), events: part, path: location.pathname });
            navigator.sendBeacon(URL, new Blob([body], { type: 'application/json' }));
            part = []; size = 0;
        }
        events.forEach(function (e) {
            var n = JSON.stringify(e).length;
            if (size + n > BEACON_MAX - 500) send();
            part.push(e); size += n;   // حدث واحد أكبر من الحد بيتبعت لوحده (ممكن يترفض)
        });
        send();
    }

    function flush(beacon) {
        if (stopped || !buf.length) return;
        if (beacon && navigator.sendBeacon) { beaconAll(buf.splice(0, buf.length)); return; }
        var body = JSON.stringify({ sid: sid, seq: nextSeq(), events: buf.splice(0, buf.length), path: location.pathname });
        fetch(URL, {
            method: 'POST', credentials: 'same-origin', keepalive: body.length < BEACON_MAX,
            headers: { 'Content-Type': 'application/json' }, body: body,
        }).then(function (r) {
            if (r.status === 403) { stop(); return; }
            return r.json().then(function (d) { if (d && d.stopped) stop(); });
        }).catch(function () { /* الشبكة وقعت — الدفعة دي بتضيع، مش مشكلة */ });
    }

    stopFn = window.rrwebRecord({
        emit: function (e) {
            if (stopped) return;
            buf.push(e);
            // اللقطة الكاملة للصفحة (type 2) كبيرة — بنبعتها فوراً بـ fetch عادي قبل ما الموظف يتنقّل
            if (e.type === 2 || buf.length >= 400) setTimeout(function () { flush(false); }, 0);
        },
        maskInputOptions: { password: true },
        maskTextClass: 'rr-mask',
        blockClass: 'rr-block',
        // الكتابة حرف بحرف (input: 'last' كان بيسجّلها بس لما الموظف يسيب الخانة — مايبانش في اللايف)
        sampling: { mousemove: 60, scroll: 150 },
        recordCanvas: false,
        collectFonts: false,
    });
    setInterval(function () { flush(false); }, 2000);
    window.addEventListener('pagehide', function () { flush(true); });
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(true); });

    // 📢 تنبيه واضح للموظف (مرة واحدة على الجهاز) — الشفافية مطلوبة قانونياً
    try {
        if (localStorage.getItem(NOTICE_KEY)) return;
    } catch (e) { return; }
    var en = (document.documentElement.lang || '').toLowerCase().indexOf('en') === 0;
    var bar = document.createElement('div');
    bar.className = 'rr-block';
    bar.setAttribute('role', 'status');
    // 🐛 [FIX]: على الموبايل التنبيه كان قاعد تحت فوق زرار «إتمام البيع» وأزرار الحفظ —
    // الضغطة بتقع عليه. على الشاشات الصغيرة يطلع فوق تحت الهيدر (مايغطّيش القائمة وزرار اللغة).
    var small = window.matchMedia && window.matchMedia('(max-width: 640px)').matches;
    bar.style.cssText = 'position:fixed;' + (small ? 'top:76px;' : 'bottom:12px;') + 'left:12px;right:12px;max-width:560px;margin:0 auto;z-index:2147483000;' +
        'background:#1C2430;color:#fff;border-radius:12px;padding:12px 14px;font:14px/1.6 system-ui,sans-serif;' +
        'box-shadow:0 8px 24px rgba(0,0,0,.25);display:flex;gap:10px;align-items:center;' + (en ? 'direction:ltr;' : 'direction:rtl;');
    var txt = document.createElement('span');
    txt.style.flex = '1';
    txt.textContent = en
        ? '🔴 This session is recorded so our support team can help you faster. Passwords are never recorded.'
        : '🔴 الجلسة دي بتتسجّل عشان فريق الدعم يقدر يساعدك أسرع. كلمات السر عمرها ما بتتسجّل.';
    var ok = document.createElement('button');
    ok.type = 'button';
    ok.textContent = en ? 'Got it' : 'تمام';
    ok.style.cssText = 'background:#fff;color:#1C2430;border:0;border-radius:8px;padding:6px 14px;font-weight:700;cursor:pointer;';
    ok.onclick = function () { try { localStorage.setItem(NOTICE_KEY, '1'); } catch (e) { /* ignore */ } bar.remove(); };
    bar.appendChild(txt); bar.appendChild(ok);
    (document.body || document.documentElement).appendChild(bar);
})();
