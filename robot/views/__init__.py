"""
robot/views — REST API the ESP32 firmware calls.

Auth model: the ESP32 authenticates as a *device* via `X-Robot-Token`
(`robot.security.authenticate_device`), not as a Django user — the robot stands
on the shop floor, not behind a login. Privileged actions (creating a sale,
dispensing a part) additionally require a face-authorized employee in the same
request, enforced here. The device may send the face embedding itself, or echo
back an `employee_id` from a face match this same device was granted in the
last few minutes — an id on its own is never accepted, or the device token
alone would be enough to sell as anybody.

Every product/price response is built by `robot.pricing.safe_product_payload`,
so wholesale/cost can never reach the device.

Endpoints (prefix /api/robot/v1/ — see urls.py):
  POST heartbeat/            device liveness + firmware version
  POST scan/                 ESP32-CAM part image → identify → stock/price (or scrap price)
  POST voice/                transcript → intent → spoken reply (workshop assistant + POS)
  POST face/                 face embedding → attendance clock-in/out + authorize session
  POST sale/                 create a retail sale from a scan (requires face auth)
  POST motor/                queue a physical-articulation command (requires face auth)
  GET  motor/pending/        commands the ESP32 should execute + ack
  GET  procurement-signals/  open low-stock signals for the Procurement Agent
  POST teach/                staff correct a scan / teach an alias (learning loop)
  POST enroll/capture/       camera capture for the staff face-enrollment round

🔀 مقسومة موديول لكل مجال — والأسماء كلها بتتصدّر من هنا، فـ `robot/urls.py` (`views.X`)
وباقي الروبوت والاختبارات شغالين زي ما هم.

| الموديول | فيه إيه |
|---|---|
| _common      | توثيق الجهاز والموظف (وش/جلسة) والصلاحيات وغلاف الـ endpoints |
| device       | heartbeat، الحالة، telemetry، الصور، الأوامر المعلّقة، sync، تسجيل الوشوش |
| scanning     | مسح القطعة بالكاميرا |
| speech       | استقبال الكلام والرد المسموع والأوفلاين |
| conversation | فهم الكلام والرد عليه (`_handle_voice` / `_answer_voice`) |
| attendance   | حضور وانصراف الموظفين وترحيب العملاء |
| selling      | البيع من الروبوت |
| motion       | الموتورات، الكلام، إطار الكاميرا، بص على |
| inventory    | الاستلام، الجرد، التعليم، الكشك، إشارات الشراء |

🔌 **SEAMS** — الأسماء اللي الاختبارات بتعمل لها `mock.patch.object(views, "X")`
(توثيق الجهاز/الموظف، `_handle_voice`، الموديلات). جوه الحزمة بتتنادى دايماً `views.X`
عشان الـ patch على الحزمة يوصل لكل الـ endpoints. عايز تعمل patch لاسم جديد؟ ضيفه
لـ SEAMS ونادِه بنفس الطريقة — `erp_core.tests.test_codebase_structure` بيتأكد.
"""
from __future__ import annotations

# ⬇️ الموديولات والأسماء دي متاحة على `views` عن قصد: باقي الروبوت والاختبارات بيستخدموها
#    (`views.services`، `views.entries`، `views.RobotAccessLog`…). أسماء الموديولات اللي جوه
#    الحزمة مختلفة عن أسماء الـ endpoints (`scanning` مش `scan`) عشان `views.scan` يفضل الدالة.
from .. import audio as audio_svc  # noqa: F401
from .. import enrollment, entries, faces, permissions, replies, security, services, vision, wakename  # noqa: F401
from ..models import (  # noqa: F401
    MotorCommandLog, RobotAccessLog, RobotCommand, RobotScanEvent, RobotSnapshot, RobotVoiceInteraction,
)
from ..pricing import safe_product_payload  # noqa: F401

from .device import (  # noqa: F401
    commands_ack, commands_pending, enroll_capture, heartbeat, service_status,
    snapshot_upload, sync_pull, sync_push, telemetry,
)
from .scanning import (  # noqa: F401
    scan,
)
from .speech import (  # noqa: F401
    SAID_AGO_MAX_MS, voice, voice_offline,
)

from .attendance import (  # noqa: F401
    customer_greet, face,
)
from .selling import (  # noqa: F401
    sale,
)
from .motion import (  # noqa: F401
    camera_frame, look, motor, motor_pending, speak,
)
from .inventory import (  # noqa: F401
    intake, kiosk_customer, kiosk_part, kiosk_return_check, procurement_signals, stock_take,
    stock_take_apply, teach,
)

# أسماء بشرطة تحتية (مش في القوايم اللي فوق) — كانت متاحة من الملف القديم
from ._common import _FACE_SESSION_MINUTES, _MIN_VISION_CONFIDENCE, _VOICE_FACE_SECONDS, _authorized_employee, _device_or_401, _employee_is_active, _kiosk_lookup_endpoint, _require_permission, _robot_endpoint, _voice_employee  # noqa: F401
from .scanning import _announce_scan  # noqa: F401
from .speech import _apply_offline_voice, _log_offline_turn, _recorded_at, _said_at, _timing_line, _voice_audio, _voice_reply, _voice_turn  # noqa: F401
from .conversation import _ENROLL_CANCEL_PHRASES, _ENROLL_SKIP_PHRASES, _PAGE_ACK_RE, _TEACH_RE, _answer_meant, _answer_voice, _answer_voice_as, _discuss, _enrollment_control, _handle_voice, _is_enrollment_control, _part_reply, _talk_shop, _understood  # noqa: F401
from .attendance import _attendance_greeting  # noqa: F401

# 🔌 الأسماء اللي الاختبارات بتعمل لها patch — بتتنادى جوه الحزمة كـ `views.X` (شوف فوق)
SEAMS = (
    'MotorCommandLog',
    'RobotAccessLog',
    'RobotCommand',
    'RobotScanEvent',
    'RobotVoiceInteraction',
    '_authorized_employee',
    '_device_or_401',
    '_handle_voice',
    '_require_permission',
    '_voice_employee',
    'safe_product_payload',
)
