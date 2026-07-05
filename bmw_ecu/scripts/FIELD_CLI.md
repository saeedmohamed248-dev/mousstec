# Mousstec Field CLI — تجربة حقيقية على العربية من غير الويب

أداة سطر أوامر بتكلّم العربية عبر الـ **CANable** مباشرة — **من غير داتابيز ولا
Redis ولا الموقع**. بتستخدم نفس منطق الـ Coding Room المتِست.

## المتطلبات (مرة واحدة)

1. **لابتوب** (Windows / Mac / Linux) عليه **Python 3.10+**.
2. **أدابتر CANable / slcan** — مش الكابل الأزرق FTDI.
3. تثبيت 3 مكتبات بس:

```bash
pip install python-can can-isotp pyserial
```

4. المشروع على اللابتوب (`git clone` للـ branch)، وتشتغل من جوّه فولدر المشروع.

## توصيل الكابل بالـ OBD (D-CAN)

| CANable | فيشة OBD |
|---|---|
| CAN-H | pin 6 |
| CAN-L | pin 14 |
| GND | pin 4/5 |

الكونتاكت **ON**. دوّر على اسم الجهاز:
- Linux: `ls /dev/ttyACM* /dev/ttyUSB*`
- Mac: `ls /dev/cu.usbmodem* /dev/cu.usbserial-*`

## الأوامر

```bash
# 1) اتأكد الكابل بيكلّم العربية
python -m bmw_ecu.scripts.field_cli ping --port /dev/ttyACM0 --tx 0x6F1 --rx 0x612

# 2) اقرا الـ FA من العربية
python -m bmw_ecu.scripts.field_cli read-fa --port /dev/ttyACM0 --tx 0x6F1 --rx 0x612

# 3) التشخيص الكامل (تغيير الكمبيوتر). الـ ISN اختياري لو معاك.
python -m bmw_ecu.scripts.field_cli diagnose --engine N18 --bench \
    --port /dev/ttyACM0 --tx 0x6F1 --rx 0x612

# 4) معاينة تحديث الـ FA (محتاج كتالوج فيه القيمة المتأكّدة)
python -m bmw_ecu.scripts.field_cli fa-plan --to N18 --catalog fa_catalog.json \
    --port /dev/ttyACM0 --tx 0x6F1 --rx 0x612

# 5) كتابة الـ FA فعلياً (بعد ما تراجع الخطة)
python -m bmw_ecu.scripts.field_cli fa-write --to N18 --confirm --catalog fa_catalog.json \
    --port /dev/ttyACM0 --tx 0x6F1 --rx 0x612
```

> بدل ما تكتب `--port/--tx/--rx` كل مرة، تقدر تعملهم export:
> `export BMW_ECU_KDCAN_PORT=/dev/ttyACM0 BMW_ECU_CAN_TX_ID=0x6F1 BMW_ECU_CAN_RX_ID=0x612`

## الكتالوج (عشان تحديث الـ FA)

`fa-plan` و `fa-write` بيرفضوا لحد ما تسجّل **كود N18 المتأكّد** (مقروء من عربية
N18 سليمة). اعمل ملف `fa_catalog.json`:

```json
{
  "type_code_engine": { "3F30": "N14" },
  "engine_transforms": [
    { "from": "N14", "to": "N18", "new_type_code": "<كود N18 المتأكّد>" }
  ]
}
```

## الحقيقة الصريحة

- **قراية الـ FA + التشخيص + معاينة/كتابة الـ FA** → شغّالين بالكابل.
- **تزاوج الـ ISN (اللي هيدوّر العربية)** → لسه محتاج **بنش** (N18 مايتكتبش OBD)،
  الأداة بتقولك كده وما بتدّعيش إنها عملته.
- **كود N18 المتأكّد** لازم يجي من عربية حقيقية — النظام مبيخمّنوش.

## أنسب ترتيب لأول تجربة

1. `ping` → تتأكد الوصلة.
2. `read-fa` → تشوف VO عربيتك الحقيقي (ده أول انتصار).
3. `diagnose` → يأكّدلك السبب والخطوات.
4. لو جبت كود N18 → `fa-plan` بعدين `fa-write --confirm`.
