#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys
import subprocess
import shutil
from pathlib import Path
import socket 

# =====================================================================
# 🛡️ 1. الفحوصات الاستباقية والأتمتة الذكية (Pre-flight Mouss Tec Checks)
# =====================================================================
def auto_heal_env_file(env_path):
    """🛠️ ابتكار: المعالج الذاتي لبيئة العمل والأسرار (Auto-Env Bootstrapper)"""
    example_env = env_path.parent / '.env.example'
    if not env_path.exists() and example_env.exists():
        print("🌱 [MOUSS TEC AUTO-HEAL]: لم يتم العثور على ملف '.env'. جاري استنساخ '.env.example' تلقائياً...")
        shutil.copy(example_env, env_path)
        print("✅ تم إنشاء ملف '.env' السحابي بنجاح. يرجى مراجعة المتغيرات الخاصة بالـ API Keys.\n")

def check_infrastructure_health():
    """📡 رادار نبض البنية التحتية قبل الإقلاع (Infrastructure Telemetry Ping)"""
    # نتحقق فقط عند تشغيل السيرفر الفعلي لتجنب تكرار الطباعة مع الأوامر السريعة
    if 'runserver' in sys.argv or 'up' in sys.argv:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            # التحقق من جاهزية قاعدة البيانات المتوافقة مع الـ Tenants
            if s.connect_ex(('127.0.0.1', 5432)) != 0:
                print("⚠️  [MOUSS TEC INFRASTRUCTURE]: سيرفر قاعدة البيانات (PostgreSQL) لا يستجيب!")
                print("💡 نصيحة: تأكد من تشغيل الخدمة أو حاوية Docker لضمان عزل الـ Schemas.\n")
            
            # التحقق من جاهزية سيرفر الكاش وقنوات الـ WebSockets لايف للمزادات
            if s.connect_ex(('127.0.0.1', 6379)) != 0:
                print("⚠️  [MOUSS TEC INFRASTRUCTURE]: خادم الـ Redis والـ Channels لا يستجيب!")
                print("💡 تنبيه: ستتعطل غرف المزادات الحية (Blind Bidding) والمزامنة الفورية للـ POS.\n")

def pre_flight_checks():
    """نظام فحص سيادي لحماية نواة المنصة والتعامل الذكي مع تضارب البورتات"""
    if sys.version_info < (3, 9):
        sys.exit("🛑 [MOUSS TEC FATAL ERROR]: Mouss Tec Core Engine requires Python 3.9 or higher.")

    if sys.prefix == sys.base_prefix and 'runserver' in sys.argv:
        print("\n⚠️  [MOUSS TEC WARNING]: أنت تقوم بتشغيل الأوامر خارج البيئة الوهمية معزولة العواقب (venv)!")
        print("💡 نصيحة: لتفعيل البيئة الحامية، اكتب: source venv/bin/activate\n")

    if 'runserver' in sys.argv or 'up' in sys.argv:
        port = 8000 
        for arg in sys.argv:
            if arg.isdigit() or ':' in arg:
                try: port = int(arg.split(':')[-1])
                except ValueError: pass
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                print(f"\n🛑 [MOUSS TEC PORT CONFLICT]: البورت المشغل {port} مشغول حالياً ببرنامج آخر أو سيرفر معلق!")
                try:
                    # محاولة اصطياد الـ PIDs المعلقة وتطهيرها تلقائياً بذكاء
                    output = subprocess.check_output(f"lsof -t -i:{port}", shell=True, stderr=subprocess.DEVNULL)
                    pids = output.decode('utf-8').strip().split('\n')
                    if pids:
                        print(f"👻 تم رصد عمليات ميتة (PIDs: {', '.join(pids)}) تمنع المنظومة الحية من الإقلاع.")
                        kill_choice = input("🔪 هل تريد تدميرها تلقائياً بالصلاحيات وتشغيل المحرك؟ (y/N): ").lower()
                        if kill_choice == 'y':
                            for pid in pids:
                                subprocess.run(f"kill -9 {pid}", shell=True)
                            print("✅ تم التطهير الشامل للبورت! جاري إشعال المحرك السحابي...\n")
                            return 
                except Exception:
                    pass 
                
                print(f"💡 نصيحة: قم بإنهاء السيرفر المعلق يدوياً، أو شغل المنصة بمرونة على بورت آخر:")
                print(f"   python manage.py runserver {port + 1}\n")
                sys.exit(1)

# =====================================================================
# 🧠 2. مترجم الأوامر والماكرو للمطورين (Smart CLI Aliases)
# =====================================================================
def apply_cli_aliases():
    """تحويل الاختصارات التشغيلية الذكية إلى أوامر المعمارية المعقدة لـ Django Tenants"""
    if len(sys.argv) > 1:
        alias = sys.argv[1]
        if alias == 'up':
            sys.argv[1] = 'runserver'
        elif alias == 'db:push':
            print("🚀 [MOUSS TEC COPILOT]: تفعيل ماكرو 'db:push' -> توليد ومزامنة الـ Schemas للفروع والمشترك...")
            subprocess.run([sys.executable, "manage.py", "makemigrations"])
            subprocess.run([sys.executable, "manage.py", "migrate_schemas", "--shared"])
            subprocess.run([sys.executable, "manage.py", "migrate_schemas", "--tenant"])
            sys.exit(0)

