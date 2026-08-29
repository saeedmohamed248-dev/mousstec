# 🔩 محرك الفك التدريجي المتعدد المستويات + قوالب الفك (Reverse BOM)

أي عنصر مخزون ممكن يبقى "أب" ويتفكّك لعناصر "أبناء"، والابن نفسه ممكن
يتفكّك بدوره → **عمق لا نهائي**:

```
نص كت (Half-cut)
 └── محرك كامل
      ├── شورت بلوك
      ├── رأس المحرك ──► يتفكّك بدوره ──► كامة + سوباب...
      ├── دينامو
      └── كمبروسر تكييف
```

## توزيع التكلفة — طريقة القيمة البيعية (Sales Value Method)

```
تكلفة الأب المعدّلة = تكلفة الأب − إيراد الخردة
وزن الابن           = سعره التقديري ÷ إجمالي الأسعار التقديرية
تكلفة الابن         = الوزن × تكلفة الأب المعدّلة
```

**نزاهة مالية صفرية التسريب:** `DecimalField` + تقريب بنكي ثابت +
`transaction.atomic` + `select_for_update`. فروق المليمات تُضاف لأغلى ابن حتى:

```
Σ(تكلفة الأبناء) + إيراد الخردة  ≡  تكلفة الأب   (بالمليم بالظبط)
```

حارس نزاهة يرفض التنفيذ لو المعادلة مش متوازنة.

## الموديلات

| الموديل | الغرض |
|---|---|
| `InventoryItem` | عنصر عام (أب/ابن) — `sku`, `name`, `cost`, `status`. خصائص شجرة: `parent_item`, `depth` |
| `DisassemblyEvent` | حدث فك — `parent_item`, `total_scrap_revenue`, لقطات مجمّدة للتدقيق |
| `DisassemblyResult` | ناتج فك — `event`, `child_item`, `estimated_sales_price`, `allocated_cost` |
| `DisassemblyTemplate` | قالب فك قياسي (Reverse BOM) — `name`, `engine_code`, `default_scrap_revenue` |
| `TemplateItem` | بند قالب — `part_name`, `default_estimated_sales_price` **أو** `weight_percentage %` |

## قوالب الفك (Reverse BOM)

بدل ما تكتب أبناء محرك N20 يدوي كل مرة، تعرّف القالب مرة واحدة وتحمّله.
البند بياخد **سعر تقديري افتراضي** أو **نسبة وزن %** (تتحوّل لسعر من قيمة الأب).

## طبقة الأعمال — `DisassemblyService`

```python
from inventory.services import DisassemblyService

# فك مباشر
event, report = DisassemblyService.disassemble(
    parent_item=half_cut,
    children=[
        {'sku': 'N20-SB', 'name': 'شورت بلوك', 'estimated_sales_price': Decimal('45000')},
        {'sku': 'N20-HEAD', 'name': 'رأس محرك', 'estimated_sales_price': Decimal('30000')},
    ],
    total_scrap_revenue=Decimal('5000'),
)

# أو من قالب: يبني مسودة، تعدّلها، بعدين تنفّذ
draft = DisassemblyService.load_template(parent_item=engine, template=n20_template)
# ... المستخدم يشيل بند تالف أو يظبط سعر ...
report = DisassemblyService.execute_disassembly(draft)
```

## واجهات الـ API (JSON — تسجيل دخول مطلوب)

| المسار | الوصف |
|---|---|
| `GET  /inventory/disassembly/templates/?engine=N20` | قائمة القوالب |
| `POST /inventory/disassembly/load-template/` | يبني مسودة من قالب (`parent_item_id`, `template_id`) |
| `POST /inventory/disassembly/result/<id>/update/` | تعديل سعر بند (`estimated_sales_price`) |
| `POST /inventory/disassembly/result/<id>/remove/` | شيل بند تالف من المسودة |
| `POST /inventory/disassembly/<event_id>/execute/` | اعتماد وتوزيع التكلفة |

## الملفات

- `inventory/models/disassembly.py`
- `inventory/services/disassembly_service.py`
- `inventory/views/disassembly.py`
- `inventory/admin/disassembly.py`
- migrations: `0029_recursive_disassembly`, `0030_disassembly_templates`
