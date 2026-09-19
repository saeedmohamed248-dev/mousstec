"""
main.py — The Brain of the Smart Parts Robot.

A FastAPI backend that:
  * Receives text (transcribed speech) or scanned barcode data from the
    Android web app in the robot's head.
  * Uses an LLM (Anthropic / OpenAI / Gemini — auto-detected from env, with
    a rule-based fallback so it runs with NO api key) as a "helpful, expert
    BMW / MINI parts specialist robot".
  * Gives the LLM tools that hit the company ERP (mocked in database.py):
    stock check, price check, customer lookup, and return/warranty
    validation.
  * Keeps per-session conversation context so the robot remembers the chat.

Run:  uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import database as db

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Provider is auto-detected from whichever API key is present. Force one by
# setting ROBOT_AI_PROVIDER=anthropic|openai|gemini|mock.
AI_PROVIDER = os.getenv("ROBOT_AI_PROVIDER", "auto").lower()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")

ANTHROPIC_MODEL = os.getenv("ROBOT_ANTHROPIC_MODEL", "claude-sonnet-4-5")
OPENAI_MODEL = os.getenv("ROBOT_OPENAI_MODEL", "gpt-4o-mini")
GEMINI_MODEL = os.getenv("ROBOT_GEMINI_MODEL", "gemini-1.5-flash")

SYSTEM_PROMPT = (
    "You are 'MOUS', a friendly, expert customer-service robot for a BMW / "
    "MINI spare-parts store. Your body is built from recycled BMW parts and "
    "you speak to walk-in customers out loud, so keep replies SHORT, warm "
    "and conversational (1-3 sentences) — they will be read aloud by a "
    "text-to-speech voice. You are fluent in both Arabic and English; reply "
    "in the same language the customer used.\n\n"
    "You can help with two things:\n"
    "  1. SALES — checking whether a part is in stock and its price.\n"
    "  2. RETURNS / WARRANTY — checking if an item can be returned or is "
    "still under warranty.\n\n"
    "Rules:\n"
    "  * ALWAYS use your tools to get real stock, prices, customer and "
    "return data. Never invent a price, a stock number or a warranty result.\n"
    "  * For a return, if you don't have an invoice number, ask the customer "
    "to hold the invoice or part barcode up to your camera, OR to tell you "
    "their phone number.\n"
    "  * When a return is eligible or an item is under warranty, tell the "
    "customer the good news and direct them to the cashier. When it isn't, "
    "explain kindly why (e.g. the return window has passed).\n"
    "  * If a part isn't found, say so and offer to check a different part.\n"
    "  * Be concise and never read out raw JSON or part numbers unless the "
    "customer asks."
)


# ---------------------------------------------------------------------------
# Tool definitions — shared by all providers
# ---------------------------------------------------------------------------

# Canonical tool spec (JSON-schema). Each provider adapter reshapes this.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_part_availability",
        "description": (
            "Check whether a spare part is in stock. Accepts a part number "
            "or a free-text description (e.g. 'BMW F30 steering rack')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Part number or description to look up.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_price",
        "description": "Get the current selling price of a spare part.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Part number or description to price.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_customer_by_phone",
        "description": "Look up a customer's profile by their phone number.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Customer phone number."}
            },
            "required": ["phone"],
        },
    },
    {
        "name": "validate_return",
        "description": (
            "Validate return / warranty eligibility for a purchase. Provide "
            "the invoice number (e.g. decoded from a scanned barcode) and/or "
            "the customer's phone number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "Invoice number, often from the barcode/QR.",
                },
                "phone": {
                    "type": "string",
                    "description": "Customer phone number (fallback lookup).",
                },
            },
        },
    },
]

# Map tool names to the real Python functions in database.py.
TOOL_IMPL = {
    "check_part_availability": db.check_part_availability,
    "check_price": db.check_price,
    "get_customer_by_phone": db.get_customer_by_phone,
    "validate_return": db.validate_return,
}


def _run_tool(name: str, args: dict) -> dict:
    """Execute a tool call against the ERP layer, safely."""
    impl = TOOL_IMPL.get(name)
    if impl is None:
        return {"error": f"unknown tool {name}"}
    try:
        return impl(**args)
    except Exception as exc:  # never crash the conversation on bad args
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Session memory (in-process). For multi-worker prod, back this with Redis.
# ---------------------------------------------------------------------------

# session_id -> list of provider-neutral messages: {"role", "content"}
_SESSIONS: dict[str, list[dict[str, str]]] = {}


def _get_history(session_id: str) -> list[dict[str, str]]:
    return _SESSIONS.setdefault(session_id, [])


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def _resolve_provider() -> str:
    if AI_PROVIDER != "auto":
        return AI_PROVIDER
    if ANTHROPIC_KEY:
        return "anthropic"
    if OPENAI_KEY:
        return "openai"
    if GEMINI_KEY:
        return "gemini"
    return "mock"


PROVIDER = _resolve_provider()


# ---------------------------------------------------------------------------
# LLM adapters. Each takes the running history (list of {role, content}) and
# returns the assistant's final text reply, running tool calls as needed.
# ---------------------------------------------------------------------------

def _chat_anthropic(history: list[dict[str, str]]) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_KEY)
    tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in TOOLS
    ]
    # Convert neutral history to Anthropic message blocks.
    messages = [{"role": m["role"], "content": m["content"]} for m in history]

    while True:
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = _run_tool(block.name, block.input or {})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            continue
        # Final text answer.
        return "".join(b.text for b in resp.content if b.type == "text").strip()


def _chat_openai(history: list[dict[str, str]]) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_KEY)
    tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOLS
    ]
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]

    while True:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=tools,
            max_tokens=600,
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            messages.append(msg.model_dump(exclude_none=True))
            for call in msg.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                result = _run_tool(call.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result),
                    }
                )
            continue
        return (msg.content or "").strip()


def _chat_gemini(history: list[dict[str, str]]) -> str:
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_KEY)
    tools = [
        {
            "function_declarations": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                }
                for t in TOOLS
            ]
        }
    ]
    model = genai.GenerativeModel(
        GEMINI_MODEL, system_instruction=SYSTEM_PROMPT, tools=tools
    )
    chat = model.start_chat(
        history=[
            {
                "role": "user" if m["role"] == "user" else "model",
                "parts": [m["content"]],
            }
            for m in history[:-1]
        ]
    )
    resp = chat.send_message(history[-1]["content"])
    while True:
        parts = resp.candidates[0].content.parts
        fcalls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        if not fcalls:
            return (resp.text or "").strip()
        replies = []
        for fc in fcalls:
            result = _run_tool(fc.name, dict(fc.args or {}))
            replies.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name, response={"result": result}
                    )
                )
            )
        resp = chat.send_message(replies)


def _chat_mock(history: list[dict[str, str]]) -> str:
    """Rule-based fallback so the robot works with no API key.

    It runs a tiny intent classifier over the latest user message, calls the
    right ERP tool, and phrases a friendly reply. This keeps the whole demo
    runnable offline and doubles as documentation of the intended flow.
    """
    raw = history[-1]["content"] if history else ""
    user_msg = raw.lower()

    greet_words = ("just walked up", "opening line", "greet them")
    return_words = ("return", "warranty", "refund", "ارجاع", "استرجاع", "ضمان")
    price_words = ("price", "cost", "how much", "سعر", "بكام", "كام")

    # Opening greeting (triggered by /api/session's internal prompt).
    if any(w in user_msg for w in greet_words):
        return "Hello and welcome! I'm MOUS. How can I help you today — a part, a price, or a return?"

    if any(w in user_msg for w in return_words):
        # Try to find an invoice number in the text (INV-...) else phone.
        token = None
        for word in raw.replace(",", " ").split():
            cleaned = word.strip(".,!?;:)]}").upper()
            if cleaned.startswith("INV-"):
                token = cleaned
                break
        phone = "".join(ch for ch in user_msg if ch.isdigit())
        res = db.validate_return(invoice_number=token, phone=phone or None)
        if not res["found"]:
            return (
                "Sure, I can help with a return. Please hold your invoice or "
                "the part's barcode up to my camera, or tell me your phone "
                "number."
            )
        if res["return_eligible"]:
            return (
                f"Good news! Your {res['part_name']} is within the "
                f"{res['return_window_days']}-day return window. Please "
                "proceed to the cashier and they'll process your refund."
            )
        if res["under_warranty"]:
            return (
                f"Your {res['part_name']} is past the return window, but it's "
                "still under warranty. Please head to the cashier for a "
                "warranty claim."
            )
        return (
            f"I'm sorry — your {res['part_name']} was purchased "
            f"{res['days_since_purchase']} days ago, which is outside both "
            "the return window and the warranty period, so we can't take it "
            "back. Is there anything else I can help with?"
        )

    if any(w in user_msg for w in price_words):
        res = db.check_price(user_msg)
        if not res["found"]:
            return "Which part would you like the price for? You can name it or hold its barcode to my camera."
        return f"The {res['name']} is {res['price']:.0f} {res['currency']}. Would you like to check stock too?"

    # Default: treat as a stock inquiry.
    res = db.check_part_availability(user_msg)
    if not res["found"]:
        if not user_msg.strip():
            return "Hello and welcome! I'm MOUS. Which BMW or MINI part are you looking for today?"
        return "I couldn't find that part. Could you tell me the exact part name, or hold its barcode up to my camera?"
    if res["in_stock"]:
        price = db.check_price(res["part_number"])
        return (
            f"Yes! We have the {res['name']} in stock ({res['stock']} available, "
            f"{res['location']}), price {price['price']:.0f} {price['currency']}. "
            "Shall I hold one for you?"
        )
    return f"The {res['name']} is currently out of stock. Would you like me to check a compatible alternative?"


_CHAT_ADAPTERS = {
    "anthropic": _chat_anthropic,
    "openai": _chat_openai,
    "gemini": _chat_gemini,
    "mock": _chat_mock,
}


def generate_reply(session_id: str, user_text: str) -> str:
    """Append the user's turn, run the LLM, store & return the reply."""
    history = _get_history(session_id)
    history.append({"role": "user", "content": user_text})
    adapter = _CHAT_ADAPTERS.get(PROVIDER, _chat_mock)
    try:
        reply = adapter(history)
    except Exception as exc:
        # If the live provider fails (bad key, network), fall back gracefully.
        reply = _chat_mock(history)
        reply += ""  # keep silent about the internal error to the customer
        print(f"[robot] provider '{PROVIDER}' failed, used fallback: {exc}")
    history.append({"role": "assistant", "content": reply})
    # Keep memory bounded (last ~20 turns).
    if len(history) > 40:
        del history[: len(history) - 40]
    return reply


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Smart Parts Robot — Brain", version="1.0.0")

