# Mousstec ERP — Architecture Guide

> **Living Document** — يتحدّث كل ما يتم تعديل بنية النظام أو تنفيذ refactor.
> آخر تحديث: 2026-06-13

---

## 1. الرؤية المعمارية (Vision)

نقسم النظام إلى **domains مستقلة** بدلاً من apps عملاقة. كل domain يملك:
- نموذجه (`models/`)
- منطقه (`services/`)
- واجهاته (`views/`)
- إشاراته (`signals/`)
- اختباراته (`tests/`)
- إداراته (`admin.py`)

**القاعدة الذهبية:** domain لا يستورد models من domain آخر مباشرة. التواصل بين الـ domains يحدث عبر **service interfaces** فقط.

```
┌──────────────────────────────────────────────────────────┐
│   tenancy  │  marketplace_b2b  │  design_store  │  ...   │
│  (kernel)  │   (domain app)    │  (domain app)  │        │
└──────┬───────────┬───────────────────┬──────────────────┘
       │           │                   │
       ▼           ▼                   ▼
   services    services            services
   (entitle-   (auction,           (ai_pipeline,
    ments,      escrow,             quality,
    billing)    bidding)            persistence)
       │           │                   │
       └───────────┴───────────────────┘
                   │
                   ▼
              core (DB, settings, middleware, AI primitives)
```

---

## 2. الوضع الحالي (As-Is)

### 2.1 الـ apps الموجودة

| App | الحجم | الـ models | الدور |
|-----|------|-----------|------|
| `clients` | **29,553 سطر / 78 ملف** | **103 class** | kitchen sink: SaaS + Marketplace + Design + Billing |
| `inventory` | ~15K سطر | ~30 class | ERP أساسي + خلطة B2B |
| `printing` | ~3K سطر | ~10 class | خدمات الطباعة + designer |
| `smart_diagnostics` | متوسط | ~8 class | تشخيص ذكي للسيارات |
| `hr` | متوسط | ~10 class | شؤون الموظفين |
| `erp_core` | متوسط | — | settings + AI engines + middleware |
| `diagnostics_catalog` | صغير | ~5 class | كتالوج أكواد التشخيص |
| `messenger_bot` | صغير | ~2 class | bot integration |

### 2.2 الـ Mega-Files (تقتل المراجعة)

| File | Lines | Risk |
|------|-------|------|
| `clients/models.py` | **4462** (103 classes) | 🔴 Critical |
| `inventory/views.py` | **4052** | 🔴 Critical |
| `erp_core/ai/design_engine.py` | **2971** | 🟠 High |
| `inventory/admin.py` | **2877** | 🟠 High |
| `clients/views/design_store_views.py` | **2374** | 🟠 High |
| `printing/views.py` | **2103** | 🟡 Medium |
| `inventory/models.py` | **1612** | 🟡 Medium |

### 2.3 المشاكل المعمارية

1. **Domain leakage:** `inventory` يحوي B2B marketplace endpoints رغم أن الـ marketplace كله في `clients`.
2. **God app:** `clients` يخلط 6 domains (Tenancy / Billing / B2B Marketplace / C2C Marketplace / Design Store / Diagnostics).
3. **Circular risk:** أي تغيير في `clients/models.py` يخاطر بكسر كل الـ apps لأن الكل يستورد منه.
4. **Test friction:** الـ tests مكدسة في `clients/tests/` بدون تنظيم حسب الـ domain.

---

## 3. خريطة الـ Domains المستهدفة (To-Be)