def main():
    """Run administrative tasks."""
    
    # =====================================================================
    # 🔒 3. حاقن الأسرار المبكر والمعالج الذاتي (Early Env Bootstrapper - FIXED ORDER)
    # =====================================================================
    # المزامنة والتقديم اللوجيستي: يجب تخليق وقراءة ملف الأسرار قبل معالجة أي أوامر فرعية
    env_path = Path(__file__).resolve().parent / '.env'
    auto_heal_env_file(env_path)
    
    try:
        import environ
        if env_path.exists():
            environ.Env.read_env(str(env_path))
    except ImportError:
        pass 

    # الآن نقوم بتشغيل الماكرو والاختصارات بأمان كامل بعد استقرار البيئة
    apply_cli_aliases()

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')
    
    # تشغيل الفحوصات الطبية والمخزنية للبنية التحتية والشبكة
    check_infrastructure_health()
    pre_flight_checks()

    # =====================================================================
    # 🛡️ 4. حارس العمارة السحابية والـ Multi-Tenancy (Command Interceptor)
    # =====================================================================
    if 'migrate' in sys.argv and 'migrate_schemas' not in sys.argv:
        print("\n🛑 [SaaS ARCHITECTURE ERROR]: حظر أمني! يمنع استخدام أمر 'migrate' العادي في إمبراطورية Mouss Tec!")
        print("💡 التوجيه الهندسي الصواب: استخدم معالج النطاقات لتوجيه الجداول للـ Schema الصحيحة:")
        print("   python manage.py migrate_schemas --shared  (لتحديث جداول السوق والمزادات المركزية)")
        print("   python manage.py migrate_schemas --tenant  (لتحديث مخازن وخزائن الفروع والورش)")
        print("   أو اختصر الطريق تماماً واكتب: python manage.py db:push\n")
        sys.exit(1)

    if 'createsuperuser' in sys.argv and '--schema' not in ' '.join(sys.argv) and 'tenant_command' not in sys.argv:
        print("\n🛑 [SaaS SECURITY ERROR]: خرق أمني! لا يمكن إنشاء مدير للنظام دون تحديد هوية النطاق (Schema)!")
        print("💡 التوجيه الهندسي الصواب: لإنشاء جذر الإدارة المركزي أو أدمن لفرع معين، استخدم:")
        print("   python manage.py tenant_command createsuperuser --schema=public\n")
        sys.exit(1)

    # =====================================================================
    # 🚨 5. قفل الإنتاج ورادار حماية البيانات الحساسة (Production Kill-Switch)
    # =====================================================================
    critical_commands = ['flush', 'dropdb', 'dbshell', 'sqlflush']
    is_production = os.environ.get('DEBUG') == 'False' or os.environ.get('ENV') == 'production'

    if any(cmd in sys.argv for cmd in critical_commands):
        if is_production:
            print("\n⛔ [FATAL PRODUCTION LOCK]: حظر سيادي صارم! تحذير من مسح أو تعديل الداتابيز على السيرفر الحي!")
            print("🚨 هذا الخادم يحتوي على دفاتر أستاذ مالية (Ledger)، أرصدة Escrow مجمّدة وعقود أساطيل حية!")
            confirm = input("⚠️ هل أنت متأكد بنسبة 100%؟ هذه العملية تدميرية ولا يمكن التراجع عنها مطلقاً.\nاكتب 'DESTROY-MOUSSTEC-DB' لفك القفل: ")
            if confirm != 'DESTROY-MOUSSTEC-DB':
                sys.exit("\n✅ تم كبح القفل وإحباط العملية. أصول وعطاءات الورش والتجار في أمان تام وعزل سياسي.")
        else:
            print(f"🔐 [MOUSS TEC AUDIT]: Alert! A critical database command is being executed locally: {' '.join(sys.argv[1:])}")

    # =====================================================================
    # 🎨 6. واجهة إقلاع المحرك اللحظي (Mouss Tec Boot Sequence)
    # =====================================================================
    if 'runserver' in sys.argv or 'daphne' in sys.argv[0]:
        print("\n" + "━"*65)
        print("🚀 MOUSS TEC ECOSYSTEM ENGINE IS IGNITING...")
        print("🛡️  Architecture: Multi-Tenant B2B SaaS | Anti-Theft Guard: Active")
        print("📡  WebSockets Channels: Ready | AI Core Agents: Standby")
        print("━"*65 + "\n")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc

    execute_from_command_line(sys.argv)

if __name__ == '__main__':
    main()