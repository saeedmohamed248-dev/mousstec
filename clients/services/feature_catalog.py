"""
📚 كتالوج مميزات Mouss Tec — مصدر واحد لصفحة «المميزات» وصفحات القطاعات
==========================================================================
قبل كده كل صفحة تعريفية كانت مكتوبة بإيدها: صفحة المميزات بتتكلم عن ورش بس، ومفيهاش
المطابع ولا الموارد البشرية ولا السوق ولا الدعم من جوه السيستم، وجدول المقارنة فيها
ثابت وغلط («موظفين غير محدود» في Empire وهي 10). وكان فيها وعود مش متنفذة («دعم 24/7»،
«توفر 99.9%»).

القاعدة هنا: كل سطر ميزة موجودة فعلاً في الكود (اتراجعت قبل ما تتكتب). الميزة اللي ليها
كود في كتالوج الباقات (``code``) بيظهر جنبها «من باقة X» محسوب من الباقات الحالية — فلو المالك
نقل ميزة لباقة تانية من المحرر، الصفحة بتتعدّل لوحدها.

كل نص بالعربي والإنجليزي جنب بعض (زي Feature.name_ar/name_en) عشان مايبقاش فيه سطر
من غير ترجمة يطلع عربي في الموقع الإنجليزي.
"""
from __future__ import annotations

ADDON = ('إضافة باشتراك منفصل', 'Paid add-on')
DEVICE = ('بيحتاج جهاز', 'Needs hardware')

