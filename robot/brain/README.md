# «مخ موس» — the robot on a real computer

The ESP32 bridge sent every question's audio to the server, where Gemini
wrote it down: 3.5–4.8 s of the 5–11 s before each answer, and an unsoldered
mic pin or a slow line made the robot deaf. Here a **mini PC** (or a Mac, to
try it) with a USB mic and speakers does the listening itself:

```
 mic ──► VAD (a sentence) ──► Whisper, on this computer ──► the words
                                                              │
 speakers ◄── the spoken reply ◄── /api/robot/v1/voice/ ◄─────┘
                                   (stock · retail price · Egyptian numbers, ~10 ms)
```

| Before (ESP32) | Now |
|---|---|
| audio up to the server, Gemini writes it down (3.5–4.8 s, 429/503 at peak hours) | Whisper on the machine (`bench` shows how fast on yours) |
| 1.8 s of RAM per sentence | 12 s |
| a soldered I2S mic | any USB / Bluetooth mic |

The server is unchanged: `/voice/` has always taken `transcript` (the words)
as well as `audio`, and answers with the spoken reply (`reply_audio=pcm`).
Same device token as the bridge. If Whisper can't load (not installed, no
model), the brain sends the recording instead and the server listens as before.

What it does to hear a parts shop well: the hint read before every clip is the
robot's name plus the names of the parts in stock (from `/sync/pull/`,
refreshed every 15 min); what Whisper invents over noise («ترجمة نانسي
قنقر», "thanks for watching") is dropped; a sentence with not one quiet frame
in it (a fan, a radio) isn't sent to the ears at all. The room's level is
learned on every frame, so a loud shop stops "talking" after a few seconds.
When the server can't be reached it says «النت فاصل» in its own voice, kept
on disk.

Self-contained: numpy, requests, sounddevice, faster-whisper (MIT — fine to
ship). Tests: `robot/tests/test_brain.py`.

## التشغيل على الماك (للتجربة)

1. نزّل **Python 3.12** من <https://www.python.org/downloads/> وسطّبه.
2. فك الـ zip، وفي الفولدر `mouss-brain` هتلاقي `brain.ini` فيه عنوان ورشتك والتوكن.
3. افتح **Terminal** واكتب:
   ```bash
   cd ~/Downloads/mouss-brain
   bash run_mac.sh devices
   ```
   أول مرة هياخد كام دقيقة بيجهّز البرامج. هيطلعلك المايكات والسماعات، والجهاز الافتراضي قدّامه `>` أو `<`.
4. لو المايك أو السماعة مش الافتراضي، اكتب جزء من اسمه في `brain.ini`
   (`mic = ` و `speaker = `).
5. جرّب:
   ```bash
   bash run_mac.sh echo     # اتكلم 3 ثواني واسمع المايك بيسمع إيه
   bash run_mac.sh say      # صوت الروبوت من السيرفر
   bash run_mac.sh bench    # جملة واحدة على كل موديل: السرعة والدقة
   bash run_mac.sh          # الروبوت
   ```
   أول تشغيل بيحمّل موديل Whisper (small حوالي 500MB). والماك هيسألك تسمح للـ Terminal بالمايك: وافق.

⚠️ اقفل الـ ESP32 وانت بتجرّب، وإلا الاتنين هيردوا على «يا موس».

## على الـ mini PC (Ubuntu 24.04)

```bash
cd ~/mouss-brain
bash install_ubuntu.sh
```

بيركّب كل حاجة، وبيخلّي الروبوت يشتغل لوحده أول ما الجهاز يفتح (من غير شاشة):

- اللوج: `journalctl --user -u mouss-brain -f`
- بعد ما تعدّل `brain.ini`: `systemctl --user restart mouss-brain`

## الأوامر

| | |
|---|---|
| `run` | الروبوت (ده اللي بيشتغل لو مكتبتش حاجة) |
| `devices` | المايكات والسماعات |
| `meter` | مستوى المايك 5 ثواني (اسكت ثانيتين وبعدين اتكلم) |
| `echo` | 3 ثواني من المايك بيتشغّلوا في السماعة |
| `say نص` | صوت الروبوت |
| `hear` | جملة واحدة ← الكلام المكتوب (على الجهاز، من غير سيرفر) |
| `bench` | جملة واحدة على `small` و`medium` و`large-v3-turbo` |

كل الإعدادات في `brain.ini` (اتفرّج على `brain.ini.example`). وأهمها
`model`: ابدأ بـ `small`، ولو `bench` قال إن `medium` أو `large-v3-turbo` سريعين على جهازك، استخدمهم لأنهم أدق في اللهجة المصرية.
