#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys
import subprocess
import shutil
from pathlib import Path
import socket 

# =====================================================================
# 🚀 1. الفحوصات الاستباقية والأتمتة الذكية (Pre-flight Mouss Tec Checks)
# =====================================================================
def auto_heal_env_file(env_path):
    """🛠️ ابتكار: المعالج الذاتي لبيئة العمل (Auto-Env Bootstrapper)"""
    example_env = env_path.parent / '.env.example'
    if not env_path.exists() and example_env.exists():
        print("🌱 [MOUSS TEC AUTO-HEAL]: لم يتم العثور على ملف '.env'. جاري استنساخ '.env.example' تلقائياً...")
        shutil.copy(example_env, env_path)
        print("✅ تم إنشاء ملف '.env'. يرجى مراجعة إعداداته إذا لزم الأمر.\n")

def check_infrastructure_health():
    """📡 ابتكار: رادار نبض البنية التحتية قبل الإقلاع (Infrastructure Ping)"""
    if 'runserver' in sys.argv or 'up' in sys.argv:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex(('127.0.0.1', 5432)) != 0:
                print("⚠️  [MOUSS TEC INFRASTRUCTURE]: سيرفر قاعدة البيانات (PostgreSQL) لا يستجيب!")
                print("💡 نصيحة: تأكد من تشغيل خدمة الداتا بيز أو حاوية Docker.\n")
            
            if s.connect_ex(('127.0.0.1', 6379)) != 0:
                print("⚠️  [MOUSS TEC INFRASTRUCTURE]: سيرفر الكيش (Redis) لا يستجيب!")
                print("💡 تنبيه: ستكون هناك مشاكل في المزادات الحية والـ WebSockets.\n")

def pre_flight_checks():
    """نظام فحص ذكي يعمل قبل تنفيذ أي أمر لضمان الأمان والاستقرار."""
    if sys.version_info < (3, 9):
        sys.exit("🛑 [MOUSS TEC FATAL ERROR]: Mouss Tec Core Engine requires Python 3.9 or higher.")

    if sys.prefix == sys.base_prefix and 'runserver' in sys.argv:
        print("\n⚠️  [MOUSS TEC WARNING]: أنت تقوم بتشغيل الأوامر خارج البيئة الوهمية (venv)!")
        print("💡 نصيحة: لتفعيل البيئة، اكتب: source venv/bin/activate\n")

    if 'runserver' in sys.argv or 'up' in sys.argv:
        port = 8000 
        for arg in sys.argv:
            if arg.isdigit() or ':' in arg:
                try: port = int(arg.split(':')[-1])
                except ValueError: pass
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                print(f"\n🛑 [MOUSS TEC PORT CONFLICT]: البورت {port} مشغول حالياً ببرنامج آخر أو سيرفر معلق!")
                try:
                    output = subprocess.check_output(f"lsof -t -i:{port}", shell=True, stderr=subprocess.DEVNULL)
                    pids = output.decode('utf-8').strip().split('\n')
                    if pids:
                        print(f"👻 تم اكتشاف عمليات ميتة (PIDs: {', '.join(pids)}) تمنع السيرفر من العمل.")
                        kill_choice = input("🔪 هل تريد تدميرها تلقائياً وتشغيل السيرفر؟ (y/N): ").lower()
                        if kill_choice == 'y':
                            for pid in pids:
                                subprocess.run(f"kill -9 {pid}", shell=True)
                            print("✅ تم التطهير! جاري تشغيل السيرفر...\n")
                            return 
                except Exception:
                    pass 
                
                print(f"💡 نصيحة: قم بإنهاء السيرفر القديم، أو شغل النظام على بورت مختلف هكذا:")
                print(f"   python manage.py runserver {port + 1}\n")
                sys.exit(1)

