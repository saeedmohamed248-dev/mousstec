# Omnichannel AI Automation — Add-on Architecture

A subscription add-on that lets a tenant connect **their own** WhatsApp Business
Cloud API number and Facebook Messenger page (BYOK — Bring Your Own Key). When a
customer messages the tenant, a central webhook routes the message to a Celery
task that reads that tenant's live inventory/prices and replies with an LLM.

Because it's BYOK, every WhatsApp conversation is billed by Meta directly to the
tenant's own card — **Mouss Tec takes no cut of message costs**.

## Why the app lives in `SHARED_APPS` (public schema)

Meta delivers every webhook to a single public URL, *before* any tenant
subdomain/schema is known. The routing table that maps an inbound
`phone_number_id` (WhatsApp) or `page_id` (Messenger) back to the owning tenant
must therefore be queryable without a schema context. So:

- `TenantChannelConfig` + `ChannelMessageLog` live in the **public** schema
  (FK to `clients.Client`, indexed on `whatsapp_phone_number_id` / `facebook_page_id`).
- The tenant's **inventory & prices** stay in the **tenant** schema and are read
  inside the Celery task via `django_tenants.utils.schema_context(schema_name)`.

## Request flow

```
Customer → WhatsApp/Messenger
        → Meta Cloud API
        → POST /api/webhooks/omnichannel/         (OmnichannelWebhookView)
             • parse envelope (routing.extract_inbound_messages)
             • resolve tenant by phone_number_id / page_id (routing.resolve_config)
             • verify X-Hub-Signature-256 with the tenant's app secret
             • ack Meta 200 in <1s, hand off to Celery
        → omnichannel.process_inbound_message  (Celery, queue: heavy_ai_tasks)
             • gate on subscription + ai_enabled
             • schema_context(tenant): build priced catalogue snapshot
             • LLM reply (tenant BYO OpenAI/Gemini key, else platform Gemini)
             • send reply via Meta Graph API using the tenant's token
             • log to ChannelMessageLog
```

## The three deliverables

| Deliverable | Location |
|---|---|
| **1. Models & architecture** | `omnichannel/models.py`, `omnichannel/crypto.py`, `omnichannel/migrations/0001_initial.py` |
| **2. Webhook + Celery logic** | `omnichannel/views.py`, `omnichannel/tasks.py`, `omnichannel/services/{routing,meta_api,inventory_context,llm}.py` |
| **3. Arabic onboarding guide** | `omnichannel/templates/omnichannel/onboarding_guide.html` (rendered at `/omnichannel/guide/`) |

## Security

- **Secrets at rest**: Meta access token, app secret, and BYO LLM key are stored
  as Fernet ciphertext (`crypto.py`) and never rendered back or logged in clear.
  Key precedence: `OMNICHANNEL_SECRET_KEK` → `OBD_DEVICE_SECRET_KEK` →
  a SECRET_KEY-derived key (dev fallback, logged as a warning).
- **Webhook authenticity**: every POST is HMAC-SHA256 verified against the
  tenant's app secret (`X-Hub-Signature-256`). If a tenant hasn't set an app
  secret yet, the message is allowed but a warning is logged.
- **Fast ack**: the view always returns 200 within milliseconds, so we never hit
  Meta's ~20s webhook timeout regardless of LLM latency.

## Configuration (env)

```
OMNICHANNEL_VERIFY_TOKEN=   # platform webhook verify token handed to tenants
META_GRAPH_VERSION=v19.0
OMNICHANNEL_SECRET_KEK=     # Fernet.generate_key() — REQUIRED in production
OMNICHANNEL_GEMINI_MODEL=   # optional platform fallback model
```

## Separate subscription (250 EGP/month)

This is an independently-billed add-on, gated by the subscription lifecycle on
`TenantChannelConfig` (`is_subscription_active` + `subscription_expires_at`,
mirroring the OBD add-on). `is_operational` — and therefore any auto-reply — is
gated on `subscription_is_valid` (active AND not expired).

Two activation paths (both supported):