```
mousstec/
├── core/                          # kernel (كان erp_core)
│   ├── settings/
│   ├── urls.py
│   ├── middleware/
│   │   ├── tenant_quota.py
│   │   └── auth.py
│   └── ai/                        # AI primitives بس
│       ├── design_engine/         # يقسم لـ package
│       ├── printing_copilot.py
│       └── credit_packages.py
│
├── tenancy/                       # Tenant + Subscription + Billing
│   ├── models/
│   │   ├── tenant.py              # Client
│   │   ├── plan.py                # Plan + Feature + PlanRevision
│   │   ├── subscription.py        # TenantSubscription
│   │   └── addon.py               # AIAddonPackage + DiagnosticsAddon + TopUp
│   ├── services/
│   │   ├── entitlements.py        # ✅ موجود
│   │   ├── billing.py
│   │   └── plan_mapping.py        # ✅ موجود
│   ├── signals/
│   │   ├── quota.py               # ✅ موجود (signals_quota.py)
│   │   └── revisions.py           # auto_create_plan_revision
│   ├── middleware/
│   │   └── tenant_quota.py
│   ├── views/
│   │   ├── admin.py               # saas_admin_views
│   │   └── subscription.py
│   └── tests/
│
├── marketplace_b2b/               # B2B parts marketplace
│   ├── models/
│   │   ├── listing.py             # GlobalB2BMarketplace + PartListing + PartListingPhoto
│   │   ├── bidding.py             # BlindBiddingRequest + BidOffer
│   │   ├── order.py               # PartOrder + PartWantedRequest + PartWantedOffer
│   │   ├── escrow.py              # EscrowLedger + EscrowHold
│   │   └── reference.py           # PartCarMake
│   ├── views/
│   │   ├── search.py
│   │   ├── bidding.py
│   │   ├── b2b.py
│   │   └── wallet.py
│   ├── services/
│   │   ├── auction.py             # bidding lifecycle
│   │   ├── escrow.py              # ✅ موجود
│   │   └── fitment.py             # ✅ موجود
│   ├── signals/
│   │   └── ledger.py
│   ├── admin.py
│   └── tests/
│       ├── test_escrow_signals.py            # ✅
│       └── test_blind_bidding_lifecycle.py   # ✅
│
├── marketplace_c2c/               # Customer marketplace
│   ├── models/
│   │   ├── customer.py            # MarketplaceCustomer + UserVerification
│   │   ├── order.py               # MarketplaceOrder
│   │   ├── service_request.py     # ServiceRequest + TenderOffer
│   │   ├── dispute.py             # DisputeTicket + DisputeEvidence + Liability
│   │   └── notification.py        # CustomerNotification
│   ├── views/
│   ├── services/
│   │   ├── disputes.py            # ✅ موجود
│   │   └── trust.py               # ✅ موجود
│   └── tests/
│
├── design_store/                  # AI design store
│   ├── models/
│   │   ├── design.py              # CustomerDesign + DesignPrintRequest + DesignPromptLog
│   │   ├── package.py             # DesignPackage + DesignPurchase
│   │   ├── conversation.py        # DesignConversation + DesignConversationTurn + DesignChatMessage
│   │   ├── brand.py               # CustomerBrandProfile
│   │   └── learning.py            # AIPromptLearningLog + AIStudioSession + AILimitTracker
│   ├── views/
│   │   ├── store.py               # split من design_store_views (2374 → ~4 files)
│   │   ├── generate.py
│   │   ├── download.py
│   │   └── chat.py
│   ├── services/
│   │   ├── ai_pipeline.py         # ✅ موجود
│   │   ├── persistence.py         # ✅ موجود (design_persistence.py)
│   │   ├── chat.py                # ✅ موجود (design_chat.py)
│   │   └── quality.py
│   └── tests/
│
├── workshop/                      # خدمات الميكانيكية (Automotive)
│   ├── models/
│   │   ├── vehicle.py             # Vehicle
│   │   ├── maintenance.py         # MaintenanceContract
│   │   └── repair_card.py         # (future)
│   ├── views/
│   ├── services/
│   └── tests/
│
├── inventory/                     # مخزون نقي (يتنضف من B2B/AI)
│   ├── models/
│   │   ├── product.py             # Product + ProductCategory
│   │   ├── stock.py               # Inventory + StockTransfer + InventoryMovement + StockAlert
│   │   ├── partner.py             # Customer + Vendor + Branch
│   │   ├── invoice.py             # SaleInvoice + PurchaseInvoice
│   │   ├── accounting.py          # ChartOfAccount + AccountingEntry
│   │   ├── treasury.py            # Treasury
│   │   └── audit.py               # AuditLog + ImportSession
│   ├── views/
│   │   ├── invoices.py            # split من views.py (4052 → ~6 files)
│   │   ├── stock.py
│   │   ├── treasury.py
│   │   ├── reports.py
│   │   └── api.py
│   ├── services/                  # ✅ موجود (audit/inventory/invoice/reporting/treasury)
│   ├── signals/
│   │   └── invoice.py             # totals + commission
│   ├── admin/                     # split من admin.py (2877 → ~5 files)
│   └── tests/
│       └── test_commission_signal.py  # ✅
│
├── billing/                       # المدفوعات والـ invoices (cross-domain)
│   ├── models/
│   │   ├── invoice.py             # PlatformInvoice
│   │   ├── receipt.py             # ManualPaymentReceipt
│   │   └── credit.py              # AIBonusGrant + TenantDesignTopUp
│   ├── services/
│   │   └── paymob.py              # ✅ موجود
│   ├── views/
│   │   └── webhook.py             # webhook_views
│   └── tests/
│
├── support/                       # Tickets + Chat + Errors
│   ├── models/
│   │   ├── ticket.py              # SupportTicket + StaffRole
│   │   ├── chat.py                # ChatSession + ChatMessage
│   │   └── monitoring.py          # SystemErrorLog + VisitorLog + PlatformEvent
│   ├── views/
│   │   └── support.py
│   └── tests/
│
├── printing/                      # ✅ موجود — يستخدم الـ skeleton
├── hr/                            # ✅ موجود
├── smart_diagnostics/             # ✅ موجود — يقسم services أكتر
│
└── frontend/                      # (اختياري) templates مركزية
    └── templates/
        ├── tenancy/
        ├── marketplace_b2b/
        ├── design_store/
        └── shared/
```

