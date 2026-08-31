"""
🧪 check_templates — يصنّف (compile) كل قوالب المشروع للكشف المبكر عن أخطاء
الصياغة قبل ما توصل للمستخدمين كصفحة 500.

الدافع: عطل إنتاجي حصل لأن قالب login_finder.html استخدم {% trans %} بدون
{% load i18n %}، فكان بيرمي TemplateSyntaxError عند التصيير → كل زيارة لـ
/login/ بتطلّع 500. الخطأ ده مكانش بيظهر إلا وقت تصيير القالب فعلياً.

الأمر ده بيمشي على كل مجلدات القوالب (DIRS + قوالب التطبيقات)، يعمل
get_template لكل ملف .html، ويجمّع أي فشل.

الاستخدام:
    python manage.py check_templates            # يفشل فقط لو قالب عام حرج مكسور
    python manage.py check_templates --all       # يفشل لو أي قالب مكسور
    python manage.py check_templates --list       # يطبع كل القوالب اللي اتفحصت

بيرجع exit code = 1 لو فيه فشل ضمن النطاق المطلوب (مفيد كبوابة في الـ CI/deploy).
"""
import os

from django.core.management.base import BaseCommand
from django.template import engines
from django.template.loader import get_template


# قوالب عامة بيشوفها الزائر قبل الدخول — لو أي واحد فيهم مكسور، ده عطل حرج
# لازم يوقف الـ deploy (مش مجرد تحذير).
CRITICAL_PUBLIC_TEMPLATES = {
    'clients/login_finder.html',
    'clients/signup_register.html',
    'clients/auto_landing.html',
    'clients/print_landing.html',
}


class Command(BaseCommand):
    help = "يصنّف كل قوالب Django للكشف المبكر عن أخطاء الصياغة (TemplateSyntaxError)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--all', action='store_true',
            help='يفشل (exit 1) لو أي قالب مكسور، مش بس القوالب العامة الحرجة.',
        )
        parser.add_argument(
            '--list', action='store_true',
            help='يطبع أسماء كل القوالب اللي اتفحصت.',
        )

    def _iter_template_dirs(self):
        """يجمّع كل مجلدات القوالب: DIRS الصريحة + مجلد templates لكل تطبيق."""
        dirs = []
        for engine in engines.all():
            # DIRS الصريحة
            for d in getattr(engine, 'template_dirs', []) or []:
                dirs.append(str(d))
        # مجلدات templates داخل كل تطبيق (APP_DIRS=True)
        try:
            from django.template.utils import get_app_template_dirs
            for d in get_app_template_dirs('templates'):
                dirs.append(str(d))
        except Exception:
            pass
        # إزالة التكرار مع الحفاظ على الترتيب
        seen = set()
        unique = []
        for d in dirs:
            if d not in seen and os.path.isdir(d):
                seen.add(d)
                unique.append(d)
        return unique

    def _collect_template_names(self):
        """يرجّع set بأسماء القوالب النسبية (زي 'clients/login_finder.html')."""
        names = set()
        for base in self._iter_template_dirs():
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    if fn.endswith(('.html', '.txt', '.xml')):
                        rel = os.path.relpath(os.path.join(root, fn), base)
                        names.add(rel.replace(os.sep, '/'))
        return names

    def handle(self, *args, **options):
        names = sorted(self._collect_template_names())
        if options['list']:
            for n in names:
                self.stdout.write(f"  • {n}")

        broken = {}  # name -> error message
        for name in names:
            try:
                get_template(name)
            except Exception as exc:  # TemplateSyntaxError وغيره
                broken[name] = f"{exc.__class__.__name__}: {exc}"

        self.stdout.write(
            self.style.SUCCESS(f"🧪 اتفحص {len(names)} قالب — سليم: {len(names) - len(broken)}، مكسور: {len(broken)}")
        )

        if not broken:
            self.stdout.write(self.style.SUCCESS("✅ كل القوالب بتتصنّف تمام."))
            return

        # اطبع كل المكسور
        for name, err in sorted(broken.items()):
            self.stderr.write(self.style.ERROR(f"❌ {name}\n     {err}"))

        critical_broken = sorted(set(broken) & CRITICAL_PUBLIC_TEMPLATES)
        if critical_broken:
            self.stderr.write(self.style.ERROR(
                "\n🚨 قوالب عامة حرجة مكسورة (هتطلّع 500 للزوار): " + ", ".join(critical_broken)
            ))

        # قرار الفشل: --all → أي كسر يفشل؛ الافتراضي → القوالب الحرجة بس
        should_fail = bool(broken) if options['all'] else bool(critical_broken)
        if should_fail:
            # SystemExit(1) عشان يبقى بوابة صالحة للـ CI/deploy
            raise SystemExit(1)
