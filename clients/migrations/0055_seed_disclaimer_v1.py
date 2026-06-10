"""
Seed the v1.0 platform-liability disclaimer. This is the legal text
every buyer must accept at checkout. The platform's contractual exposure
is zero — the marketplace facilitates transactions between buyer and
seller but is not party to the sale of goods.

Text follows the structure used by eBay, Etsy, and Facebook Marketplace
Buyer Protection disclosures, adapted to Egyptian / Arabic e-commerce
norms.
"""
from django.db import migrations


DISCLAIMER_BODY_AR = """
يُمثّل سوق Mousstec منصة وسيطة فقط لتسهيل تعاملات بيع وشراء قطع غيار
السيارات بين المستخدمين (P2P). المنصة:

1. ليست بائعاً أو مشترياً ولا طرفاً في عقد البيع.
2. لا تضمن جودة القطعة أو مطابقتها للمواصفات — هذه مسؤولية البائع وحده.
3. تحتفظ بالمبلغ في حساب ضمان (Escrow) حتى انتهاء فترة الضمان المتفق عليها
   مع البائع، ثم تُحوّله للبائع تلقائياً.
4. تتدخّل لتسوية النزاعات خلال 3 أيام من تاريخ التسليم فقط — بعد هذه
   المدة يُعتبر المشتري قابلاً للقطعة بحالتها.

سياسة شحن المرتجعات:
* إذا أراد المشتري إرجاع القطعة بسبب تغيّر رأيه أو خطأ في المقاس →
  المشتري يتحمّل تكاليف شحن المرتجع.
* إذا كانت القطعة معيبة أو مختلفة عن المعروض أو لم تصل أصلاً →
  البائع يتحمّل تكاليف شحن المرتجع.
* المنصة لا تتحمّل أي تكاليف شحن مرتجع في أي ظرف.

بالإقرار على هذا النص، يوافق المشتري على ألا يطالب المنصة بأي تعويض مادي
أو معنوي خارج آلية الـ Escrow الموضّحة أعلاه.
""".strip()


DISCLAIMER_BODY_EN = """
Mousstec Marketplace operates as a facilitating platform for peer-to-peer
automotive parts trade. The platform:

1. Is not a seller, buyer, or party to the sale contract.
2. Does not warrant the quality or specification of any listed part —
   that is solely the seller's responsibility.
3. Holds the buyer's payment in escrow until the agreed warranty window
   expires, then releases the funds to the seller automatically.
4. Intervenes in disputes only during the 3-day inspection window after
   delivery; after that the buyer is deemed to accept the part as-is.

Return shipping policy:
* Buyer changes mind / wrong size ordered → buyer pays return shipping.
* Part is defective / not as described / never arrived → seller pays.
* The platform never covers return shipping costs.

By accepting this disclaimer at checkout, the buyer agrees not to seek
any compensation from the platform outside the escrow mechanism above.
""".strip()


def seed_disclaimer(apps, schema_editor):
    Disclaimer = apps.get_model('clients', 'PlatformLiabilityDisclaimer')
    if Disclaimer.objects.filter(version='v1.0').exists():
        return
    Disclaimer.objects.create(
        version='v1.0',
        title_ar='إخلاء مسؤولية المنصة — v1.0',
        body_ar=DISCLAIMER_BODY_AR,
        body_en=DISCLAIMER_BODY_EN,
        is_active=True,
    )


def reverse(apps, schema_editor):
    Disclaimer = apps.get_model('clients', 'PlatformLiabilityDisclaimer')
    Disclaimer.objects.filter(version='v1.0').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('clients', '0054_escrowhold_platformliabilitydisclaimer_and_more'),
    ]
    operations = [
        migrations.RunPython(seed_disclaimer, reverse),
    ]