---

## 4. ملاك الـ Domains (Owners)

> ✏️ تُملأ من قبل قائد التيم. كل domain يحتاج owner أساسي + backup.

| Domain | Owner | Backup | الحالة |
|--------|-------|--------|--------|
| `core` | TBD | TBD | ⚪ unassigned |
| `tenancy` | TBD | TBD | ⚪ unassigned |
| `marketplace_b2b` | TBD | TBD | ⚪ unassigned |
| `marketplace_c2c` | TBD | TBD | ⚪ unassigned |
| `design_store` | TBD | TBD | ⚪ unassigned |
| `workshop` | TBD | TBD | ⚪ unassigned |
| `inventory` | TBD | TBD | ⚪ unassigned |
| `billing` | TBD | TBD | ⚪ unassigned |
| `support` | TBD | TBD | ⚪ unassigned |
| `printing` | TBD | TBD | ⚪ unassigned |
| `hr` | TBD | TBD | ⚪ unassigned |
| `smart_diagnostics` | TBD | TBD | ⚪ unassigned |

**مسؤوليات الـ Owner:**
- يـ approve PRs اللي تـ touch الـ domain ده
- يحافظ على نظافة الـ domain (لا يسمح بـ cross-domain imports)
- يحدّث الـ tests والـ docs الخاصة بالـ domain
- يقرر الـ migrations الخطرة

---

## 5. التبعيات بين الـ Domains (Dependencies)

```
┌─────────────┐
│    core     │ ← الكل يعتمد عليه (kernel)
└──────┬──────┘
       │
   ┌───┼───────────────────────────┐
   ▼   ▼                           ▼
┌──────────┐                ┌────────────┐
│ tenancy  │ ◄──── (entitlement gates) ──┤  جميع الـ domains
└────┬─────┘                └─────┬──────┘
     │                            │
     │ (subscription check)       │
     ▼                            ▼
┌───────────────┐         ┌───────────────┐
│ marketplace_  │         │ design_store  │
│   b2b / c2c   │         └───────┬───────┘
└───────┬───────┘                 │
        │                         │
        └──────────┬──────────────┘
                   ▼
              ┌─────────┐
              │ billing │ ← يستقبل events من الكل
              └─────────┘
```

### قواعد التبعيات

| القاعدة | السبب |
|---------|------|
| **`core` لا يستورد من أي domain آخر** | يبقى kernel نقي |
| **الـ domains تستورد من `core` و `tenancy` فقط** | tenancy يقدم entitlement gates للكل |
| **الـ tests لـ domain ما تتعامل مع models domain آخر إلا عبر factories** | عزل الـ tests |
| **`billing` يستقبل events ولا يصدر** | billing هو consumer، مش publisher |
| **Cross-domain imports = code smell** | يجب أن يكون عبر services API |

---

## 6. اصطلاحات التسمية (Naming Conventions)