# (id, icon, color, (title ar, en), (intro ar, en), industry or None, [items])
# item = (icon, (title ar, en), (desc ar, en), code or None, badge or None)
SECTIONS = (
    ('sales', 'fa-file-invoice-dollar', 'purple',
     ('المبيعات والفواتير', 'Sales & invoicing'),
     ('بيع بسرعة من الكاشير أو الموبايل — والفاتورة بتخصم المخزون وتسجّل في الخزينة والحسابات لوحدها.',
      'Sell fast from the counter or your phone — every invoice updates stock, cash and the books by itself.'),
     None, (
         ('fa-receipt', ('فواتير بيع وشراء', 'Sales & purchase invoices'),
          ('ترقيم تلقائي وخصومات وضريبة، وطباعة A4 أو على طابعة كاشير حرارية.',
           'Auto numbering, discounts and tax, printed on A4 or a thermal receipt printer.'), None, None),
         ('fa-hand-holding-dollar', ('الآجل والتحصيل', 'Credit sales & collections'),
          ('حساب لكل عميل، تحصيل على دفعات، وكشف حساب جاهز تبعته.',
           'An account per customer, collect in instalments, and a ready statement to send.'), None, None),
         ('fa-person-walking', ('عميل نقدي', 'Walk-in customer'),
          ('بيع سريع من غير ما تسجّل العميل — والاسم بيتحفظ على الفاتورة نفسها.',
           'Quick sale without registering a customer — the name is kept on the invoice itself.'), None, None),
         ('fa-rotate-left', ('المرتجعات', 'Returns'),
          ('مرتجع البيع والشراء بيرجّع المخزون ويعدّل الخزينة والقيود.',
           'Sales and purchase returns put stock back and correct cash and the ledger.'), None, None),
         ('fa-share-nodes', ('إرسال الفاتورة واتساب', 'Send invoices on WhatsApp'),
          ('ابعت الفاتورة للعميل برسالة واحدة من الموبايل.',
           'Send the invoice to your customer in one tap from your phone.'), None, None),
         ('fa-address-book', ('العملاء والموردين', 'Customers & suppliers'),
          ('بيانات وتاريخ تعامل ورصيد لكل عميل ومورد.',
           'Details, history and balance for every customer and supplier.'), None, None),
     )),
    ('inventory', 'fa-boxes-stacked', 'blue',
     ('المخزون والمشتريات', 'Inventory & purchasing'),
     ('اعرف عندك إيه في كل فرع بالظبط، ومين صرف إيه وإمتى.',
      'Know exactly what you have in every branch — and who moved what, and when.'),
     None, (
         ('fa-warehouse', ('مخزون لكل فرع', 'Stock per branch'),
          ('رصيد كل صنف في كل فرع، وتحويلات بين الفروع بإذن.',
           'Every item’s balance in every branch, with approved transfers between branches.'), None, None),
         ('fa-barcode', ('الباركود', 'Barcodes'),
          ('بيع واشتري ودوّر على أي صنف بالمسح.', 'Sell, buy and find any item by scanning it.'), None, None),
         ('fa-bell', ('تنبيه النواقص', 'Low-stock alerts'),
          ('السيستم بيقولك إيه قرب يخلص قبل ما يخلص.', 'The system tells you what is running out before it does.'),
          None, None),
         ('fa-truck-ramp-box', ('المشتريات والموردين', 'Purchasing & suppliers'),
          ('فواتير شراء وشحنات وطلبات أسعار من الموردين.',
           'Purchase invoices, shipments and requests for quotation to suppliers.'), None, None),
         ('fa-file-import', ('إدخال فاتورة الشراء من ملف أو صورة', 'Import purchases from a file or photo'),
          ('ارفع ملف إكسيل أو صوّر فاتورة المورد والأصناف تتسجّل لوحدها.',
           'Upload an Excel file or snap the supplier’s invoice and the items are entered for you.'), None, None),
         ('fa-clock-rotate-left', ('سجل الحركات', 'Movement history'),
          ('مين أضاف ومين صرف كل قطعة وإمتى.', 'Who added and who issued every item, and when.'), None, None),
     )),
    ('finance', 'fa-scale-balanced', 'emerald',
     ('الخزائن والحسابات', 'Cash & accounting'),
     ('حسابات حقيقية بالقيد المزدوج من غير ما تكتب قيد بإيدك.',
      'Real double-entry accounting without writing a single journal entry by hand.'),
     None, (
         ('fa-vault', ('خزائن متعددة', 'Multiple cash boxes'),
          ('خزينة لكل فرع أو كاشير، وتحويلات بينهم، ومصروفات بأنواعها.',
           'A cash box per branch or cashier, transfers between them, and categorised expenses.'), None, None),
         ('fa-book', ('قيود تلقائية', 'Automatic journal entries'),
          ('كل فاتورة ومصروف وتحصيل بيتسجّل في الدفاتر لوحده.',
           'Every invoice, expense and collection is posted to the books automatically.'), None, None),
         ('fa-building-columns', ('البنوك والتسوية البنكية', 'Banks & reconciliation'),
          ('طابق كشف البنك مع حركاتك واعرف الفرق فوراً.',
           'Match the bank statement with your records and spot differences instantly.'), None, None),
         ('fa-sitemap', ('شجرة الحسابات والسنة المالية', 'Chart of accounts & fiscal year'),
          ('شجرة جاهزة لنشاطك تقدر تعدّل عليها، وقفل للسنة المالية.',
           'A ready chart for your trade that you can adjust, plus fiscal-year closing.'), None, None),
         ('fa-chart-pie', ('الميزانية والأرباح والخسائر', 'Balance sheet and P&L'),
          ('صافي ربحك الحقيقي شهرياً وسنوياً.', 'Your real net profit, monthly and yearly.'), None, None),
         ('fa-percent', ('الضرايب', 'Taxes'),
          ('ضريبة القيمة المضافة على الفواتير وتقاريرها.', 'VAT on invoices and VAT reports.'), None, None),
     )),
    ('team', 'fa-users-gear', 'amber',
     ('الفروع والموظفين والأمان', 'Branches, staff & security'),
     ('كل موظف يشوف شغله بس، وانت تشوف كل حاجة من أي مكان.',
      'Every employee sees only their own work — you see everything, from anywhere.'),
     None, (
         ('fa-code-branch', ('فروع متعددة', 'Multiple branches'),
          ('كل فرع بمخزونه وخزينته، وتقارير لكل فرع أو مجمّعة.',
           'Each branch with its own stock and cash, with per-branch or combined reports.'), None, None),
         ('fa-user-shield', ('أدوار وصلاحيات', 'Roles & permissions'),
          ('كاشير، فني، مخزن، محاسب، مدير — ودور «عرض فقط» للشريك أو المراجع.',
           'Cashier, technician, storekeeper, accountant, manager — plus a view-only role for partners or auditors.'),
          None, None),
         ('fa-fingerprint', ('حضور وانصراف بالوش والموقع', 'Face & location attendance'),
          ('الموظف يسجّل من موبايله والسيستم يتأكد إنه هو وإنه في الفرع.',
           'Staff check in from their phone; the system verifies their face and that they are at the branch.'),
          None, None),
         ('fa-calendar-check', ('الورديات والإجازات والسلف', 'Shifts, leave & advances'),
          ('سلف بأقساط بتتخصم من المرتب لوحدها، وطلبات إجازة بموافقة.',
           'Advances repaid in instalments straight from payroll, and leave requests with approval.'), None, None),
         ('fa-money-check-dollar', ('المرتبات والعمولات', 'Payroll & commissions'),
          ('مرتب كل موظف بالحضور والسلف والعمولة على المبيعات أو الشغل.',
           'Each salary computed from attendance, advances and commission on sales or jobs.'), 'hr_payroll', None),
         ('fa-lock', ('تحقق بخطوتين وسجل مراجعة', 'Two-step login & audit trail'),
          ('حماية حساب المالك، وكل تعديل متسجّل باسم اللي عمله.',
           'Owner accounts protected by a second step, and every change logged with who made it.'), None, None),
     )),
    ('ai', 'fa-brain', 'fuchsia',
     ('الذكاء الاصطناعي والتقارير', 'AI & reports'),
     ('اسأل عن شغلك بالعربي واعرف بتكسب منين.', 'Ask about your business in plain language and see where you make money.'),
     None, (
         ('fa-comments', ('المستشار الذكي', 'AI business advisor'),
          ('اسأل «بعت كام النهارده؟» أو «إيه أكتر صنف بيكسب؟» والرد من أرقامك الحقيقية. كل باقة فيها عدد أسئلة '
           'في الشهر (موضّح في جدول المقارنة) وبعدها بتكمّل من رصيد AI.',
           'Ask “How much did I sell today?” or “Which item earns the most?” and get answers from your real numbers. '
           'Each plan includes a monthly number of questions (see the comparison table), then it continues on AI credits.'),
          'ai_copilot', None),
         ('fa-chart-line', ('تقارير متقدمة', 'Advanced reports'),
          ('ربح كل صنف وفرع وموظف، ومقارنة فترات، وتصدير إكسيل وPDF.',
           'Profit per item, branch and employee, period comparisons, and Excel/PDF export.'), 'reports_advanced', None),
         ('fa-camera', ('استوديو صور المنتجات', 'Product photo studio'),
          ('صوّر المنتج بموبايلك والـ AI يطلّعه بخلفية احترافية للبيع أونلاين.',
           'Snap a product with your phone and AI gives it a professional background for online selling.'), None, None),
         ('fa-robot', ('مساعد بيرد على الزوار', 'Website assistant'),
          ('بوت على الموقع بيشرح السيستم ويرد على الأسئلة في أي وقت.',
           'A chatbot on the website that explains the system and answers questions at any time.'), None, None),
     )),
    ('automotive', 'fa-car', 'sky',
     ('مراكز الصيانة ومحلات قطع الغيار', 'Workshops & spare-parts shops'),
     ('كل اللي الورشة محتاجاه: من استلام العربية لحد تذكير العميل بالصيانة الجاية.',
      'Everything a workshop needs — from receiving the car to reminding the customer about the next service.'),
     'automotive', (
         ('fa-screwdriver-wrench', ('كروت الصيانة', 'Repair job cards'),
          ('كرت لكل عربية: الشكوى، الفني، القطع، المصنعية، الصور، والتسليم.',
           'A card per car: complaint, technician, parts, labour, photos and handover.'), 'workshop_repair_cards', None),
         ('fa-car-side', ('العربيات وتاريخ الصيانة', 'Vehicles & service history'),
          ('تاريخ كامل لكل عربية وجواز صحة تبعته للعميل.',
           'A full history per vehicle and a health passport you can share with the customer.'), None, None),
         ('fa-bell', ('تذكير الصيانة الجاية', 'Next-service reminders'),
          ('رسالة للعميل لما ميعاد الزيت أو الصيانة يقرب — يرجعلك هو مش غيرك.',
           'A message to the customer when the next oil change or service is due — so they come back to you.'),
          None, None),
         ('fa-star', ('تقييم العملاء', 'Customer feedback'),
          ('العميل بيقيّم الخدمة بعد التسليم وانت تشوف التقييمات.',
           'Customers rate the service after handover and you see every rating.'), None, None),
         ('fa-stethoscope', ('التشخيص الذكي (OBD)', 'Smart diagnostics (OBD)'),
          ('اقرا أكواد الأعطال وفسّرها بالعربي، مع خطط فحص خطوة بخطوة.',
           'Read fault codes, explained in plain language, with step-by-step test plans.'),
          'diagnostics_guided_tests', None),
         ('fa-microchip', ('تحليل الأعطال بالذكاء الاصطناعي', 'AI fault-code analysis'),
          ('الـ AI بيرجّح سبب العطل والقطع المحتملة وتكلفة الإصلاح.',
           'AI suggests the likely cause, the parts involved and the repair cost.'), 'workshop_dtc_ai', None),
         ('fa-wave-square', ('البيانات الحية', 'Live data'),
          ('قراءات الحساسات لحظة بلحظة وانت شغال على العربية.', 'Sensor readings in real time while you work on the car.'),
          'diagnostics_live_data', None),
         ('fa-magnifying-glass', ('البحث الذكي عن القطع', 'Smart parts finder'),
          ('من العطل للقطعة المطلوبة ومكانها في مخزنك أو في السوق.',
           'From the fault to the right part — in your stock or on the marketplace.'),
          'diagnostics_smart_parts_finder', None),
         ('fa-book-open', ('أطلس الإصلاح', 'Repair atlas'),
          ('إجابات موثّقة لأسئلة الفنيين بالصور والضفاير.', 'Verified answers to technicians’ questions with photos and wiring.'),
          None, None),
         ('fa-recycle', ('تفكيك السكراب', 'Salvage dismantling'),
          ('فك عربية أو قطعة لأجزاء وكل جزء يدخل المخزون بتكلفته.',
           'Break a car or unit into parts and each part enters stock at its cost.'), None, None),
         ('fa-file-contract', ('عقود الأساطيل', 'Fleet contracts'),
          ('عقود صيانة لشركات العربيات والتاكسي بأسعار متفق عليها.',
           'Maintenance contracts for fleets and taxi companies at agreed prices.'), 'workshop_fleet_contracts', None),
         ('fa-tablet-screen-button', ('كشك الاستقبال الذكي', 'Smart reception kiosk'),
          ('شاشة في الاستقبال بتكلم العميل بالصوت وبتسأل عن القطع.',
           'A reception screen that talks to customers by voice and checks parts availability.'), None, DEVICE),
     )),
    ('printing', 'fa-print', 'rose',
     ('المطابع ومكاتب التصميم', 'Print shops & design studios'),
     ('من عرض السعر لحد التسليم والتحصيل — والتصميم بالذكاء الاصطناعي.',
      'From quotation to delivery and payment — with AI design built in.'),
     'printing', (
         ('fa-calculator', ('عروض أسعار بتسعير تلقائي', 'Auto-priced quotations'),
          ('اختار الخامة والمقاس والكمية والسعر يطلع من تكلفة الخامات والماكينة.',
           'Pick material, size and quantity and the price is calculated from material and machine cost.'),
          'print_auto_pricing', None),
         ('fa-print', ('أوامر الطباعة والشغلانات', 'Print orders & jobs'),
          ('تابع كل أمر من التصميم للطباعة للتسليم.', 'Track every order from design to press to delivery.'),
          'print_orders', None),
         ('fa-check-double', ('بروفة بموافقة العميل', 'Customer proof approval'),
          ('ابعت البروفة لينك والعميل يوافق أو يطلب تعديل من موبايله.',
           'Send the proof as a link and the customer approves or asks for changes from their phone.'), None, None),
         ('fa-layer-group', ('الخامات والمخزون', 'Materials & stock'),
          ('الخامات بتتخصم من المخزن مع كل أمر، ومشتريات ومرتجعات وتسويات.',
           'Materials are deducted with every order, plus purchases, returns and adjustments.'), None, None),
         ('fa-money-check', ('الشيكات والأصول والإقرار الضريبي', 'Cheques, assets & VAT return'),
          ('شيكات واردة وصادرة، أصول بإهلاك شهري تلقائي، وإقرار ضريبة جاهز.',
           'Incoming and outgoing cheques, assets with automatic monthly depreciation, and a ready VAT return.'),
          None, None),
         ('fa-pen-ruler', ('سجل أعمال المصممين', 'Designer worklog'),
          ('شغل كل مصمم ووقته ومراجعته — وعمولته في المرتب.',
           'Each designer’s work, time and review — and their commission in payroll.'), 'print_designer_worklog', None),
         ('fa-wand-magic-sparkles', ('تصاميم بالذكاء الاصطناعي', 'AI designs'),
          ('اكتب الفكرة والـ AI يطلّع تصميم جاهز للطباعة — عدد تصاميم شهري حسب الباقة.',
           'Describe the idea and AI produces a print-ready design — a monthly number of designs per plan.'), None, None),
         ('fa-store', ('متجر التصاميم', 'Design store'),
          ('باقات تصاميم جاهزة تشتريها وتطبعها لعملائك.', 'Ready design packs you can buy and print for your customers.'),
          None, None),
     )),
    ('market', 'fa-shop', 'teal',
     ('السوق والتسويق', 'Marketplace & marketing'),
     ('بيع لناس أكتر من غير ما تتعب في الإعلانات.', 'Reach more buyers without the hassle of advertising.'),
     None, (
         ('fa-store', ('السوق المفتوح', 'Open marketplace'),
          ('اعرض قطعك ومنتجاتك للناس كلها واستقبل الطلبات.', 'List your parts and products for everyone and receive orders.'),
          'open_marketplace', None),
         ('fa-handshake', ('سوق B2B والمزادات', 'B2B market & auctions'),
          ('اشتري وبيع بالجملة مع تجار وورش تانية.', 'Buy and sell wholesale with other traders and workshops.'),
          'b2b_marketplace', None),
         ('fa-shield-halved', ('دفع آمن (Escrow)', 'Safe payment (escrow)'),
          ('فلوس المشتري محجوزة عند المنصة لحد ما يستلم — والنزاعات بتتحل من المنصة.',
           'The buyer’s money is held by the platform until delivery — and disputes are resolved by us.'), None, None),
         ('fa-bullhorn', ('Social Studio', 'Social Studio'),
          ('نشر منتجاتك أوتوماتيك على فيسبوك وإنستجرام، وحملات إعلانية بتقارير.',
           'Auto-post your products to Facebook and Instagram, with ad campaigns and reports.'), None, ADDON),
         ('fa-comment-dots', ('رد آلي على واتساب وماسنجر', 'WhatsApp & Messenger auto-reply'),
          ('بوت بيرد على عملائك بأسعارك ومخزونك الحقيقي.', 'A bot that answers your customers with your real prices and stock.'),
          None, ADDON),
         ('fa-cart-shopping', ('ربط متجر Wix', 'Wix store sync'),
          ('طلبات متجرك على Wix بتنزل فواتير في السيستم لوحدها.',
           'Orders from your Wix store arrive in the system as invoices automatically.'), None, None),
     )),
    ('platform', 'fa-headset', 'indigo',
     ('الاشتراك والدعم', 'Subscription & support'),
     ('ابدأ ببلاش، وادفع بالطريقة اللي تناسبك، ولو حصلت مشكلة كلّمنا من جوه السيستم.',
      'Start free, pay the way that suits you, and reach us from inside the system if anything goes wrong.'),
     None, (
         ('fa-gift', ('تجربة مجانية', 'Free trial'),
          ('جرّب السيستم كامل من غير كارت.', 'Try the full system with no card required.'), None, None),
         ('fa-credit-card', ('طرق دفع مصرية', 'Local payment methods'),
          ('كارت، فودافون كاش، إنستاباي — وفاتورة لكل دفعة.',
           'Card, Vodafone Cash or InstaPay — with an invoice for every payment.'), None, None),
         ('fa-arrow-up-right-dots', ('ترقية في أي وقت', 'Upgrade any time'),
          ('بتدفع الفرق بس بحساب الأيام الفاضلة من اشتراكك.', 'You only pay the difference for the days left on your plan.'),
          None, None),
         ('fa-headset', ('زرار «مشكلة؟ كلّمنا»', '“Need help?” button'),
          ('من أي صفحة: اكتب، صوّر الشاشة، سجّل فيديو أو صوتك — وتابع الرد في «تذاكري».',
           'From any page: type, capture the screen, record a video or your voice — and follow the reply in “My tickets”.'),
          None, None),
         ('fa-headset', ('دعم فني أولوية', 'Priority support'),
          ('تذاكرك بتترد الأول.', 'Your tickets are answered first.'), 'priority_support', None),
         ('fa-mobile-screen', ('تطبيق الموبايل', 'Mobile app'),
          ('المخزن والفواتير والحضور وكشف المرتب من الموبايل.', 'Stock, invoices, attendance and payslips from your phone.'),
          None, None),
         ('fa-language', ('عربي وإنجليزي', 'Arabic & English'),
          ('واجهة بالعربي والإنجليزي، وموقع للإمارات بالدرهم.', 'Arabic and English interface, and a UAE site in dirhams.'),
          None, None),
         ('fa-user-plus', ('ادعُ صاحبك', 'Refer a friend'),
          ('صاحبك ياخد خصم على أول اشتراك وانت تاخد شهر مجاني.',
           'Your friend gets a discount on their first plan and you get a free month.'), None, None),
         ('fa-file-export', ('بياناتك ملكك', 'Your data is yours'),
          ('تقدر تصدّر بيانات شركتك في أي وقت.', 'Export your company’s data at any time.'), None, None),
     )),
)