# The Android browser talks to us over the LAN, so allow cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str


class ScanRequest(BaseModel):
    code: str  # decoded barcode / QR text (invoice number or part number)
    session_id: Optional[str] = None


@app.get("/api/health")
def health() -> dict:
    """Report which AI provider is active — handy for setup debugging."""
    return {"status": "ok", "provider": PROVIDER}


@app.post("/api/session")
def new_session() -> dict:
    """Start a fresh conversation and return a greeting."""
    session_id = str(uuid.uuid4())
    greeting = generate_reply(
        session_id,
        "A customer has just walked up to you. Greet them warmly and ask how "
        "you can help. (This is your opening line.)",
    )
    return {"session_id": session_id, "reply": greeting}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Main conversational endpoint — receives transcribed speech."""
    session_id = req.session_id or str(uuid.uuid4())
    reply = generate_reply(session_id, req.message)
    return ChatResponse(reply=reply, session_id=session_id)


@app.post("/api/scan", response_model=ChatResponse)
def scan(req: ScanRequest) -> ChatResponse:
    """Receive a scanned barcode/QR and let the AI react to it in context."""
    session_id = req.session_id or str(uuid.uuid4())
    # Hand the scan to the AI as a user turn so it decides what to do next
    # (stock lookup for a part number, return validation for an invoice, …).
    prompt = (
        f"[The customer just scanned this code with the camera: {req.code}]. "
        "Use your tools to look it up (it may be an invoice number for a "
        "return, or a part number for a sale) and tell them the result."
    )
    reply = generate_reply(session_id, prompt)
    return ChatResponse(reply=reply, session_id=session_id)


# ---------------------------------------------------------------------------
# Serve the frontend (so the Android phone just opens http://<laptop-ip>:8000)
# ---------------------------------------------------------------------------

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_FRONTEND_DIR):

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))

    app.mount(
        "/static", StaticFiles(directory=_FRONTEND_DIR), name="static"
    )