| Layer | Convention | مثال |
|-------|-----------|------|
| **Apps** | snake_case قصير | `marketplace_b2b` ✅ — `B2BMarketplaceApp` ❌ |
| **Models** | `PascalCase`، اسم المجال يـ prefix لو فيه لبس | `BiddingRequest` ✅ — `BlindBiddingRequest` لو واضح من السياق |
| **Views** | `<verb>_<resource>_view` | `submit_bid_offer_view` ✅ — `Bidding` (class) ❌ |
| **Services** | `domain.services.<concern>` | `tenancy.services.entitlements` |
| **Signals** | `<app>/signals/<topic>.py` | `tenancy/signals/quota.py` |
| **Tasks (Celery)** | `<app>.tasks.<verb>_<noun>` | `billing.tasks.dispatch_invoices` |
| **Tests** | `test_<feature>.py` | `test_blind_bidding_lifecycle.py` |
| **Migrations** | `NNNN_<verb>_<what>.py` | `0069_diagnostics_features_in_catalog.py` ✅ |
| **Templates** | `<app>/<feature>.html` | `design_store/store.html` |
| **URLs (namespace)** | `<app>:<view_name>` | `marketplace_b2b:listing_detail` |

### قاعدة الـ Imports داخل الملف

```python
# 1. stdlib
import os
from decimal import Decimal

# 2. third-party
from django.db import models
from rest_framework import serializers

# 3. core / kernel
from core.middleware.auth import require_login

# 4. cross-domain (services only — مش models)
from tenancy.services.entitlements import check_feature

# 5. same-domain
from .models import BiddingRequest
from .services.auction import close_auction
```

---

## 7. اصطلاحات الـ Tests

### 7.1 التنظيم

```
<app>/tests/
├── __init__.py
├── factories.py                # model factories (factory_boy)
├── test_<feature>.py           # وحدة منطقية
├── test_<feature>_signals.py   # signals
└── test_<feature>_lifecycle.py # end-to-end داخل الـ domain
```

### 7.2 القواعد

1. **اسم الـ test method يصف السلوك**: `test_expired_grant_does_not_starve_valid_ones` ✅
2. **AAA pattern**: Arrange / Act / Assert — كل قسم بسطر فاصل
3. **factories مش fixtures**: استخدم `factory_boy` للـ test data
4. **ما تحطش mock للـ DB**: استخدم real DB transactions (سبب: المخاطر اتشافت سابقاً)
5. **كل bug fix = test جديد**: اسم الـ test يـ document الـ bug

### 7.3 Coverage targets

| Layer | Target |
|-------|--------|
| Services | **90%+** (المنطق الحرج) |
| Signals | **100%** (silent breakage = خطر) |
| Views | **70%+** (golden path + edge cases) |
| Models (custom methods) | **80%+** |
| Admin | اختياري |

---

## 8. Migration Playbook

### 8.1 Migrations عادية (آمنة)

```bash
python manage.py makemigrations <app> --name <NNNN_verb_what>
python manage.py migrate
```

### 8.2 نقل model بين apps (Wave 2)

**القاعدة الذهبية:** `db_table` يفضل ثابت. الـ Django state بتتغير، الـ DB ما تتلمسش.

```python
# in old_app/migrations/00XX_remove_model.py
operations = [
    migrations.SeparateDatabaseAndState(
        state_operations=[migrations.DeleteModel(name='MyModel')],
        database_operations=[],  # DB ما تتغيرش
    )
]

# in new_app/migrations/0001_initial.py
operations = [
    migrations.SeparateDatabaseAndState(
        state_operations=[migrations.CreateModel(
            name='MyModel',
            fields=[...],
            options={'db_table': 'old_app_mymodel'},  # ← الـ table القديمة
        )],
        database_operations=[],
    )
]
```

### 8.3 تقسيم ملف داخل نفس الـ app (Wave 1) — لا migration

تقسيم `clients/models.py` إلى `clients/models/__init__.py` + sub-modules:
- **لا migration مطلوبة** لأن الـ Python path للـ class يفضل: `clients.models.Plan`
- شرط: الـ `__init__.py` يـ re-export كل الـ classes

### 8.4 Migrations خطرة (تحتاج owner approval)

