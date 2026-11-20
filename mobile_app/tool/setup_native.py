#!/usr/bin/env python3
"""
📦 تجهيز تطبيق Mouss Tec للمتاجر (Google Play / App Store) — أمر واحد.

المستودع فيه كود Dart بس (lib/ و test/)، والقشرة الأصلية (android/ و ios/) بتتولّد
محلياً. السكربت ده:
  1. يولّد android/ و ios/ بـ `flutter create` (مابيلمسش lib/ ولا pubspec.yaml)
  2. يظبط الاسم والـ Application ID / Bundle ID والصلاحيات (الإنترنت، الكاميرا، الإشعارات)
  3. يظبط توقيع نسخة أندرويد من android/key.properties (لو موجود)
  4. يحط ملفات Firebase لو موجودة في firebase/ (للإشعارات)
  5. يولّد الأيقونة وشاشة البداية من assets/icon/icon.png

التشغيل (من جوه mobile_app):
    python3 tool/setup_native.py
وبعدها:
    flutter build appbundle --release      # Google Play
    flutter build ipa --release            # App Store (على ماك)

آمن تشغيله أكتر من مرة (كل خطوة بتتأكد إنها ماتعملتش قبل كده).
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = 'Mouss Tec'
ORG = 'com.mousstec'
APP_ID = os.environ.get('MOUSSTEC_APP_ID', 'com.mousstec.mobile')


def run(*cmd):
    print('$', ' '.join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def path(*parts):
    return os.path.join(ROOT, *parts)


def read(p):
    with open(p, encoding='utf-8') as fh:
        return fh.read()


def write(p, s):
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write(s)


def flutter_create():
    if not os.path.isdir(path('android')) or not os.path.isdir(path('ios')):
        run('flutter', 'create', '.', '--org', ORG, '--project-name', 'mousstec_mobile',
            '--platforms=android,ios')


def android():
    manifest = path('android', 'app', 'src', 'main', 'AndroidManifest.xml')
    s = read(manifest)
    s = re.sub(r'android:label="[^"]*"', f'android:label="{APP_NAME}"', s, count=1)
    perms = ['android.permission.INTERNET', 'android.permission.CAMERA',
             'android.permission.POST_NOTIFICATIONS']
    for p in perms:
        if p not in s:
            s = s.replace('<application', f'<uses-permission android:name="{p}"/>\n    <application', 1)
    write(manifest, s)

    gradle = path('android', 'app', 'build.gradle.kts')
    g = read(gradle)
    g = re.sub(r'applicationId = "[^"]*"', f'applicationId = "{APP_ID}"', g)
    # Firebase/الكاميرا محتاجين minSdk 23 على الأقل
    g = g.replace('minSdk = flutter.minSdkVersion', 'minSdk = maxOf(flutter.minSdkVersion, 23)')
    if 'key.properties' not in g:
        g = g.replace('plugins {', 'import java.util.Properties\nimport java.io.FileInputStream\n\nplugins {', 1)
        g = g.replace('android {', '''// 🔑 توقيع نسخة المتجر: android/key.properties (مش بيترفع على Git)
val keystoreProperties = Properties()
val keystorePropertiesFile = rootProject.file("key.properties")
if (keystorePropertiesFile.exists()) {
    keystoreProperties.load(FileInputStream(keystorePropertiesFile))
}

android {''', 1)
        g = g.replace('''    buildTypes {''', '''    signingConfigs {
        if (keystorePropertiesFile.exists()) {
            create("release") {
                keyAlias = keystoreProperties["keyAlias"] as String
                keyPassword = keystoreProperties["keyPassword"] as String
                storeFile = file(keystoreProperties["storeFile"] as String)
                storePassword = keystoreProperties["storePassword"] as String
            }
        }
    }

    buildTypes {''', 1)
        g = g.replace('signingConfig = signingConfigs.getByName("debug")',
                      'signingConfig = if (keystorePropertiesFile.exists()) signingConfigs.getByName("release")'
                      ' else signingConfigs.getByName("debug")')
    write(gradle, g)

    gs = path('firebase', 'google-services.json')
    if os.path.exists(gs):
        shutil.copy(gs, path('android', 'app', 'google-services.json'))
        settings = path('android', 'settings.gradle.kts')
        st = read(settings)
        if 'com.google.gms.google-services' not in st:
            st = st.replace('id("com.android.application")',
                            'id("com.google.gms.google-services") version "4.4.4" apply false\n'
                            '    id("com.android.application")', 1)
            write(settings, st)
        g = read(gradle)
        if 'com.google.gms.google-services' not in g:
            g = g.replace('id("com.android.application")',
                          'id("com.android.application")\n    id("com.google.gms.google-services")', 1)
            write(gradle, g)
        print('✅ Firebase (Android) متظبط')
    else:
        print('ℹ️  firebase/google-services.json مش موجود — الإشعارات على أندرويد مقفولة لحد ما تحطه')


def ios():
    plist = path('ios', 'Runner', 'Info.plist')
    s = read(plist)
    s = re.sub(r'(<key>CFBundleDisplayName</key>\s*<string>)[^<]*(</string>)', rf'\g<1>{APP_NAME}\g<2>', s)
    extra = {
        'NSCameraUsageDescription': 'الكاميرا بتُستخدم لمسح باركود قطع الغيار والخامات.',
        'ITSAppUsesNonExemptEncryption': None,
    }
    for key, val in extra.items():
        if f'<key>{key}</key>' in s:
            continue
        entry = (f'\t<key>{key}</key>\n\t<string>{val}</string>\n' if val is not None
                 else f'\t<key>{key}</key>\n\t<false/>\n')
        s = s.replace('</dict>\n</plist>', entry + '</dict>\n</plist>', 1)
    if '<key>UIBackgroundModes</key>' not in s:
        s = s.replace('</dict>\n</plist>', '\t<key>UIBackgroundModes</key>\n\t<array>\n'
                      '\t\t<string>remote-notification</string>\n\t</array>\n</dict>\n</plist>', 1)
    write(plist, s)

    pbx = path('ios', 'Runner.xcodeproj', 'project.pbxproj')
    p = read(pbx)
    p = re.sub(r'PRODUCT_BUNDLE_IDENTIFIER = com\.mousstec\.mousstecMobile;',
               f'PRODUCT_BUNDLE_IDENTIFIER = {APP_ID};', p)
    write(pbx, p)

    podfile = path('ios', 'Podfile')
    if os.path.exists(podfile):
        pf = read(podfile)
        pf = re.sub(r"^#?\s*platform :ios, '[\d.]+'", "platform :ios, '15.0'", pf, flags=re.M)
        write(podfile, pf)

    gs = path('firebase', 'GoogleService-Info.plist')
    if os.path.exists(gs):
        shutil.copy(gs, path('ios', 'Runner', 'GoogleService-Info.plist'))
        print('✅ Firebase (iOS) اتنسخ — ضيفه لـ Runner في Xcode مرة واحدة (Add Files to "Runner")')
    else:
        print('ℹ️  firebase/GoogleService-Info.plist مش موجود — الإشعارات على iOS مقفولة لحد ما تحطه')


def icons_and_splash():
    run('dart', 'run', 'flutter_launcher_icons')
    run('dart', 'run', 'flutter_native_splash:create')


def main():
    os.chdir(ROOT)
    flutter_create()
    android()
    ios()
    if '--no-icons' not in sys.argv:
        icons_and_splash()
    print('\n✅ جاهز. نسخة المتجر:\n'
          '   flutter build appbundle --release   (Google Play)\n'
          '   flutter build ipa --release         (App Store — على ماك)')


if __name__ == '__main__':
    main()
