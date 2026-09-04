"""
Tenant dashboard screens for the Omnichannel add-on:
  • settings screen  — connect Meta credentials + tune AI behaviour
  • onboarding guide — the step-by-step Arabic tutorial (Deliverable 3)

Both run in a tenant request context (connection.tenant is the current Client).
The config row lives in the public schema but is reachable from the tenant
schema because django-tenants keeps `public` on the search_path.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_tenants.utils import schema_context

from .forms import TenantChannelConfigForm
from .models import ChannelMessageLog, TenantChannelConfig

logger = logging.getLogger("mouss_tec_core")

# One purchase grants 30 days.
_SUBSCRIPTION_PERIOD = timedelta(days=30)


def _current_tenant():
    tenant = getattr(connection, "tenant", None)
    # public schema has no real tenant Client row we want to edit here
    if tenant is None or getattr(tenant, "schema_name", "public") == "public":
        return None
    return tenant


def _webhook_url(request) -> str:
    path = reverse("omnichannel_webhook")
    return request.build_absolute_uri(path)


@login_required
def settings_screen(request):
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")

    config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)

    if request.method == "POST":
        form = TenantChannelConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ إعدادات الأتمتة بنجاح ✅")
            return redirect("omnichannel_settings")
        messages.error(request, "برجاء مراجعة الحقول المميّزة بالأحمر.")
    else:
        form = TenantChannelConfigForm(instance=config)

    recent_logs = ChannelMessageLog.objects.filter(tenant=tenant)[:20]
    widget_key = config.ensure_web_widget_key()
    widget_src = request.build_absolute_uri(
        reverse("omnichannel_web_widget_js", kwargs={"key": widget_key}))

    context = {
        "form": form,
        "config": config,
        "webhook_url": _webhook_url(request),
        "platform_verify_token": getattr(settings, "OMNICHANNEL_VERIFY_TOKEN", ""),
        "widget_src": widget_src,
        "recent_logs": recent_logs,
        "guide_url": reverse("omnichannel_guide"),
        "overview_url": reverse("omnichannel_overview"),
    }
    return render(request, "omnichannel/settings.html", context)


@login_required
def onboarding_guide(request):
    context = {
        "webhook_url": _webhook_url(request),
        "platform_verify_token": getattr(settings, "OMNICHANNEL_VERIFY_TOKEN", ""),
        "settings_url": reverse("omnichannel_settings"),
        "overview_url": reverse("omnichannel_overview"),
    }
    return render(request, "omnichannel/onboarding_guide.html", context)


def public_page(request):
    """PUBLIC marketing page for the add-on — no login required.

    Served on the public domain so prospective customers can learn about the
    feature and start subscribing. Actual purchase happens inside a tenant
    account, so the CTAs route to login / signup (and to /omnichannel/ for
    already-logged-in tenant admins).
    """
    tenant = _current_tenant()
    country = _region_country(request)
    context = {
        "price": TenantChannelConfig.price_for_country(country),
        "country": country,
        "free_conversations": 1000,
        "login_url": "/login/?next=/omnichannel/",
        "signup_url": reverse("saas_customer_signup"),
        "is_logged_in_tenant": tenant is not None and request.user.is_authenticated,
        "overview_url": reverse("omnichannel_overview"),
    }
    return render(request, "omnichannel/public.html", context)


@login_required
def overview(request):
    """Dedicated landing/overview page for the add-on (feature page + subscribe)."""
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")

    config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)
    context = {
        "config": config,
        "tenant": tenant,
        "price": TenantChannelConfig.price_for_country(getattr(tenant, "country", "EG")),
        "free_conversations": 1000,
        "wallet_balance": getattr(tenant, "wallet_balance", Decimal("0")),
        "currency": _currency(tenant),
        "settings_url": reverse("omnichannel_settings"),
        "guide_url": reverse("omnichannel_guide"),
        "subscribe_url": reverse("omnichannel_subscribe"),
        "pay_url": reverse("omnichannel_pay"),
        "console_url": reverse("omnichannel_console"),
        "can_manage": request.user.is_superuser,
    }
    return render(request, "omnichannel/overview.html", context)


@login_required
@require_POST
def subscribe(request):
    """Self-serve: charge one month (250 EGP) from the tenant wallet and grant 30 days.

    Wallet is topped up via the platform's existing Paymob flow; here we debit it
    atomically. Only the tenant's own admin (superuser) may purchase.
    """
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")
    if not request.user.is_superuser:
        messages.error(request, "فقط المدير المسؤول عن الحساب يمكنه تفعيل الاشتراك.")
        return redirect("omnichannel_overview")

    price = TenantChannelConfig.price_for_country(getattr(tenant, "country", "EG"))
    config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)

    try:
        with transaction.atomic():
            # Import here to avoid any import-time coupling with the clients app.
            from clients.models import Client, EscrowLedger

            locked = Client.objects.select_for_update().get(pk=tenant.pk)
            if locked.wallet_balance < price:
                messages.error(
                    request,
                    f"رصيد محفظتك ({locked.wallet_balance} {_currency(tenant)}) لا يكفي "
                    f"لتفعيل الاشتراك ({price} {_currency(tenant)}). برجاء شحن المحفظة أولاً.",
                )
                return redirect("omnichannel_overview")

            # Debit the wallet + record an auditable ledger entry.
            Client.objects.filter(pk=locked.pk).update(
                wallet_balance=F("wallet_balance") - price
            )
            EscrowLedger.objects.create(
                client=locked,
                transaction_type="fee_deduction",
                amount=price,
                description=f"اشتراك أتمتة القنوات (Omnichannel) — شهر ({price} {_currency(tenant)})",
            )
            config.grant_subscription(_SUBSCRIPTION_PERIOD, by_user=request.user)
    except Exception as exc:
        logger.exception("omnichannel: subscribe failed for %s: %s", tenant.schema_name, exc)
        messages.error(request, "تعذّر تفعيل الاشتراك. برجاء المحاولة مرة أخرى أو التواصل مع الدعم.")
        return redirect("omnichannel_overview")

    messages.success(
        request,
        "تم تفعيل اشتراك الأتمتة لمدة 30 يوماً ✅ اربط حسابك الآن من شاشة الإعدادات.",
    )
    return redirect("omnichannel_settings")


# =====================================================================
# 🌐 Website chat widget — inbound endpoint + embeddable script
# =====================================================================
def _cors(resp):
    resp["Access-Control-Allow-Origin"] = "*"
    resp["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@csrf_exempt
def web_chat(request, key):
    """Public website-widget endpoint: {visitor_id, message} → synchronous AI reply.

    Cross-origin (runs on the tenant's own site), so CORS is open and CSRF exempt.
    Gated on the widget key + subscription; replies synchronously (no webhook).
    """
    if request.method == "OPTIONS":
        return _cors(JsonResponse({}))
    if request.method != "POST":
        return _cors(JsonResponse({"error": "POST only"}, status=405))
    try:
        data = json.loads(request.body or "{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    message = (data.get("message") or "").strip()[:2000]
    visitor = (data.get("visitor_id") or "").strip()[:64] or "web_anon"
    if not message:
        return _cors(JsonResponse({"error": "empty"}, status=400))

    config = (TenantChannelConfig.objects
              .filter(web_widget_key=key, web_widget_enabled=True)
              .select_related("tenant").first())
    if config is None:
        return _cors(JsonResponse({"reply": "خدمة الشات غير متاحة حالياً."}))

    # Light per-visitor throttle to curb abuse of the open endpoint.
    throttle = f"omni_web_{key}_{visitor}"
    n = cache.get(throttle, 0)
    if n and n > 20:
        return _cors(JsonResponse({"reply": "لقد أرسلت رسائل كثيرة، برجاء المحاولة بعد قليل."}))
    cache.set(throttle, n + 1, timeout=60)

    tenant = config.tenant
    if not (config.subscription_is_valid and config.ai_enabled):
        return _cors(JsonResponse({"reply": config.fallback_message}))

    from .services.inventory_context import build_catalog_context
    from .services.llm import generate_reply

    currency = _currency(tenant)
    catalog = ""
    try:
        with schema_context(tenant.schema_name):
            catalog = build_catalog_context(message, currency=currency)
    except Exception as exc:
        logger.warning("omnichannel web_chat: catalog read failed: %s", exc)
    reply = generate_reply(config, message, catalog) or config.fallback_message

    try:
        from .models import ChannelMessageLog
        ChannelMessageLog.objects.create(
            tenant=tenant, channel="website", sender_id=visitor,
            contact_name=(data.get("name") or "").strip()[:120],
            inbound_text=message, outbound_text=reply,
            status=ChannelMessageLog.Status.REPLIED,
        )
    except Exception:
        logger.exception("omnichannel web_chat: log failed")

    return _cors(JsonResponse({"reply": reply}))


def web_widget_js(request, key):
    """Serve the embeddable chat-widget JavaScript for a tenant."""
    endpoint = request.build_absolute_uri(reverse("omnichannel_web_chat", kwargs={"key": key}))
    js = _WIDGET_JS_TEMPLATE.replace("__ENDPOINT__", endpoint)
    resp = HttpResponse(js, content_type="application/javascript; charset=utf-8")
    resp["Cache-Control"] = "public, max-age=300"
    return resp


_WIDGET_JS_TEMPLATE = r"""
(function(){
  var ENDPOINT="__ENDPOINT__";
  try{ if(window.__mtOmniLoaded) return; window.__mtOmniLoaded=true; }catch(e){}
  var vid=""; try{ vid=localStorage.getItem("mt_omni_vid")||""; if(!vid){ vid="v"+Date.now()+Math.random().toString(36).slice(2,8); localStorage.setItem("mt_omni_vid",vid);} }catch(e){ vid="v"+Date.now(); }
  var C="#0f2c4c";
  var st=document.createElement("style");
  st.textContent=".mtw-b{position:fixed;bottom:20px;inset-inline-end:20px;width:58px;height:58px;border-radius:50%;background:"+C+";color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.25);z-index:2147483000;font-size:26px}.mtw-p{position:fixed;bottom:88px;inset-inline-end:20px;width:340px;max-width:92vw;height:460px;max-height:72vh;background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.28);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;font-family:system-ui,'Segoe UI',Tahoma,sans-serif}.mtw-h{background:"+C+";color:#fff;padding:14px 16px;font-weight:700}.mtw-m{flex:1;overflow-y:auto;padding:12px;background:#f5f7fb}.mtw-row{display:flex;margin:6px 0}.mtw-c{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:8px 12px;max-width:78%;font-size:14px;white-space:pre-wrap}.mtw-u{margin-inline-start:auto}.mtw-u .mtw-c{background:"+C+";color:#fff;border:0}.mtw-f{display:flex;gap:6px;padding:10px;border-top:1px solid #eee}.mtw-i{flex:1;border:1px solid #cbd5e1;border-radius:10px;padding:8px 10px;font-size:14px;outline:none}.mtw-s{background:"+C+";color:#fff;border:0;border-radius:10px;padding:0 14px;cursor:pointer}";
  document.head.appendChild(st);
  var b=document.createElement("div"); b.className="mtw-b"; b.innerHTML="&#128172;"; document.body.appendChild(b);
  var p=document.createElement("div"); p.className="mtw-p";
  p.innerHTML='<div class="mtw-h">تحدث معنا</div><div class="mtw-m" id="mtwM"></div><div class="mtw-f"><input class="mtw-i" id="mtwI" placeholder="اكتب رسالتك..."><button class="mtw-s" id="mtwS">إرسال</button></div>';
  document.body.appendChild(p);
  var M=p.querySelector("#mtwM"),I=p.querySelector("#mtwI"),S=p.querySelector("#mtwS");
  function add(t,who){ var r=document.createElement("div"); r.className="mtw-row"+(who=="u"?" mtw-u":""); var c=document.createElement("div"); c.className="mtw-c"; c.textContent=t; r.appendChild(c); M.appendChild(r); M.scrollTop=M.scrollHeight; return c; }
  var greeted=false;
  b.onclick=function(){ var o=p.style.display==="flex"; p.style.display=o?"none":"flex"; if(!o&&!greeted){ greeted=true; add("أهلاً بك! كيف أقدر أساعدك؟","a"); I.focus(); } };
  function send(){ var t=(I.value||"").trim(); if(!t) return; add(t,"u"); I.value=""; var w=add("...","a");
    fetch(ENDPOINT,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({visitor_id:vid,message:t})})
      .then(function(r){return r.json();}).then(function(d){ w.textContent=(d&&d.reply)||"—"; })
      .catch(function(){ w.textContent="تعذّر الاتصال، حاول مرة أخرى."; }); }
  S.onclick=send; I.addEventListener("keydown",function(e){ if(e.key==="Enter") send(); });
})();
"""


def _region_country(request) -> str:
    """Resolve the marketing region (EG/AE) from the request host."""
    try:
        from erp_core.regions import region_from_request
        return region_from_request(request).get("country", "EG")
    except Exception:
        return "EG"


def _currency(tenant) -> str:
    try:
        return tenant.effective_currency
    except Exception:
        return "ج.م"


# =====================================================================
# 💳 Direct card payment via Paymob (in addition to wallet debit)
# =====================================================================
@login_required
@require_POST
def pay_with_card(request):
    """Start a Paymob card checkout for one month of the add-on.

    Mirrors the platform's other Paymob checkouts (diagnostics/parts): build an
    iframe URL with the subscription metadata and redirect. Activation happens
    server-to-server in `paymob_callback` after Paymob confirms payment.
    """
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")
    if not request.user.is_superuser:
        messages.error(request, "فقط المدير المسؤول عن الحساب يمكنه تفعيل الاشتراك.")
        return redirect("omnichannel_overview")

    # Ensure a config row exists so the callback can always find/activate it.
    TenantChannelConfig.objects.get_or_create(tenant=tenant)

    price = TenantChannelConfig.price_for_country(getattr(tenant, "country", "EG"))
    callback_url = request.build_absolute_uri(reverse("omnichannel_paymob_callback"))
    try:
        from clients.services.paymob import create_iframe_url

        iframe_url = create_iframe_url(
            amount_egp=price,
            customer_phone=getattr(tenant, "phone", "") or "",
            customer_name=getattr(tenant, "name", "") or "",
            customer_email=getattr(tenant, "email", "") or "",
            order_ref=f"omni-{tenant.pk}",
            callback_url=callback_url,
            item_name="Mouss Tec Omnichannel AI (1 month)",
            metadata={"client_pk": tenant.pk, "kind": "omnichannel_sub"},
            cache_key_prefix="paymob_omni",
        )
    except Exception as exc:  # RuntimeError with an Arabic message, or network
        logger.error("omnichannel: paymob iframe failed for %s: %s", tenant.schema_name, exc)
        messages.error(request, f"تعذّر فتح بوابة الدفع: {exc}")
        return redirect("omnichannel_overview")

    return redirect(iframe_url)


@csrf_exempt
def paymob_callback(request):
    """Server-to-server payment confirmation from Paymob → activate 30 days.

    HMAC verification is mandatory (fail-closed): without it anyone could grant
    themselves a paid subscription by POSTing a forged success payload.
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    from clients.services.paymob import verify_paymob_hmac

    ok, reason = verify_paymob_hmac(request, body_data=data)
    if not ok:
        logger.warning("omnichannel: paymob callback HMAC failed: %s", reason)
        return JsonResponse({"error": "hmac_failed", "reason": reason}, status=403)

    obj = data.get("obj", {}) or {}
    if not obj.get("success"):
        return JsonResponse({"status": "ignored"})

    # Resolve our metadata. Primary: the cache keyed by the Paymob order id
    # (create_iframe_url stores it under 'paymob_omni_{order_id}'). Fallback:
    # payment_key_claims.extra, if the integration echoes it.
    order_id = str((obj.get("order", {}) or {}).get("id") or "")
    metadata = cache.get(f"paymob_omni_{order_id}") or {}
    if not metadata:
        metadata = (obj.get("payment_key_claims", {}) or {}).get("extra", {}) or {}

    if metadata.get("kind") != "omnichannel_sub":
        # Not ours — another feature's callback. Ack without acting.
        return JsonResponse({"status": "ignored"})
    client_pk = metadata.get("client_pk")
    if not client_pk:
        logger.error("omnichannel: paymob callback missing client_pk — %s", metadata)
        return JsonResponse({"error": "metadata"}, status=400)

    paymob_id = obj.get("id", "")
    # Idempotency — Paymob may retry the callback.
    guard = f"omni_paymob_{paymob_id}"
    if paymob_id and cache.get(guard):
        return JsonResponse({"status": "duplicate"})

    with schema_context("public"):
        config, _created = TenantChannelConfig.objects.get_or_create(tenant_id=client_pk)
        config.grant_subscription(_SUBSCRIPTION_PERIOD)

    if paymob_id:
        cache.set(guard, "processed", timeout=86400)
    logger.info("omnichannel: paymob subscription activated client=%s paymob_id=%s",
                client_pk, paymob_id)
    return JsonResponse({"status": "ok"})
