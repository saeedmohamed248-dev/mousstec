/*
 * app.js — The Senses of the Smart Parts Robot.
 *
 * Runs in the Android phone's browser (inside the robot's head). It:
 *   1. Listens to the customer with the Web Speech API (Speech-to-Text).
 *   2. Sends the transcript to the backend /api/chat.
 *   3. Reads the AI's reply aloud (Text-to-Speech).
 *   4. On demand, opens the camera and scans a barcode/QR with html5-qrcode,
 *      posting the decoded value to /api/scan.
 *
 * The backend base URL is auto-derived from wherever this page is served,
 * so opening http://<laptop-ip>:8000 on the phone "just works".
 */

(() => {
  "use strict";

  const API = window.location.origin; // backend serves this page too
  const $ = (id) => document.getElementById(id);

  const app = $("app");
  const transcriptEl = $("transcript");
  const stateLabel = $("stateLabel");
  const statusDot = $("statusDot");
  const micBtn = $("micBtn");
  const scanBtn = $("scanBtn");
  const hint = $("hint");

  let sessionId = null;
  let recognition = null;
  let listening = false;
  let scanner = null; // Html5Qrcode instance
  let scanning = false;
  let currentLang = "en-US"; // switches to Arabic if the user speaks Arabic

  // ---------------------------------------------------------------------
  // UI helpers
  // ---------------------------------------------------------------------

  function setState(state, label) {
    app.className = "app"; // reset
    if (state) app.classList.add("state-" + state);
    stateLabel.textContent = label || "";
  }

  function addBubble(text, who) {
    const b = document.createElement("div");
    b.className = "bubble " + who;
    const tag = document.createElement("span");
    tag.className = "who";
    tag.textContent = who === "bot" ? "MOUS" : "YOU";
    b.appendChild(tag);
    b.appendChild(document.createTextNode(text));
    transcriptEl.appendChild(b);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  function addScanChip(code) {
    const c = document.createElement("div");
    c.className = "scan-chip";
    c.textContent = "📷 Scanned: " + code;
    transcriptEl.appendChild(c);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  // Detect Arabic so we can set the TTS/STT voice accordingly.
  function looksArabic(text) {
    return /[؀-ۿ]/.test(text);
  }

  // ---------------------------------------------------------------------
  // Text-to-Speech (robot voice)
  // ---------------------------------------------------------------------

  function speak(text) {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel(); // stop any in-progress speech
    const u = new SpeechSynthesisUtterance(text);
    u.lang = looksArabic(text) ? "ar-SA" : "en-US";
    u.rate = 1.0;
    u.pitch = 1.0;

    // Pause listening while speaking so the robot doesn't hear itself.
    if (listening) stopListening(true);

    u.onstart = () => setState("speaking", "speaking");
    u.onend = () => setState("", "tap the mic to talk");
    window.speechSynthesis.speak(u);
  }

  // ---------------------------------------------------------------------
  // Backend calls
  // ---------------------------------------------------------------------

  async function startSession() {
    try {
      const res = await fetch(API + "/api/session", { method: "POST" });
      const data = await res.json();
      sessionId = data.session_id;
      statusDot.textContent = "● online";
      statusDot.style.color = "var(--ok)";
      addBubble(data.reply, "bot");
      speak(data.reply);
    } catch (e) {
      statusDot.textContent = "● offline";
      statusDot.style.color = "var(--danger)";
      addBubble("I can't reach my brain (backend). Please check the connection.", "bot");
    }
  }

  async function sendMessage(text) {
    addBubble(text, "user");
    setState("thinking", "thinking…");
    try {
      const res = await fetch(API + "/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      const data = await res.json();
      sessionId = data.session_id;
      addBubble(data.reply, "bot");
      speak(data.reply);
    } catch (e) {
      setState("", "tap the mic to talk");
      addBubble("Sorry, I lost my connection. Could you try again?", "bot");
    }
  }

  async function sendScan(code) {
    addScanChip(code);
    setState("thinking", "checking…");
    try {
      const res = await fetch(API + "/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code, session_id: sessionId }),
      });
      const data = await res.json();
      sessionId = data.session_id;
      addBubble(data.reply, "bot");
      speak(data.reply);
    } catch (e) {
      setState("", "tap the mic to talk");
      addBubble("I couldn't verify that code. Please try scanning again.", "bot");
    }
  }

  // ---------------------------------------------------------------------
  // Speech-to-Text (Web Speech API)
  // ---------------------------------------------------------------------

  function initRecognition() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      hint.textContent = "Voice input isn't supported on this browser — use Chrome on Android.";
      micBtn.disabled = true;
      return;
    }
    recognition = new SR();
    recognition.lang = currentLang;
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.continuous = false;

    recognition.onresult = (event) => {
      const text = event.results[0][0].transcript.trim();
      if (!text) return;
      // Adapt the recognizer's language for the next turn.
      currentLang = looksArabic(text) ? "ar-SA" : "en-US";
      sendMessage(text);
    };

    recognition.onerror = (e) => {
      listening = false;
      micBtn.classList.remove("on");
      if (e.error === "not-allowed") {
        hint.textContent = "Microphone permission is required.";
      }
      setState("", "tap the mic to talk");
    };

    recognition.onend = () => {
      // Fires after each utterance; reset the button unless we're speaking.
      if (listening) {
        listening = false;
        micBtn.classList.remove("on");
        micBtn.textContent = "🎙️ TALK";
      }
    };
  }

  function startListening() {
    if (!recognition || listening) return;
    // Stop TTS so the robot doesn't transcribe its own voice.
    window.speechSynthesis && window.speechSynthesis.cancel();
    try {
      recognition.lang = currentLang;
      recognition.start();
      listening = true;
      micBtn.classList.add("on");
      micBtn.textContent = "● LISTENING";
      setState("listening", "listening…");
    } catch (_) {
      /* start() throws if already started — ignore */
    }
  }

  function stopListening(silent) {
    if (!recognition) return;
    listening = false;
    micBtn.classList.remove("on");
    micBtn.textContent = "🎙️ TALK";
    try { recognition.stop(); } catch (_) {}
    if (!silent) setState("", "tap the mic to talk");
  }

  // ---------------------------------------------------------------------
  // Camera / barcode scanning (html5-qrcode)
  // ---------------------------------------------------------------------

  async function startScanning() {
    if (scanning) { stopScanning(); return; }
    const readerEl = $("reader");
    readerEl.classList.add("active");
    scanBtn.classList.add("on");
    scanBtn.textContent = "✕ CLOSE";
    setState("", "hold the barcode steady…");
    stopListening(true);

    scanner = new Html5Qrcode("reader");
    const config = { fps: 10, qrbox: { width: 240, height: 160 } };

    try {
      await scanner.start(
        { facingMode: "environment" }, // rear camera
        config,
        (decodedText) => {
          // Got a code — stop scanning and send it to the brain.
          stopScanning();
          sendScan(decodedText);
        },
        () => { /* per-frame scan failure: ignore, keep trying */ }
      );
    } catch (err) {
      readerEl.classList.remove("active");
      scanBtn.classList.remove("on");
      scanBtn.textContent = "📷 SCAN";
      addBubble("I couldn't open the camera. Please grant camera permission.", "bot");
    }
  }

  function stopScanning() {
    scanning = false;
    scanBtn.classList.remove("on");
    scanBtn.textContent = "📷 SCAN";
    const readerEl = $("reader");
    if (scanner) {
      scanner.stop().then(() => scanner.clear()).catch(() => {}).finally(() => {
        readerEl.classList.remove("active");
      });
      scanner = null;
    } else {
      readerEl.classList.remove("active");
    }
    setState("", "tap the mic to talk");
  }

  // ---------------------------------------------------------------------
  // Wire up
  // ---------------------------------------------------------------------

  micBtn.addEventListener("click", () => {
    if (listening) stopListening();
    else startListening();
  });

  scanBtn.addEventListener("click", startScanning);

  // Kick off: init voice, then greet on first user interaction (browsers
  // block autoplay audio until the user taps, so we greet after tap too).
  initRecognition();
  startSession();

  // Some mobile browsers need voices loaded before first speak().
  if ("speechSynthesis" in window) {
    window.speechSynthesis.onvoiceschanged = () => {};
  }
})();