- `RemoveField` على jadwal production
- تغيير `unique_together` أو `index_together`
- تغيير `on_delete` (CASCADE → PROTECT أو العكس)
- أي migration بتعمل `RunSQL` خام

---

## 9. خارطة الـ Refactor (Waves)

### Wave 0 — Documentation ✅
- ✅ `docs/ARCHITECTURE.md` (هذا الملف)
- ⏳ `docs/RFC-001-domain-split.md` (لاحقاً، عند الحاجة)

### Wave 1 — Internal File Splits (آمن، صفر behavior change) ✅

| # | الملف | قبل | بعد __init__.py | Submodules | الحالة |
|---|------|----|----------------|-----------|--------|
| 1 | `clients/models.py` | 4462 | **37** | 8 (tenancy/marketplace_b2b/marketplace_c2c/design_store/billing/support/diagnostics/monitoring) | ✅ Done |
| 2 | `inventory/views.py` | 4052 | **74** | 10 (utils + dashboards/printing/vehicles/webhooks/stock_ops/ai_agents/business_ops/reports/service) | ✅ Done |
| 3 | `erp_core/ai/design_engine.py` | 2971 | 2971 | rename only | ⚠️ Skeleton — split blocked by @patch contracts (see ADR-003) |
| 4 | `inventory/admin.py` | 2877 | **58** | 10 (mixins/organization/customers/catalog/invoices/finance/dashboard/audit/accounting/b2b) | ✅ Done |
| 5 | `clients/views/design_store_views.py` | 2374 | **61** | 3 (navigation/generate/delivery) | ✅ Done |
| 6 | `printing/views.py` | 2103 | **40** | 7 (utils/ai_design/copilot/catalog/ai_diagnostics/studio/finance) | ✅ Done |
| 7 | `inventory/models.py` | 1612 | **29** | 7 (organization/catalog/customers/finance/invoices/operations/diagnostics) | ✅ Done |

**Cumulative shrinkage:** 20,451 سطر في الـ entry files → **2,270 سطر** (-89%). 45 domain submodule جديد.

**استراتيجية كل split (المُتَّبَعة فعلاً):**
1. ✅ `git mv <file>.py <file>/__init__.py` — package skeleton كـ sub-commit أول
2. ✅ كل submodule = sub-commit مستقل، يـ run tests + commit
3. ✅ الـ __init__.py يـ `from .submodule import *` للـ backward compat
4. ✅ Underscore-prefixed helpers يتـ re-export صراحةً (لأن `import *` بيتخطاهم)
5. ✅ Decorator stacks (`@csrf_exempt @login_required`) تُنقل مع الـ function (الـ extractor walks back through `@/#/blank` lines)

**Lessons learned (في ADR-003):** الـ pattern آمن لـ models (Django ContentType بيـ resolve FKs lazy)، وللـ views/admin (HTTP adapters مستقلة). بس **مش آمن لـ modules فيها functions بتنادي بعضها داخلياً + tests بتـ `@patch('module.X')`** — لأن الـ `@patch` بيـ swap الـ binding في الـ `__init__.py` namespace بس، مش في الـ submodule اللي بيستخدم الـ function. لازم scan الـ tests الأول.

### Wave 2 — Extract New Domain Apps (متوسط الخطورة)

1. `tenancy` ← extract من `clients` (الأسهل، الأقل تبعيات)
2. `marketplace_b2b` ← extract من `clients` + `inventory`
3. `design_store` ← extract من `clients`
4. `billing` ← extract من `clients`
5. `marketplace_c2c` ← extract من `clients`
6. `support` ← extract من `clients`
7. `workshop` ← extract من `inventory`

**كل extraction = PR منفصل + migration دقيقة + tests pass**

### Wave 3 — Service Boundaries (إعادة هيكلة المنطق)

- إزالة كل cross-domain model imports
- استبدالها بـ services interfaces
- إضافة integration tests للـ service contracts

### Wave 4 — Optional: Microservices Readiness

- لو الحجم بقى يستدعي، أي domain ممكن يتحول لـ microservice مستقل
- شرط: لا يوجد cross-domain model imports
- شرط: events متروكة عبر message queue (Celery حالياً)

---

## 10. سجل القرارات المعمارية (ADR Log)

