"""
🧠 حد أسئلة المستشار الذكي الشهري
==================================
المستشار (شات المبيعات/المخزون/الأرباح) بيجيب الأرقام من قاعدة الشركة ببلاش، بس صياغة الرد
بتروح لمزوّد ذكاء اصطناعي بيحاسب بالاستخدام. عشان كده كل باقة فيها عدد أسئلة في الشهر
(ai_copilot.monthly_limit — فاضي أو 0 = غير محدود):

    Silver 50 · Gold 300 · Empire غير محدود  |  Starter 100 · Pro 300 · Enterprise غير محدود

لما الحد يخلص، الأسئلة بتكمّل من «رصيد AI» بتاع الشركة (نفس الرصيد اللي بيتشحن من «إدارة
الاشتراك»): كل رصيد = 10 أسئلة. التجربة والشركات القديمة من غير اشتراك مالهاش حد.
"""
from __future__ import annotations

from django.utils import timezone

QUESTIONS_PER_CREDIT = 10
TOPUP_URL = '/subscription/manage/#ai-credits'


def _month_count(tenant):
    from clients.models import AILimitTracker
    now = timezone.now()
    return AILimitTracker.objects.filter(tenant=tenant, action_type='copilot_question',
                                         used_at__year=now.year, used_at__month=now.month).count()


def copilot_status(tenant):
    """{'limit': int|None, 'used': int} — None = غير محدود."""
    from tenancy.services.entitlements import EntitlementService
    limit = EntitlementService.limit(tenant, 'ai_copilot') if tenant is not None else None
    return {'limit': limit or None, 'used': _month_count(tenant) if tenant is not None else 0}


def take_copilot_question(tenant, source=''):
    """بيحجز سؤال للشركة ⇒ (True, None) أو (False, رد JSON جاهز للواجهة).

    جوه حد الباقة: سؤال عادي. بعد الحد: أول سؤال في كل 10 بيخصم رصيد AI واحد، ولو مفيش رصيد
    السؤال بيترفض برسالة فيها الترقية والشحن."""
    from clients.models import AILimitTracker
    if tenant is None or not getattr(tenant, 'pk', None):
        return True, None
    status = copilot_status(tenant)
    limit, used = status['limit'], status['used']
    meta = {'source': source}
    if limit is not None and used >= limit:
        extra = used - limit
        if extra % QUESTIONS_PER_CREDIT == 0:
            from erp_core.ai.credits import consume_tenant_credit
            res = consume_tenant_credit(tenant, {'kind': 'copilot_questions', 'block': QUESTIONS_PER_CREDIT})
            if not res.get('success'):
                return False, _quota_response(limit)
        meta['paid_from_credit'] = True
    AILimitTracker.objects.create(tenant=tenant, action_type='copilot_question', metadata=meta)
    return True, None


def _quota_response(limit):
    from django.http import JsonResponse
    msg = (f'خلصت أسئلة المستشار الذكي لشهر ده ({limit} سؤال). تقدر ترقّي باقتك، أو تشحن «رصيد AI» '
           f'من «إدارة الاشتراك» — كل رصيد = {QUESTIONS_PER_CREDIT} أسئلة.')
    return JsonResponse({'error': msg, 'message': msg, 'recommendations': msg, 'answer': None,
                         'code': 'copilot_quota', 'upgrade_url': TOPUP_URL}, status=403)
