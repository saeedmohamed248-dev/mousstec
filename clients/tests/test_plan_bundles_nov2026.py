"""
💎 الباقات الجديدة (نوفمبر ٢٠٢٦): السوق للكل، والرد الآلي والتسويق والكشف جوه الباقات بأعداد شهرية.

  ردود واتساب: لايت 100 · سيلفر 1,500 · جولد 4,000 (+ ماسنجر/إنستجرام/كومنتات) · إمباير 10,000
  بوستات: 4 · 20 · 60 · من غير حد   —   إعلانات ممولة من جولد   —   كشف أعطال: 20 · 60 · 200 · 600 عربية
"""
import uuid
from datetime import date, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from clients.models import Client, Plan, TenantSubscription
from omnichannel.models import ChannelMessageLog, TenantChannelConfig
from social_ads.models import SocialAdsConfig, SocialPost


def _tenant():
    schema = f"tst_{uuid.uuid4().hex[:8]}"
    with mock.patch.object(Client, "auto_create_schema", False):
        Client.objects.bulk_create([Client(schema_name=schema, name="شركة", owner_name="م", phone="01000000000")])
    return Client.objects.get(schema_name=schema)


def _subscribe(tenant, slug):
    plan = Plan.objects.get(slug=slug)
    # حدود الباقة على الشركة قبل الاشتراك ⇒ sync_limits_to_tenant مايعملش Client.save (اللي بيبني
    # سكيمة كاملة للشركة دي في الاختبار وبياخد دقيقة ونص)
    Client.objects.filter(pk=tenant.pk).update(max_branches=plan.max_branches, max_users=plan.max_users,
                                               max_treasuries=plan.max_treasuries)
    tenant = Client.objects.get(pk=tenant.pk)
    TenantSubscription.objects.update_or_create(tenant=tenant, defaults=dict(
        plan=plan, is_active=True, current_period_start=date.today(),
        current_period_end=date.today() + timedelta(days=30),
        locked_entitlements=dict(plan.entitlements or {}), locked_monthly_price=plan.monthly_price,
        locked_at=timezone.now()))
    return Client.objects.get(pk=tenant.pk)


class PlanMatrixTests(TestCase):
    def ent(self, slug, code):
        return (Plan.objects.get(slug=slug).entitlements or {}).get(code) or {}

    def test_marketplace_is_open_on_every_plan(self):
        for plan in Plan.objects.filter(slug__in=["auto-lite", "auto-silver", "print-lite", "print-starter"]):
            self.assertTrue((plan.entitlements.get("b2b_marketplace") or {}).get("enabled"), plan.slug)

    def test_tier_limits(self):
        self.assertEqual(self.ent("auto-lite", "ai_auto_reply")["monthly_limit"], 100)
        self.assertEqual(self.ent("print-starter", "ai_auto_reply")["monthly_limit"], 1500)
        self.assertEqual(self.ent("auto-gold", "ai_auto_reply")["monthly_limit"], 4000)
        self.assertFalse(self.ent("auto-silver", "ai_auto_reply_social").get("enabled"))
        self.assertTrue(self.ent("print-pro", "ai_auto_reply_social").get("enabled"))
        self.assertEqual(self.ent("auto-lite", "social_studio")["monthly_limit"], 4)
        self.assertIsNone(self.ent("auto-empire", "social_studio").get("monthly_limit"))
        self.assertTrue(self.ent("auto-gold", "ads_autopilot").get("enabled"))
        self.assertEqual(self.ent("auto-lite", "ai_copilot")["monthly_limit"], 10)
        self.assertEqual(Plan.objects.get(slug="auto-silver").monthly_diagnostics_scans_quota, 60)
        self.assertEqual(Plan.objects.get(slug="auto-lite").monthly_diagnostics_scans_quota, 20)
        self.assertEqual(Plan.objects.get(slug="print-lite").monthly_ai_designs_quota, 10)
        self.assertIn("السوق (B2B وقطع الغيار) مجاناً", Plan.objects.get(slug="auto-lite").features)

    def test_fault_diagnosis_on_every_auto_plan(self):
        """كشف الأعطال في كل باقات السيارات: تحليل كود العطل + بوت التشخيص بعدد صغير في لايت."""
        for slug in ("auto-lite", "auto-silver", "auto-gold", "auto-empire"):
            self.assertTrue(self.ent(slug, "workshop_dtc_ai").get("enabled"), slug)
        self.assertFalse(self.ent("print-lite", "workshop_dtc_ai").get("enabled"))
        self.assertEqual(Plan.objects.get(slug="auto-lite").monthly_diagnostics_bot_quota, 5)

    def test_compare_table_shows_cars_and_unlimited(self):
        from clients.services.plan_compare import compare_table
        plans = list(Plan.objects.filter(slug__in=["auto-lite", "auto-silver", "auto-gold", "auto-empire"])
                     .order_by("monthly_price"))
        rows = {r["label"]: r["cells"] for r in compare_table(plans, {"social_studio": "studio"})["rows"]}
        self.assertEqual([c.get("n") for c in rows["عربيات كشف الأعطال في الشهر"]], [20, 60, 200, 600])
        # بوستات إمباير من غير حد ⇒ «غير محدود» مش ✓ (السطر فيه أرقام)
        self.assertEqual(rows["studio"][-1], {"unlimited": True})