# =====================================================================
# 🧠 2. مترجم الأوامر السريعة للمطورين (Smart CLI Aliases)
# =====================================================================
def apply_cli_aliases():
    """تحويل الاختصارات السريعة إلى أوامر جانجو المعقدة"""
    if len(sys.argv) > 1:
        alias = sys.argv[1]
        if alias == 'up':
            sys.argv[1] = 'runserver'
        elif alias == 'db:push':
            print("🚀 [MOUSS TEC COPILOT]: ترجمة 'db:push' إلى إنشاء وتطبيق الـ Migrations للسحابة...")
            subprocess.run([sys.executable, "manage.py", "makemigrations"])
            subprocess.run([sys.executable, "manage.py", "migrate_schemas", "--shared"])
            subprocess.run([sys.executable, "manage.py", "migrate_schemas", "--tenant"])
            sys.exit(0)

def main():
    """Run administrative tasks."""
    apply_cli_aliases()
    
    # =====================================================================
    # 🔒 3. حاقن الأسرار المبكر والمعالج الذاتي (Early Env Injector)
    # =====================================================================
    env_path = Path(__file__).resolve().parent / '.env'
    auto_heal_env_file(env_path)
    
    try:
        import environ
        if env_path.exists():
            environ.Env.read_env(str(env_path))
    except ImportError:
        pass 

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')
    
    check_infrastructure_health()
    pre_flight_checks()

    # =====================================================================
    # 🛡️ 4. حارس العمارة السحابية (Tenant Command Interceptor)
    # =====================================================================
    if 'migrate' in sys.argv and 'migrate_schemas' not in sys.argv:
        print("\n🛑 [SaaS ARCHITECTURE ERROR]: لا تستخدم أمر 'migrate' العادي في إمبراطورية Mouss Tec!")
        print("💡 الصح: استخدم الأمر المخصص لتوجيه الجداول للريسبشن أو الفروع:")
        print("   python manage.py migrate_schemas --shared  (للسوق المركزي)")
        print("   python manage.py migrate_schemas --tenant  (للشركات والمراكز)\n")
        print("   أو استخدم اختصار: python manage.py db:push\n")
        sys.exit(1)

    if 'createsuperuser' in sys.argv and '--schema' not in ' '.join(sys.argv) and 'tenant_command' not in sys.argv:
        print("\n🛑 [SaaS SECURITY ERROR]: لا يمكنك إنشاء مدير بدون تحديد نطاق الشركة (Schema)!")
        print("💡 الصح: لإنشاء أدمن للريسبشن العام (public) أو لورشة معينة، استخدم:")
        print("   python manage.py tenant_command createsuperuser --schema=public\n")
        sys.exit(1)

    # =====================================================================
    # 🚨 5. قفل الإنتاج ورادار الأوامر الحرجة (Production Kill-Switch)
    # =====================================================================
    critical_commands = ['flush', 'dropdb', 'dbshell', 'sqlflush']
    is_production = os.environ.get('DEBUG') == 'False' or os.environ.get('ENV') == 'production'

    if any(cmd in sys.argv for cmd in critical_commands):
        if is_production:
            print("\n⛔ [FATAL PRODUCTION LOCK]: تحذير! محاولة مسح قاعدة البيانات في السيرفر الحي!")
            print("🚨 هذا السيرفر يحتوي على محافظ مالية (Escrow) وعطاءات نشطة للتجار!")
            confirm = input("⚠️ هل أنت متأكد بنسبة 100%؟ هذه العملية لا يمكن التراجع عنها.\nاكتب 'DESTROY-MOUSSTEC-DB' للتأكيد: ")
            if confirm != 'DESTROY-MOUSSTEC-DB':
                sys.exit("\n✅ تم إحباط العملية. بيانات السوق والعملاء في أمان تام.")
        else:
            print(f"🔐 [MOUSS TEC AUDIT]: Alert! A critical database command is being executed: {' '.join(sys.argv[1:])}")

    # =====================================================================
    # 🎨 6. واجهة إقلاع السيرفر (Mouss Tec Boot Sequence)
    # =====================================================================
    if 'runserver' in sys.argv or 'daphne' in sys.argv[0]:
        print("\n" + "━"*65)
        print("🚀 MOUSS TEC ECOSYSTEM ENGINE IS IGNITING...")
        print("🛡️  Architecture: Multi-Tenant B2B SaaS | Anti-Theft: Active")
        print("📡  WebSockets: Ready | AI Core: Standby")
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