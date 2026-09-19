# 🤖 Smart Parts Robot System (MOUS)

A customer-service robot for a BMW / MINI spare-parts store. An Android phone
hidden in the robot's head is its **senses** (camera, mic, speaker, display);
a **laptop** runs the FastAPI **brain** that talks to an LLM and the company
ERP/inventory system.

```
 ┌─────────────────────┐        Wi-Fi / LAN        ┌──────────────────────────┐
 │  Android phone       │  ──── HTTP (JSON) ────►   │  Laptop  (FastAPI brain) │
 │  (robot's head)      │                           │                          │
 │  • Web Speech STT/TTS│  ◄──── AI replies ─────   │  • LLM (Claude/GPT/Gemini)│
 │  • html5-qrcode cam  │                           │  • ERP tools (mock DB)    │
 └─────────────────────┘                           └──────────────────────────┘
```

## Files

| File | Role |
|------|------|
| `backend/main.py` | FastAPI app, AI logic, tool-calling, session memory, API endpoints |
| `backend/database.py` | Mocked ERP: stock, pricing, customers, return/warranty checks |
| `frontend/index.html` | Cyberpunk/BMW UI, camera div, listening/speaking orb |
| `frontend/app.js` | Speech-to-Text, Text-to-Speech, barcode scanning, backend calls |
| `requirements.txt` | Python dependencies |

## Run it locally

```bash
cd smart_robot

# 1) Install deps (a virtualenv is recommended)
pip install -r requirements.txt

# 2) (Optional) give it a real AI brain — pick ONE provider:
export ANTHROPIC_API_KEY=sk-ant-...     # or
export OPENAI_API_KEY=sk-...            # or
export GEMINI_API_KEY=...
#   With no key set, it uses a built-in rule-based brain and still works.

# 3) Start the brain (serves the frontend too)
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

- Open the interactive API docs at <http://localhost:8000/docs>.
- Check the active provider at <http://localhost:8000/api/health>.

## Connect the phone (the robot's head)

1. Put the laptop and the Android phone on the **same Wi-Fi**.
2. Find the laptop's LAN IP (`ipconfig` / `ip addr`), e.g. `192.168.1.20`.
3. On the phone open **Chrome** and go to `http://192.168.1.20:8000`.
4. Allow **microphone** and **camera** permissions.
5. Add to Home Screen / use fullscreen for a kiosk look.

> ⚠️ The Web Speech API and camera need a *secure context*. `localhost` and
> `http://` on a LAN work in Chrome for testing; for a permanent install put
> the backend behind HTTPS (e.g. a reverse proxy with a self-signed or Let's
> Encrypt cert) so mic/camera stay enabled reliably.

## The conversational flow

1. **Greeting** — `POST /api/session` returns a spoken welcome.
2. **Inquiry** — customer speaks → STT → `POST /api/chat`.
3. **Processing** — the AI calls ERP tools:
   - *Sales*: `check_part_availability` + `check_price` → speaks stock & price.
   - *Return*: asks for phone/invoice, or to scan the barcode.
4. **Action** — camera scans code → `POST /api/scan` → `validate_return`
   checks the purchase date → AI says e.g. *"still under warranty, please
   proceed to the cashier."*

Try saying: *"Do you have a BMW F30 steering rack?"*, *"How much is an oil
filter?"*, or *"I want to return this"* then scan `INV-2025-0788` (a QR/barcode
containing that text) — it's within the return window in the mock data.

## Wiring into the real ERP

`backend/database.py` returns realistic mock data with stable return shapes.
To go live, replace each function body with real queries (this repo's Django
`inventory` / `erp_core` apps, or a SQL client) — keep the return dicts
identical and `main.py` needs no changes.