class AutoReplyPlanTests(TestCase):
    def setUp(self):
        self.tenant = _subscribe(_tenant(), "auto-lite")
        self.config = TenantChannelConfig.objects.create(tenant=self.tenant, whatsapp_phone_number_id="PN")
        self.config.meta_access_token = "TOKEN"
        self.config.save()

    def test_included_in_lite_without_paying_the_addon(self):
        self.assertFalse(self.config.paid_addon_is_live)
        self.assertTrue(self.config.subscription_is_valid)
        self.assertEqual(self.config.subscription_state, "plan")
        self.assertFalse(self.config.social_channels_allowed)

    def test_quota_exhausted_sends_handoff_message_not_ai(self):
        from omnichannel import tasks
        from omnichannel.services import plan_quota
        ChannelMessageLog.objects.bulk_create([
            ChannelMessageLog(tenant=self.tenant, channel="whatsapp", sender_id=f"20100{i}",
                              inbound_text="?", outbound_text="!", status="replied") for i in range(100)])
        self.assertEqual(plan_quota.status(self.config)["used"], 100)
        self.assertFalse(plan_quota.can_reply(self.config))
        with mock.patch("omnichannel.tasks.generate_reply") as gen, \
                mock.patch("omnichannel.tasks.meta_api.send_whatsapp_text") as send:
            tasks.process_inbound_message.run(config_id=self.config.pk, channel="whatsapp",
                                              sender_id="201999", text="عندك فلتر؟", message_id="m1")
        gen.assert_not_called()
        self.assertEqual(send.call_args.kwargs["text"], self.config.fallback_message)
        log = ChannelMessageLog.objects.filter(sender_id="201999").get()
        self.assertEqual((log.status, log.error), ("skipped", "quota_exceeded"))

    def test_messenger_is_gold_and_up(self):
        from omnichannel import tasks
        with mock.patch("omnichannel.tasks.generate_reply") as gen:
            tasks.process_inbound_message.run(config_id=self.config.pk, channel="messenger",
                                              sender_id="U1", text="hi", message_id="m2")
        gen.assert_not_called()
        self.assertEqual(ChannelMessageLog.objects.get(sender_id="U1").error, "plan_channel")
        gold = TenantChannelConfig.objects.create(tenant=_subscribe(_tenant(), "auto-gold"))
        self.assertTrue(gold.social_channels_allowed)

    def test_paid_addon_has_no_limit(self):
        from omnichannel.services import plan_quota
        self.config.grant_subscription(timedelta(days=30))
        self.assertIsNone(plan_quota.monthly_limit(self.config))
        self.assertTrue(self.config.social_channels_allowed)


class SocialStudioPlanTests(TestCase):
    def test_lite_gets_four_posts_and_no_paid_ads(self):
        from social_ads.services import plan_quota
        cfg = SocialAdsConfig.objects.create(tenant=_subscribe(_tenant(), "auto-lite"))
        self.assertTrue(cfg.subscription_is_valid)
        self.assertFalse(cfg.ads_allowed)
        self.assertEqual(plan_quota.clamp(cfg, 10), 4)
        SocialPost.objects.bulk_create([SocialPost(config=cfg, tenant=cfg.tenant, caption=f"p{i}") for i in range(4)])
        self.assertEqual(plan_quota.clamp(cfg, 10), 0)
        # المستورد من الصفحة مش محسوب
        SocialPost.objects.create(config=cfg, tenant=cfg.tenant, caption="old", source=SocialPost.Source.IMPORTED)
        self.assertEqual(plan_quota.posts_used(cfg), 4)

    def test_gold_runs_ads_and_empire_is_unlimited(self):
        from social_ads.services import plan_quota
        gold = SocialAdsConfig.objects.create(tenant=_subscribe(_tenant(), "auto-gold"))
        self.assertTrue(gold.ads_allowed)
        empire = SocialAdsConfig.objects.create(tenant=_subscribe(_tenant(), "auto-empire"))
        self.assertIsNone(plan_quota.posts_left(empire))


