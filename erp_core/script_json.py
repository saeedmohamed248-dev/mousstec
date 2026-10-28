"""JSON آمن يتحط جوه <script> في القوالب (بـ |safe).

🐛 [FIX — XSS]: json.dumps مابيهرّبش «<» — فاسم عميل/صنف فيه
«</script><script>…» (ممكن ييجي من اسم بروفايل واتساب أو حجز أونلاين) كان
بيقفل وسم السكريبت ويشغّل كود في صفحة التقارير عند صاحب الشركة.
بنهرّب < > & زي django.utils.html.json_script — الناتج JSON صحيح وبيتقري بنفس القيم.
"""
import json

_ESCAPES = {ord('<'): '\\u003c', ord('>'): '\\u003e', ord('&'): '\\u0026'}


def script_json(obj, **kwargs):
    return json.dumps(obj, **kwargs).translate(_ESCAPES)
