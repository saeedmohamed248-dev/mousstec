"""
robot/services — business logic bridging the hardware to the Mouss Tec ERP.

Pure functions/helpers (no HTTP here — see `robot/views/`). Everything that touches a
product price goes through `robot.pricing.safe_product_payload` so wholesale/cost
can never leak to the robot.

Integrations:
  * inventory.Product / Inventory / Branch / SaleInvoice / StockAlert
  * hr.Employee / AttendanceRecord (via robot.security)
  * diagnostics_catalog.DTCDefinition — fault codes ("قرابات الأعطال")
  * bmw_ecu.EcuHardwareProfile / EcuPinoutDiagram — ECU coding/programming refs

🔀 مقسومة موديول لكل مجال — والأسماء كلها بتتصدّر من هنا، فـ `services.X` في باقي
الروبوت وفي الاختبارات شغال زي ما هو.

| الموديول | فيه إيه |
|---|---|
| catalog     | اسم المحل، البحث عن القطع والمخزون، القطعة اللي اتقالت في جملة، سعر المستعمل، الرف |
| device      | الموتورات، أوامر الحركة، أونلاين/أوفلاين، الراس، حارس بعد المواعيد، البطارية، نداء الموظفين |
| ai          | الرد بالذكاء الاصطناعي والبحث على النت |
| pos         | البيع بسعر القطاعي وإشارة الشراء لما المخزون يقل |
| offline     | كتالوج الأوفلاين ومزامنة أحداثه |
| diagnostics | أكواد الأعطال ومراجع الـ ECU |
| learning    | ذاكرة التعلّم (بصمة الصورة، الأسماء المتعلّمة، تصحيح المسح) |
| intake      | استلام البضاعة بالصورة |
| stock_take  | الجرد بالصوت |

🔌 **SEAMS** — الدوال اللي الاختبارات بتعمل لها `mock.patch.object(services, "X")`.
جوه الحزمة بتتنادى دايماً `services.X(...)` (عن طريق الحزمة) مش `X(...)` مباشرة، عشان
الـ patch على الحزمة يوصل لكل اللي بينادوها. عايز تعمل patch لدالة جديدة؟ ضيفها هنا
ونادِها بنفس الطريقة — `erp_core.tests.test_codebase_structure` بيتأكد من الاتنين.
"""
import logging

from .catalog import (  # noqa: F401
    branch_stock, find_product, inventory_answer, match_part_in_sentence, names_a_part,
    part_keys, point_to_shelf, shelf_location, shop_name, spoken_price, suggest_used_price,
    used_price_calibration, welcome,
)
from .device import (  # noqa: F401
    HEAD_DEAD_ZONE, HEAD_EDGE_MS, HEAD_MIN_MS, MOTOR_COMMAND_TTL, MOTOR_CURRENT_LIMIT_A,
    MOTOR_DEFAULT_MS, MOTOR_DIRECTIONS, MOTOR_MAX_MS, OFFLINE_AFTER_SECONDS, STAFF_CALL,
    STAFF_CALL_EVERY_S, call_staff, guard_window, hands_to_staff, head_edge_ms,
    head_flipped, is_after_hours, look_at, motor_runtime_ms, note_device_seen,
    raise_after_hours_alert, raise_low_battery_alert, raise_offline_alerts,
    record_motor_currents, take_camera_commands, validate_motor_command,
)
from .ai import (  # noqa: F401
    GATEWAY_IF_UNDER_S, REPLY_BUDGET_S, REPLY_TIMEOUT_S, ai_reply, invented_price,
    web_answer,
)
from .pos import (  # noqa: F401
    create_robot_sale, create_robot_sale_lines, is_cash_payment,
    maybe_raise_procurement_signal,
)
from .offline import (  # noqa: F401
    apply_offline_events, catalog_tag, offline_catalog,
)
from .diagnostics import (  # noqa: F401
    lookup_ecu_profile, lookup_fault_code,
)
from .learning import (  # noqa: F401
    image_hash, learn_from_confirmation, learned_alias_in, match_fingerprint,
    models_F_increment, nearest_fingerprint_key, resolve_from_knowledge, scan_fingerprint,
    teach_scan_correction, unlearn,
)
from .intake import (  # noqa: F401
    intake_part,
)
from .stock_take import (  # noqa: F401
    add_stock_take_count, apply_stock_take, complete_stock_take, get_open_stock_take,
    parse_count_utterance, run_stock_take, start_stock_take,
)

# أسماء بشرطة تحتية (مش في القوايم اللي فوق) — كانت متاحة من الملف القديم
from .catalog import _ARABIC_DIGITS, _ARTICLES, _CALIBRATION_MIN_SALES, _QUESTION_WORDS, _choice_label, _forms, choices_said, spoken_name, _part_words, _spellings  # noqa: F401
from .device import _STAFF_ASKED, _STAFF_WORDS, _WEAR_MIN_SAMPLES, _WEAR_RATIO, _dt_time  # noqa: F401
from .ai import _AI_ENGLISH, _AI_FACTS_PROMPT, _AI_SYSTEM_PROMPT, _CURRENCY, _MARKUP, _NUMBER, _WEB_PROMPT, _WEEKDAYS, _shop_and_time  # noqa: F401
from .pos import _cash_treasury  # noqa: F401
from .offline import _heard_sentences, _open_rfq_for, _outage_alert  # noqa: F401
from .diagnostics import _normalize_key  # noqa: F401
from .learning import _FINGERPRINT_MAX_DISTANCE, _KNOWLEDGE_SCAN_LIMIT, _hamming  # noqa: F401
from .intake import _attach_photo, _image_fingerprint  # noqa: F401

# 🔌 الدوال اللي الاختبارات بتعمل لها patch — بتتنادى جوه الحزمة كـ `services.X` (شوف فوق)
SEAMS = (
    '_cash_treasury',
    'ai_reply',
    'branch_stock',
    'create_robot_sale',
    'find_product',
    'get_open_stock_take',
    'image_hash',
    'inventory_answer',
    'is_after_hours',
    'learn_from_confirmation',
    'match_fingerprint',
    'match_part_in_sentence',
    'maybe_raise_procurement_signal',
    'nearest_fingerprint_key',
    'raise_after_hours_alert',
    'raise_low_battery_alert',
    'scan_fingerprint',
    'shelf_location',
    'shop_name',
    'take_camera_commands',
    'unlearn',
    'web_answer',
)

logger = logging.getLogger("mouss_tec_core")  # `services.logger` بيتستخدم من views
