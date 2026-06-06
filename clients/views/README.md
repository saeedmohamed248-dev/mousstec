# `clients/views/` — Architecture Guide

The marketplace, design-store, brand-memory, and AI-pipeline views for Mouss Tec ERP.

Once a single 3,282-line `_legacy.py`. Now a flat package of focused, single-responsibility modules behind a thin facade. URL handlers stay reachable as `client_views.<name>` — no `urls.py` changes needed when an endpoint moves between modules.

---

## Module map

| Module | Purpose | Public surface |
|---|---|---|
| **`__init__.py`** | Package facade. Re-exports every public endpoint by name. | 68 endpoint symbols |
| **`_shared.py`** | Auth helpers, OTP delivery, landing-bot fallback reply. | `_marketplace_auth`, `_send_otp_via_channel`, `_landing_bot_local_reply`, `_notify_merchants_of_new_request`, `_build_customer_topup_cards`, `_is_platform_owner` |
| **`_ai_pipeline.py`** | Unified Brand + Smart Router + Composite + Quality-Gate pipeline. Used by C1/C2/C3 marketplace flows. | `_resolve_brand_context`, `_persist_remote_image`, `_composite_brand_logo`, `_run_marketplace_image_pipeline` |
| **`ai_assistant_views.py`** | Landing-page Gemini sales bot + `LANDING_BOT_KNOWLEDGE` system prompt. | `ai_assistant_api` |
| **`marketplace_core_views.py`** | Customer marketplace, sector landings, signup/login, service requests, merchant feed, admin moderation. | 19 endpoints (`marketplace_*`) |
| **`design_store_views.py`** | AI design-store: browse, buy, generate, regenerate, refine, download, watermark, send-to-print, chat history. | 14 endpoints (`design_store_*`) |
| **`brand_profile_views.py`** | Customer Brand Profile CRUD (GET/POST/DELETE) + logo-slot delete + editor page. | `brand_profile_view`, `brand_profile_delete_logo`, `brand_profile_page` |
| **`design_chat_views.py`** | Conversational Design Builder (Phase N — start, message, undo, finalize, state, page). | 6 endpoints (`design_chat_*`) |
| **`auth_views.py`** | Tenant SaaS signup, sector landing pages, smart post-login redirect, account recovery. | 8 endpoints |
| **`subscription_views.py`** | Pricing page, Paymob checkout/callback, manage subscription, add-on purchase. | 6 endpoints |
| **`admin_views.py`** | Super-admin dashboard, customer detail, tenant grants, impersonation, enter-tenant. | 5 endpoints |
| **`b2b_views.py`** | B2B marketplace search, blind bidding, escrow wallet, demand predictor. | 5 endpoints |
| **`webhook_views.py`** | Universal webhook multiplexer (Paymob, Twilio, etc.). | `universal_webhook_multiplexer` |

---

## The facade pattern (`__init__.py`)

```python
from .marketplace_core_views import (
    marketplace_home, marketplace_register, marketplace_login, ...
)
from .design_store_views import (
    design_store_generate, design_store_regenerate, design_store_refine, ...
)
# ...one explicit block per module
```

### Why explicit re-exports, not `from .X import *`?

Under Daphne (ASGI), wildcard imports were observed to drop names intermittently at boot, surfacing as `AttributeError` in `erp_core/urls.py` → HTTP 502 on the live site. Explicit lists are deterministic and self-documenting — each block in the facade is the canonical inventory of one module's public surface.

### Adding a new endpoint

1. Implement the view in the module that owns its domain (e.g. a new design-store action → `design_store_views.py`).
2. Add the name to that module's `from .X import (...)` block in `__init__.py`.
3. Reference it from `erp_core/urls.py` as `client_views.<name>` — no other plumbing.

### Moving an endpoint between modules

1. Cut the function (and any module-private helpers it owns) to the new module.
2. Move its name from the old module's block to the new one in `__init__.py`.
3. URL surface is unaffected — `client_views.<name>` keeps resolving.

---

## Layering rules

To keep the package free of import cycles:

- **`_shared.py`** and **`_ai_pipeline.py`** are leaf modules — they may **not** import from any other `clients.views.*` module.
- **View modules** (`marketplace_core_views`, `design_store_views`, etc.) may import from `_shared` and `_ai_pipeline`, but **not from each other**. If you find yourself wanting to, the helper belongs in `_shared` or its own leaf.
- **`__init__.py`** imports from view modules only — it is the integration layer, never the source of logic.

---

## The C1/C2/C3 pipeline contract

All three customer-facing AI flows (`design_store_generate`, `_regenerate`, `_refine`) share the same four-stage pipeline implemented in `_ai_pipeline.py`:

```
Stage A — Brand context resolution    (_resolve_brand_context)
Stage B — Compose mega-prompt         (compose_mega_prompt, already_engineered=True)
Stage C — Smart Router                (generate_design_image: FLUX or Ideogram)
Stage D — Logo composite              (_composite_brand_logo, FLUX-only)
Stage E — Quality Gate                (verify_design_quality)
```

This is the single source of truth for marketplace AI generation. The wrapper `_run_marketplace_image_pipeline` returns a structured `{ok, image_url, used_engine, used_model, quality_score, logo_composited, brand_applied, ...}` envelope consumed identically by all three endpoints.

**Regression guard:** `clients/tests/test_design_store_pipeline.py` mocks every AI boundary (compose / generate / composite / quality_gate) and asserts each endpoint invokes the full pipeline. Runs in <2s without touching the tenant test base.

---

## Testing

- **Pipeline tests** — `clients.tests.test_design_store_pipeline` (5 tests, plain `TestCase`, mocked AI boundaries). Safe to run in isolation, no tenant teardown.
- **Brand pipeline tests** — `clients.tests.test_brand_design_pipeline` (156 tests, covers compose_mega_prompt, smart router routing, brand-context bridge, fast-path).
- Both suites import view callables directly (`from clients.views import design_store_views as legacy_views`) — when moving endpoints, update test imports accordingly.

```bash
# Fast feedback loop — no tenant DB
python manage.py test clients.tests.test_design_store_pipeline clients.tests.test_brand_design_pipeline
```

---

## History

Originally a single `clients/views.py` (~4,936 LOC, all marketplace + ERP + admin code mixed together). Split incrementally:

| Step | Module extracted | LOC moved |
|---|---|---|
| 0 (pre-session) | `auth_views`, `subscription_views`, `admin_views`, `b2b_views`, `webhook_views`, `design_chat_views`, `_shared` | ~1,650 |
| 2 | `_ai_pipeline` | 236 |
| 3 | `brand_profile_views` | 213 |
| 4 | `design_store_views` | 2,040 |
| 5 | `ai_assistant_views`, `marketplace_core_views` → **`_legacy.py` deleted** | 1,074 |

End state: 12 focused modules, 0 legacy stub, 161 tests green.