> كل قرار معماري كبير يُسجّل هنا.

### ADR-001 — اختيار "split داخلي قبل extract" (2026-06-13)
**القرار:** نبدأ بـ Wave 1 (تقسيم داخلي للملفات) قبل Wave 2 (استخراج apps جديدة).
**السبب:** Wave 1 صفر behavior change، يعطي التيم وقت للتأقلم على الـ domain map، ويكشف الـ dependencies الحقيقية قبل النقل.
**البديل المرفوض:** Big-bang refactor — مرفوض لأن المخاطر تفوق العائد على المدى القصير.

### ADR-002 — Bilingual Documentation (2026-06-13)
**القرار:** الـ docs بـ عربي للشرح + إنجليزي للـ code/paths.
**السبب:** التيم الحالي عربي بشكل أساسي، الـ paths والـ code أسماء فنية ما تتترجمش.

### ADR-003 — Module splits must respect `@patch` contracts (2026-06-14)
**القرار:** قبل ما نـ split أي module بيـ expose دوال بتنادي بعضها داخلياً، نـ scan الـ tests لـ `@patch('module.X')` patterns الأول.

**السبب:** عملنا split لـ `erp_core/ai/design_engine.py` لـ submodules
(llm_client, prompt_composer, text_utils...). الـ tests كانت بتـ patch
`erp_core.ai.design_engine._call_together_llm`. بس بعد الـ split،
`prompt_composer.py` بيـ import `_call_together_llm` من `.llm_client`
بـ `from .llm_client import _call_together_llm`، فبيكون عنده reference
محلي للـ function. الـ `@patch` على `design_engine._call_together_llm`
بيـ swap الـ name في الـ `__init__.py` namespace بس، مش في
`prompt_composer.py` — فالـ patched mock مش بيـ trigger.

**التأثير:** 12 test error في `test_brand_design_pipeline.py` بعد split.
الـ `manage.py check` نظيف لكن الـ behavior كان مكسور.

**القرار النهائي:** revert الـ split لـ design_engine (5b)، يبقى الـ
package skeleton (5a) لأن مفيش tests بتـ patch الـ rename فحسب. الـ
re-split هيتعمل لاحقاً بعد إما:
  - تعديل الـ tests لـ patch الـ submodule path الجديد
    (`design_engine.prompt_composer._call_together_llm`)
  - أو استخدام `from . import llm_client` + call عبر
    `llm_client._call_together_llm(...)` في submodules — يخلي الـ
    patch path قابل للتوقع

**درس مستفاد:** الـ split الآمن للـ models بـ FKs (clients/models.py,
inventory/models.py) لأن FKs بتـ resolve بـ Django's ContentType
lazy mechanism. لكن split للـ functions اللي بتنادي بعضها مع
test patches = مخاطرة. لازم scan قبل.

---

## 11. الـ Checklist لإضافة Domain جديد

عند إنشاء domain app جديد:

- [ ] `apps.py` فيه `default_auto_field` و `label` صريح
- [ ] `models/__init__.py` يـ re-export كل الـ models
- [ ] `services/__init__.py` يـ export الـ public API
- [ ] `signals/__init__.py` يـ register في `apps.py:ready()`
- [ ] `tests/factories.py` لكل model أساسي
- [ ] `admin.py` لكل model يحتاج admin
- [ ] إضافة الـ app لـ `INSTALLED_APPS` في `core/settings.py`
- [ ] إضافة الـ URLs لـ `core/urls.py` بـ namespace
- [ ] تحديث جدول الـ Owners في هذا الملف
- [ ] تحديث جدول الـ Dependencies إذا تطلب
- [ ] إضافة الـ owner لـ `CODEOWNERS` (لو مستخدم)

---

## 12. مصادر أخرى

- `CLAUDE.md` (root) — تعليمات للـ AI assistants
- `docs/RFC-*.md` — مقترحات تغيير معماري كبيرة (تُضاف حسب الحاجة)
- اختبارات حية للأنماط: `inventory/tests/test_accounting_cycle.py`, `clients/tests/test_blind_bidding_lifecycle.py`

---

**ملاحظة للتيم:** هذا الملف يتطور. لو لاحظت اختلاف بين الواقع والوثيقة، إما عدّل الوثيقة أو افتح issue.
