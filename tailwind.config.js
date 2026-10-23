/* 🎨 Tailwind للصفحات المستقلة (الدخول، الأسعار، بوابة السيارات، الإعدادات…).
 * الـ CSS بيتبني مرة (scripts/build_css.sh) ويتحفظ في static/vendor/css/tailwind.css
 * بدل نسخة الـ Play CDN اللي كانت بتتحمّل 400KB JavaScript وتبني الـ CSS في المتصفح.
 * أي class جديد في قالب ⇒ `make css` — والـ CI بيفشل لو الملف مش متحدّث.
 * ⚠️ الـ class اللي بيتركّب من متغيّر (bg-{{ color }}-600) مابيتشافش — يتكتب كامل أو يتضاف للـ safelist. */
const PLAN_COLORS = ['pink', 'purple', 'amber', 'slate', 'yellow'];  // clients/views/subscription_views.py

module.exports = {
  content: [
    './templates/**/*.html',
    './*/templates/**/*.html',
    './*/**/*.py',
    './static/**/*.js',
    '!./static/vendor/**',
    '!./staticfiles/**',
    '!./*/migrations/**',
    '!./*/tests/**',
    '!./node_modules/**',
    '!./e2e/**',
  ],
  // manage_subscription.html: bg-{{ plan.color }}-600/20 و text-{{ plan.color }}-400
  safelist: PLAN_COLORS.flatMap((c) => [`bg-${c}-600/20`, `text-${c}-400`]),
  theme: { extend: {} },
  plugins: [],
};