1. **Self-serve by card (Paymob)** — the tenant admin clicks *ادفع بالبطاقة*
   on `/omnichannel/` (`pay_with_card` view → Paymob iframe). After payment,
   Paymob calls `paymob_callback` (HMAC-verified, idempotent) which activates
   30 days. Metadata is resolved from the cache keyed by the Paymob order id
   (`paymob_omni_{order_id}`), mirroring the parts-marketplace flow.

   > ⚠️ **Operator step:** set the Paymob dashboard *Transaction Processed
   > Callback* to `https://<your-domain>/omnichannel/paymob-callback/` (or route
   > your central Paymob callback to it) so card payments auto-activate.

2. **Self-serve from wallet** — clicking *الخصم من رصيد المحفظة* debits one
   month atomically from the tenant's platform wallet (`EscrowLedger` +
   `wallet_balance` F() debit) and grants 30 days. Insufficient balance → the
   tenant tops up the wallet (existing Paymob top-up), then subscribes.
3. **Manual grant** — the super-admin grants/extends/revokes per tenant from:
   - the dedicated dashboard `/superadmin/omnichannel/`
     (`omnichannel/saas_admin_views.py`, mirrors OBD grant/revoke), or
   - Django admin actions on `TenantChannelConfig`
     (activate month / extend month / lifetime / revoke).

## Tenant-facing pages

| URL | Purpose |
|---|---|
| `/omnichannel-ai/` | **PUBLIC** marketing page (no login) — shown on the main site; CTAs to login/signup. Pricing & landing cards link here |
| `/omnichannel/` | Dedicated feature page **inside a tenant account** (login required): live subscription status, subscribe/renew |
| `/omnichannel/console/` | **Dedicated console (subscribers only):** overview KPIs, inbox, per-contact threads, contacts — a feature-focused dashboard with no unrelated ERP modules. Gated on a valid subscription |
| `/omnichannel/console/inbox/` | Conversations inbox (grouped by contact) |
| `/omnichannel/console/contacts/` | Contacts who messaged |
| `/omnichannel/settings/` | Connect Meta credentials, tune AI, view recent conversations |
| `/omnichannel/guide/` | Step-by-step Arabic setup tutorial **+ daily-usage guide** |
| `/omnichannel/pay/` | POST-only card checkout (redirects to Paymob) |
| `/omnichannel/paymob-callback/` | Paymob server-to-server activation (HMAC) |
| `/omnichannel/subscribe/` | POST-only self-serve purchase (wallet debit) |

The add-on is also surfaced on the public **pricing page** (`clients/pricing.html`)
and the **landing page** (`clients/landing.html`) as a 250 EGP/month add-on card
linking to `/omnichannel/`.

A sidebar link (*الرد الآلي*) is added to the tenant portal
(`inventory/_base_portal.html`), and a super-admin nav card
(*أتمتة القنوات*) to `clients/super_admin.html`.

## Deploy steps

```
python manage.py migrate_schemas --shared   # creates the public-schema tables
# restart web + celery workers (queue: heavy_ai_tasks)
```

## Tests

```
python manage.py test omnichannel
```

`test_routing.py` and `test_signature_and_prompt.py` are `SimpleTestCase`s (no DB)
covering payload parsing, echo/status filtering, HMAC verification, and the
crypto round-trip.

## Multi-number (additional numbers/pages)

The base subscription includes 1 number. Tenants buy extra capacity as a package:
+2 numbers (450 EGP / 45 AED), +4 numbers (850 EGP / 85 AED) — region-priced,
charged from the wallet. Managed at `/omnichannel/console/numbers/`.

- `TenantChannelConfig.extra_numbers` holds the purchased extra count; capacity =
  1 + extra_numbers (`number_capacity`).
- `TenantChannelNumber` (migration 0006) stores each additional number/page with
  its own encrypted Meta token + app secret.
- Routing: `services/routing.resolve_target()` resolves an inbound message to the
  matching number (primary or additional) and returns the exact token to reply
  with; the webhook passes it to the Celery task (`access_token`,
  `phone_number_id`). Same webhook URL for all numbers.
