"""🎓 دروس «تعليم السيستم» لكل قطاع — فيديوهات قصيرة بموسيقى، درس لكل جزء.

  • السيارات (مراكز الصيانة ومحلات قطع الغيار): /automotive/learn/ — static/videos/auto/
  • المطابع واستوديوهات التصميم:              /printing/learn/   — static/videos/print/

كل درس = MP4 + صورة غلاف JPG بنفس الاسم. الصفحة عامة (من غير دخول) وبتتفتح من صفحة
القطاع ومن جوه السيستم. لو هتضيف درس: حط الفيديو والغلاف في الفولدر وزوّد سطر هنا.
"""
from django.utils.translation import gettext_lazy as _

# (slug, اسم الملف من غير امتداد, العنوان, الوصف, المدة, لمين, كلاس أيقونة Font Awesome كامل, رابط الشاشة جوه السيستم)
_AUTO_LESSONS = [
    ('start', '01-start', _('البداية ولوحة التحكم'),
     _('تسجيل الدخول، قراءة أرقام اليوم من لوحة التحكم، وإعدادات مساحة العمل.'),
     '0:28', _('للكل'), 'fas fa-gauge-high', '/system/dashboard/'),
    ('inventory', '02-inventory', _('المخزن والقطع'),
     _('إضافة قطعة خطوة بخطوة، الإضافة بالجملة، الاستيراد من Excel والتحميل بالتصوير، التحويل بين الفروع، وإعادة الطلب.'),
     '0:56', _('للمراكز ومحلات القطع'), 'fas fa-boxes-stacked', '/system/quick-product/'),
    ('sales', '03-sales', _('البيع ونقطة البيع'),
     _('فاتورة بيع في ثواني: البحث بالباركود، الدفع كاش أو آجل، الإيصال، والعملاء والآجل.'),
     '0:38', _('لمحلات قطع الغيار'), 'fas fa-cash-register', '/system/lightning-pos/'),
    ('workshop', '04-workshop', _('الورشة وكروت الصيانة'),
     _('فتح أمر شغل بالشاسيه واللوحة والعداد، القطع والمصنعية والفحص، شاشة الفني، وجواز السيارة الرقمي.'),
     '0:51', _('لمراكز الصيانة'), 'fas fa-screwdriver-wrench', '/system/job-card/'),
    ('diagnostics', '05-diagnostics', _('كشف الأعطال'),
     _('اكتب كود العطل أو الشكوى واعرف المعنى وخطوات الفحص، غرفة التشخيص بجهاز OBD، وأطلس الإصلاح.'),
     '0:42', _('لمراكز الصيانة'), 'fas fa-microchip', '/diagnostic/shop/'),
    ('whatsapp', '06-whatsapp', _('واتساب والرد الآلي'),
     _('البوت بيرد على عملاءك ٢٤ ساعة من مخزونك وأسعارك على واتساب وماسنجر وإنستجرام.'),
     '0:46', _('للمراكز ومحلات القطع'), 'fab fa-whatsapp', '/omnichannel/'),
    ('marketing', '07-marketing', _('الإعلانات والتسويق'),
     _('استوديو التسويق: بوستات بالذكاء الاصطناعي، تقويم المحتوى، والحملات الممولة على فيسبوك وإنستجرام.'),
     '0:29', _('للمراكز ومحلات القطع'), 'fas fa-bullhorn', '/social-studio/'),
    ('purchases', '08-purchases', _('المشتريات والموردين'),
     _('فاتورة شراء، رفع فاتورة المورد، الشحنات والاستيراد، طلبات التسعير، وسوق التجار B2B.'),
     '0:32', _('للمراكز ومحلات القطع'), 'fas fa-truck-ramp-box', '/system/purchases/new/'),
    ('treasury', '09-treasury', _('الخزائن والمصاريف'),
     _('الخزائن (كاش وبنك ومحافظ)، تسجيل مصروف، الحركات المالية، والمطابقة البنكية.'),
     '0:29', _('للكل'), 'fas fa-vault', '/system/treasuries/'),
    ('reports', '10-reports', _('التقارير والمحاسبة'),
     _('تقارير المبيعات والقطع، المستشار الذكي، الأرباح والخسائر، ميزان المراجعة، والإقفال.'),
     '0:42', _('للكل'), 'fas fa-chart-line', '/system/reports/'),
    ('staff', '11-staff', _('الموظفين والفروع'),
     _('إضافة موظف بصلاحياته، الحضور والانصراف والمرتبات، العمولات، ومتابعة الفروع.'),
     '0:27', _('للكل'), 'fas fa-users-gear', '/system/staff/'),
    ('plans', '13-plans', _('معرفة الباقات والاشتراكات'),
     _('كل باقة بكام وهتستفيد منها إيه: سيلفر وجولد وإمباير، والاشتراكات الإضافية زي الرد الآلي على واتساب وشحن التشخيص، وخصومات الدفع المقدّم.'),
     '0:52', _('للكل'), 'fas fa-tags', '/subscription/manage/'),
    ('support', '12-support', _('الدعم والاشتراك'),
     _('إزاي تكلّمنا لو عندك مشكلة، وتشوف كل الإضافات المتاحة.'),
     '0:17', _('للكل'), 'fas fa-headset', '/support/tickets/'),
]