def min_plans(plans):
    """{code: {industry: اسم أقل باقة فيها الميزة}} — من الباقات النشطة بترتيبها."""
    out = {}
    for p in plans:
        ents = p.entitlements if isinstance(p.entitlements, dict) else {}
        for code, cfg in ents.items():
            if isinstance(cfg, dict) and cfg.get('enabled'):
                out.setdefault(code, {}).setdefault(p.industry, p)
    return out


def build_sections(*, is_en, plans=(), only=None):
    """الأقسام جاهزة للقالب باللغة الحالية.

    plans: الباقات النشطة مرتبة (industry, sort_order) — لحساب «من باقة X».
    only: tuple من ids الأقسام (لصفحات القطاعات)."""
    idx = 1 if is_en else 0
    plans = list(plans)
    firsts = {}           # أول باقة في كل قطاع ⇒ الميزة المتاحة فيها = في كل الباقات
    for p in plans:
        firsts.setdefault(p.industry, p)
    lowest = min_plans(plans)

    def plan_name(p):
        # «Gold — جولد» ⇒ «Gold»: الشارة قصيرة ومفهومة في اللغتين
        return ((p.name_en or p.name) if is_en else p.name).split(' — ')[0].strip()

    out = []
    for sid, icon, color, title, intro, industry, items in SECTIONS:
        if only and sid not in only:
            continue
        rows = []
        for i_icon, i_title, i_desc, code, badge in items:
            tier = None
            if code:
                where = lowest.get(code, {})
                if not where:
                    continue                      # الميزة اتشالت من كل الباقات ⇒ مانعلنش عنها
                inds = [industry] if industry else list(where)
                names = [plan_name(where[i]) for i in inds
                         if i in where and where[i] is not firsts.get(i)]
                tier = ' / '.join(dict.fromkeys(names)) or None
            rows.append({'icon': i_icon, 'title': i_title[idx], 'desc': i_desc[idx],
                         'tier': tier, 'badge': badge[idx] if badge else None})
        if rows:
            out.append({'id': sid, 'icon': icon, 'color': color, 'title': title[idx],
                        'intro': intro[idx], 'industry': industry, 'items': rows})
    return out
