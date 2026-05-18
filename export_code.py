import os

# المجلدات اللي عايزين نفحصها في مشروعك
TARGET_FOLDERS = ['inventory', 'clients', 'erp_core', 'templates']
OUTPUT_FILE = 'full_project_code.txt'

print("🚀 جاري تجميع أكواد المشروع...")

with open(OUTPUT_FILE, 'w', encoding='utf-8') as outfile:
    for folder in TARGET_FOLDERS:
        if not os.path.exists(folder):
            continue
        for root, dirs, files in os.walk(folder):
            # استثناء مجلدات الكاش والميجريشنز عشان الملف ميكبرش على الفاضي
            if '__pycache__' in root or 'migrations' in root:
                continue
            
            for file in files:
                # هنجيب ملفات البايثون والـ HTML بس
                if file.endswith('.py') or file.endswith('.html'):
                    file_path = os.path.join(root, file)
                    outfile.write(f"\n{'='*60}\n")
                    outfile.write(f"📁 FILE: {file_path}\n")
                    outfile.write(f"{'='*60}\n\n")
                    try:
                        with open(file_path, 'r', encoding='utf-8') as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"⚠️ Error reading file: {e}\n")

print(f"✅ تم الانتهاء! تم تجميع الأكواد في ملف: {OUTPUT_FILE}")