_ADMIN = '/secure-portal/printing'
_PRINT_LESSONS = [
    ('start', '01-start', _('البداية ولوحة التحكم'),
     _('تسجيل الدخول، أرقام المطبعة في صفحة واحدة، الأزرار السريعة، وإعدادات مساحة العمل.'),
     '0:29', _('للكل'), 'fas fa-gauge-high', '/secure-portal/'),
    ('quotes', '02-quotes', _('العملاء وعروض الأسعار'),
     _('إضافة عميل، وعرض سعر يتبعت للعميل يوافق عليه أونلاين ويتحوّل لطلب بضغطة.'),
     '0:37', _('للكل'), 'fas fa-file-signature', f'{_ADMIN}/pricequotation/add/'),
    ('orders', '03-orders', _('طلبات الطباعة والفواتير'),
     _('طلب جديد بمهام الطباعة والمقاسات والكميات والعربون، ومتابعة الحالة لحد التسليم، والفاتورة.'),
     '0:33', _('للمطابع'), 'fas fa-file-invoice', f'{_ADMIN}/printorder/add/'),
    ('production', '04-production', _('الإنتاج والماكينات والبروفات'),
     _('مهام الطباعة على الماكينات وتكلفتها الفعلية، والبروفات اللي العميل بيوافق عليها قبل الطباعة.'),
     '0:24', _('للمطابع'), 'fas fa-print', f'{_ADMIN}/printjob/'),
    ('design', '05-design', _('التصميم وAI Studio'),
     _('سجل أعمال المصممين، التصميم بالذكاء الاصطناعي، ومتجر التصاميم للعملاء.'),
     '0:25', _('لاستوديوهات التصميم'), 'fas fa-wand-magic-sparkles', f'{_ADMIN}/designerworklog/'),
    ('materials', '06-materials', _('الخامات والمخزن'),
     _('ورق وحبر وفينيل بالكمية والتكلفة، كارت الصنف، والجرد والتالف.'),
     '0:22', _('للمطابع'), 'fas fa-layer-group', f'{_ADMIN}/printmaterial/'),
    ('purchases', '07-purchases', _('المشتريات والموردين'),
     _('فاتورة شراء خامات كاش أو آجل، استلامها في المخزن، وكشف حساب المورد.'),
     '0:22', _('للكل'), 'fas fa-truck-ramp-box', f'{_ADMIN}/materialpurchase/add/'),
    ('treasury', '08-treasury', _('الخزائن والتحصيل والمصروفات'),
     _('تحصيل من عميل على طلب، تسجيل مصروف، الحركات المالية، والخزائن والبنوك.'),
     '0:33', _('للكل'), 'fas fa-vault', f'{_ADMIN}/printtransaction/add/?mode=collect'),
    ('reports', '09-reports', _('التقارير والمحاسبة'),
     _('قائمة الدخل، ميزان المراجعة والدفاتر، أعمار الديون، وضريبة القيمة المضافة والإقفال.'),
     '0:29', _('للكل'), 'fas fa-chart-line', '/printing/reports/profit-loss/'),
    ('whatsapp', '10-whatsapp', _('واتساب والرد الآلي'),
     _('البوت بيرد على عملاء المطبعة ٢٤ ساعة بالأسعار والمواعيد على واتساب وماسنجر وإنستجرام.'),
     '0:31', _('للكل'), 'fab fa-whatsapp', '/omnichannel/'),
    ('marketing', '11-marketing', _('الإعلانات والتسويق'),
     _('استوديو التسويق: بوستات بالذكاء الاصطناعي، تقويم المحتوى، والحملات الممولة على فيسبوك وإنستجرام.'),
     '0:24', _('للكل'), 'fas fa-bullhorn', '/social-studio/'),
    ('staff', '12-staff', _('الموظفين والدعم'),
     _('إضافة موظف بصلاحياته، الحضور والمرتبات، والدعم الفني.'),
     '0:21', _('للكل'), 'fas fa-users-gear', '/printing/staff/'),
]

# إعدادات كل قطاع للصفحة: الفولدر، الشعار، روابط الرجوع والتسجيل
SECTORS = {
    'automotive': {
        'key': 'automotive', 'folder': 'auto', 'lessons': _AUTO_LESSONS,
        'brand': 'AUTO', 'brand_icon': 'fa-car-side', 'landing_url_name': 'automotive_landing',
        'dashboard_url': '/system/dashboard/', 'audience': _('لمراكز الصيانة ومحلات قطع الغيار'),
        'sector_name': _('قطاع السيارات'), 'sector_icon': 'fa-car',
    },
    'printing': {
        'key': 'printing', 'folder': 'print', 'lessons': _PRINT_LESSONS,
        'brand': 'PRINT', 'brand_icon': 'fa-palette', 'landing_url_name': 'printing_landing',
        'dashboard_url': '/secure-portal/', 'audience': _('للمطابع واستوديوهات التصميم'),
        'sector_name': _('قطاع المطابع'), 'sector_icon': 'fa-print',
    },
}


def lessons_for(sector):
    """قائمة دروس القطاع كـ dicts جاهزة للقالب (العنوان مترجم حسب لغة الطلب)."""
    cfg = SECTORS[sector]
    return [
        {
            'num': i, 'slug': slug, 'title': title, 'desc': desc, 'duration': dur, 'audience': who,
            'icon': icon, 'app_url': app_url,
            'video': f'videos/{cfg["folder"]}/{fname}.mp4', 'poster': f'videos/{cfg["folder"]}/{fname}.jpg',
        }
        for i, (slug, fname, title, desc, dur, who, icon, app_url) in enumerate(cfg['lessons'], start=1)
    ]


def auto_lessons():
    return lessons_for('automotive')


def print_lessons():
    return lessons_for('printing')