class DiagnosticsPerVehicleTests(TestCase):
    def test_same_car_counts_once_a_month(self):
        from clients.services.diagnostics_quota import consume_vehicle, normalize_vehicle_key
        tenant = _subscribe(_tenant(), "auto-lite")
        key = normalize_vehicle_key(vin="wba-3a5c51-cf256985")
        self.assertEqual(key, "vin:WBA3A5C51CF256985")
        r1 = consume_vehicle(tenant, key)
        r2 = consume_vehicle(tenant, key)
        self.assertEqual(normalize_vehicle_key(plate="س ص ع 123"), "plate:سصع123")
        r3 = consume_vehicle(tenant, normalize_vehicle_key(plate="س ص ع 123"))
        self.assertTrue(r1.allowed and r2.allowed and r3.allowed)
        sub = TenantSubscription.objects.get(tenant=tenant)
        self.assertEqual(sub.diag_scans_used_this_period, 2)
        self.assertEqual(r3.plan_limit, 20)


class DiagnosticsVinFromObdTests(TestCase):
    """الغرفة اتفتحت من غير شاسيه (جلسة اليوم) وبعدين جهاز OBD قرا الشاسيه."""
    VIN_A, VIN_B = "WBA3A5C51CF256985", "KMHCT41BAEU123456"

    def setUp(self):
        from clients.services import diagnostics_quota as q
        self.q = q
        self.tenant = _subscribe(_tenant(), "auto-lite")

    def used(self):
        return TenantSubscription.objects.get(tenant=self.tenant).diag_scans_used_this_period

    def open_room(self, session="s1"):
        return self.q.consume_vehicle(self.tenant, self.q.normalize_vehicle_key(session_key=session))

    def test_first_car_takes_the_session_unit_then_free_all_month(self):
        self.open_room()
        self.assertEqual(self.used(), 1)
        self.assertTrue(self.q.link_vin_to_session(self.tenant, "s1", self.VIN_A).allowed)
        self.assertEqual(self.used(), 1)                       # نفس الوحدة — مفيش خصم تاني
        # يوم/جلسة تانية لنفس العربية: الفتحة بتتخصم وبعد قراءة الشاسيه بترجع
        self.open_room("s2")
        self.assertEqual(self.used(), 2)
        self.q.link_vin_to_session(self.tenant, "s2", self.VIN_A)
        self.assertEqual(self.used(), 1)
        self.q.link_vin_to_session(self.tenant, "s2", self.VIN_A)   # تكرار القراءة مايرجّعش تاني
        self.assertEqual(self.used(), 1)

    def test_moving_the_dongle_to_another_car_counts_a_new_car(self):
        self.open_room()
        self.q.link_vin_to_session(self.tenant, "s1", self.VIN_A)
        self.q.link_vin_to_session(self.tenant, "s1", self.VIN_B)
        self.assertEqual(self.used(), 2)

    def test_quota_exhausted_blocks_new_car(self):
        TenantSubscription.objects.filter(tenant=self.tenant).update(
            diag_scans_used_this_period=20, diag_period_start=self.q._period_start_for(self.q._today()))
        res = self.q.link_vin_to_session(self.tenant, "s9", self.VIN_B)
        self.assertFalse(res.allowed)

    def test_invalid_vin_rejected(self):
        self.assertFalse(self.q.link_vin_to_session(self.tenant, "s1", "123").allowed)


class FreeSetupRequestTests(TestCase):
    """🎁 «نركّبلك الواتساب ببلاش» من جولد/برو — تذكرة واحدة في صندوق الدعم."""

    def _post(self, tenant):
        from django.contrib.auth import get_user_model
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from omnichannel import dashboard_views as v
        req = RequestFactory().post("/omnichannel/free-setup/", {"phone": "01011112222", "note": "بعد العصر"})
        req.user = get_user_model().objects.create_user(f"u_{uuid.uuid4().hex[:6]}", password="x")
        req.session = {}
        req._messages = FallbackStorage(req)
        with mock.patch.object(v, "_current_tenant", return_value=tenant), \
             mock.patch("clients.views.support_views._notify_owner_of_report") as notify:
            resp = v.request_free_setup(req)
        return resp, notify

    def test_gold_gets_one_ticket(self):
        from clients.models import SupportTicket
        tenant = _subscribe(_tenant(), "auto-gold")
        resp, notify = self._post(tenant)
        self.assertEqual(resp.status_code, 302)
        self._post(tenant)                                   # الضغط مرتين ⇒ تذكرة واحدة
        tickets = SupportTicket.objects.filter(tenant=tenant)
        self.assertEqual(tickets.count(), 1)
        self.assertIn("01011112222", tickets.first().message)
        notify.assert_called_once()

    def test_lite_is_not_eligible(self):
        from clients.models import SupportTicket
        tenant = _subscribe(_tenant(), "auto-lite")
        self._post(tenant)
        self.assertFalse(SupportTicket.objects.filter(tenant=tenant).exists())
