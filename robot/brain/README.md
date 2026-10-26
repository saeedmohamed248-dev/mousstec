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

### With the internet down

It keeps the branch's whole catalog on disk (`~/.mouss-brain/catalog.json`:
names, codes, car models, **retail** prices, stock — the server's own
`/sync/pull/` snapshot, refreshed every 15 min — every minute with
`answer_here`, and only sent again when it changed), so from the moment it starts
it knows the stock, net or not. It also has the shop's «🗨️ الردود الجاهزة»
(the hours, the address, the phone, paying, the warranty, delivery — written on
the robot's page, `brain/replies.py`), said at once with the net up or down.
And «🎓 تعلّم السمع»: every question said to the robot goes to the server with
its recording while the net is up (`[robot] teach = yes`, since brain-0.9.0 —
in the shop Vosk was «sure» and wrong: «سلطة الزيت بوش» for «فلتر الزيت بوش»),
where Azure's or Google's speech-to-text hears it in Egyptian Arabic
(`robot/teachers.py`, within each one's minutes); what they taught comes back
with the catalog (`heard_fixes`, `brain/heardfix.py`) and is used offline too.
`teach = unsure` sends only what Vosk wasn't sure of (its words' confidence,
or nothing here makes sense of them); `teach = no` never sends a recording.
When the server takes longer than 3.5 s (the AI thinking) the robot says
«ثانية واحدة، بشوفلك» from its voice bank meanwhile; a server it can't reach
is known in 5 s, not 20, and a conversation the server had goes on here.
A question it can't send is answered here:
the server's matching and sentences, ported line for line
(`brain/knowledge.py`) — `robot/tests/test_brain_offline_db.py` asks both the
same questions and expects the same words. The voice is the same too: while
online it fetches, a clip a second, every piece an offline answer can need
(each part's name, each price, the fixed phrases) from `/speak/` as µ-law and
keeps them (`~/.mouss-brain/voice/clips/`); offline, an answer is those clips
one after the other. It remembers the conversation the way the server does
(`brain/talk.py`, the server's own file): «التانية» or «E90» answers «تقصد
أنهي؟», and «وبكام؟», «مكانه فين؟», «عليه ضمان؟», «ماركته إيه؟», «بيركب على
E91؟», «فيه أرخص؟» are about the part just named — from the catalog's
shelf, warranty, brand and models. A part that's out gets «أبلغك أول ما ييجي؟»
and the number said next waits in `~/.mouss-brain/outbox.jsonl` until the
net is back (`brain/outbox.py`, `/sync/push/`). What the server queues for it
to say — the greeting at clock-in, a page, a scan's answer — it says.

**The counter page** (`counter.py`, `[counter] port = 8765`): open
`http://<this computer's address>:8765` on a phone on the shop's Wi-Fi. Each
employee logs in with the PIN a manager set on the robot's page, and — as
their role allows — records a cash sale, an expense or a count, net or not;
it all reaches the server when the net is back. See robot/README.md, round 17. What only the server does (turning the
head to the shelf, other branches, the AI for general questions,
stock-takes, English) waits for the net.

```
[NET] server unreachable: …
[OFFLINE] 🔊 أيوه، فلتر زيت متوفر (3 قطعة) بسعر 450 جنيه و50 قرش.
```

### The shop's sound and its day

`[media]` in brain.ini points at a folder of mp3/m4a files for the Quran and
one for calm music (or a radio stream URL); `[day]` and `[routine …]` say
when: the Quran from opening for two hours, a word half an hour before
closing, anything else the owner writes in. A manager or the owner can also
say «يا موس، شغّل قرآن / موسيقى هادية / وقّف الصوت / علّي الصوت» — the
server checks the face and the role first. The robot never talks over the
Quran: it pauses it, answers, and goes on from where it stopped; music is
only turned down under its voice. Stopped by hand, the schedule doesn't start
it again that day; after a restart inside a window, what it had started
starts again. `python -m brain day` prints today's schedule, `play quran`
tries the files.

Self-contained: numpy, requests, sounddevice, faster-whisper (MIT — fine to
ship). Tests: `robot/tests/test_brain.py`, `test_brain_offline.py`,
`test_brain_offline_db.py`, `test_brain_talk.py`, `test_memory_db.py`, `test_restock_db.py`, `test_brain_counter.py`, `test_offline_ledger_db.py`.

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

## على كمبيوتر ويندوز (mini PC أو PC)

1. نزّل **Python install manager** من <https://www.python.org/downloads/> وسطّبه، وبعدين افتح **Command Prompt**
   (`Win`+`R` ← اكتب `cmd` ← Enter: شباك أسود أوله `C:\Users\...>`) واكتب `pymanager install 3.12-64`.
   لازم **64-bit**: موديل الكلام مالوش نسخة 32-bit. `run_windows.bat` بيدوّر على الـ 64-bit لوحده، ولو مالقاهاش بيقولك.
   (`pymanager` مش `py`: لو فيه Python قديمة على الجهاز، `py` بيبقى بتاعها ومبيعرفش `install`.)
2. فك `mouss-brain.zip` في فولدر ثابت (مثلاً `C:\mouss-brain`)، وفيه `brain.ini` بعنوان ورشتك والتوكن.
3. دوس دبل كليك على **`run_windows.bat`**. أول مرة بيجهّز البرامج (كام دقيقة) وبيحمّل موديل Whisper (حوالي 500MB)، وبعدين الروبوت بيشتغل.
   لو ويندوز سألك عن الـ Firewall، اختار **Private networks** علشان صفحة الكاشير تفتح من الموبايل.
4. الأوامر نفسها من الـ Command Prompt (مش شباك Python اللي أوله `>>>`؛ ده بتقفله بـ `exit()`)، بعد `cd /d C:\mouss-brain`:
   `run_windows.bat devices` · `run_windows.bat echo` · `run_windows.bat say` · `run_windows.bat bench`.
5. علشان يشتغل لوحده أول ما الجهاز يفتح: `run_windows.bat autostart` (بيحط اختصار في Startup). وخلّي ويندوز
   يدخل لوحده من غير باسورد، و**اقفل الـ Sleep** (Settings ← Power ← Never).
6. لو ظهر `Could not find module ...ctranslate2.dll`: ناقص **Microsoft Visual C++ Redistributable**؛ نزّله
   من <https://aka.ms/vs/17/release/vc_redist.x64.exe> وسطّبه. لحد ما يتسطّب السيرفر هو اللي بيسمع (محتاج نت).
   والجهاز البطيء (زي Celeron بـ 2 core) بيعرف لوحده: لو جملة التجربة أخدت أكتر من 3 ثواني، السيرفر بيسمع
   طول ما النت شغال، وWhisper اللي على الجهاز بيسمع بس لما النت يفصل (`[ears] use = auto`؛ `always` أو `offline`
   لو عايز تحدد بنفسك، و`model = off` علشان مايحمّلش Whisper خالص).

## على الـ mini PC (Ubuntu 24.04)

```bash
cd ~/mouss-brain
bash install_ubuntu.sh
```

بيركّب كل حاجة، وبيخلّي الروبوت يشتغل لوحده أول ما الجهاز يفتح (من غير شاشة)،
وبيدّي الروبوت صلاحية المايك والسماعة من غير ما حد يعمل login، وبيقفل الـ Sleep.
بعدها اعمل restart مرة. ويُفضّل كمان:

- **Settings ← System ← Users ← Automatic Login: On** — الصوت بيبقى جاهز أول ما الجهاز يفتح.
- في الـ BIOS: **Restore on AC Power Loss = Power On** — بعد ما الكهربا تقطع وترجع، الجهاز يفتح لوحده.

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
| `wake` | تعليم «يا موس» والأوامر: 32 صوت عربي + أصوات المحل (دقيقتين تسجيل، وأول مرة حوالي 15 دقيقة تعلّم). `wake voices` من غير مايك، و`wake forget` بيمسح |

### الرد من الجهاز على طول (`[robot] answer_here = yes`)
أسئلة المخزون والأسعار («عندك فلتر زيت؟»، «بكام؟») بيرد عليها من كتالوج الجهاز نفسه **في أقل من عُشر ثانية** بدل
1.2–3 ثواني رايح جاي للسيرفر؛ الكتالوج بيتحدّث كل دقيقة، والسيرفر بيتسأل في نفس الوقت (كلام بس من غير صوت) —
بيسجّل المحادثة وبيمسك الخيط، ولو قال حاجة مختلفة (سعر تاني، أو القطعة اتباعت من دقيقة) الروبوت بيصحّح على طول
«معلش، …». البيع والمصروفات والأوامر والفروع التانية والقطع اللي الجهاز ميعرفهاش — للسيرفر زي ما هي، وكمان الرد
اللي بعد سؤال («تقصد أنهي؟»، «قولي رقم موبايلك»).

### «يا موس» بموديل مخصوص (`wake`)
زي «Alexa» و«Hey Siri» بالظبط: موديل صغير شغلته الوحيدة إنه يعرف إذا الاسم اتقال في أول الجملة، من غير نت ومن غير جوجل،
وعلى أي معالج (numpy بس، من غير AVX). Vosk مفيهوش كلمة «موس» خالص، فبيكتبها «يمس» أو «انظر إلى كابوس»؛ الموديل ده
بيسمع الاسم نفسه، والمخ بيحط «يا موس» مكان الكلام اللي Vosk كتبه.

بيتعلّم من **32 صوت عربي** (رجالة وستات، مصري وخليجي وشامي ومغاربي — أصوات Microsoft نفسها اللي الروبوت بيتكلم بيها)،
كل صوت بسرعة وطبقة مختلفة، **ومن أصوات المحل نفسه**. الأصوات بتتعمل مرة واحدة بالنت (`voices.py`) وبتتحفظ في
`%USERPROFILE%\.mouss-brain\wake\voices`؛ بعدها الروبوت مش محتاجها ولا محتاج نت.

`run_windows.bat wake` — خمس خطوات قريب من المايك (دقيقتين)، وبعدها الكمبيوتر يكمّل لوحده:
1. «يا موس» لوحدها 10 مرات
2. نادي اللي في المحل بأساميهم 6 مرات («يا محمد»، «يا أحمد»…) — عشان ميصحاش على أي «يا + اسم»
3. كلمات قريبة منه (فانوس، كابوس، موز، فلوس…) كل واحدة لوحدها
4. اتكلم 20 ثانية في أي حاجة غير الاسم
5. 10 ثواني من صوت المحل من غير كلام

في الآخر بيختبر نفسه على أصوات **مسمعهاش** (كل رابع صوت) وتلت تسجيلات المحل، ويقول لقى الاسم كام مرة وصحي غلط
كام مرة؛ ولو مش كويس كفاية مبيستخدموش. وبيتعلّم كمان **الأوامر بصوتها**: «شغل القرآن»، «شغل مزيكا هادية»،
«وقف الصوت»، «علي الصوت»، «وطي الصوت» — لما Vosk يكتبها غلط («وطّي» مش من كلماته)، المخ بيبعتها للسيرفر صح،
والسيرفر لسه هو اللي بيقرر مين مسموحله (الوش والصلاحية). أول مرة بتاخد حوالي ربع ساعة على الـ Celeron، وبعد كده
أسرع (البصمات بتتحفظ).
الجزء الثابت (`models/wake_features.npz`) من [openWakeWord](https://github.com/dscripka/openWakeWord) (Apache 2.0 —
اتفرّج على `models/NOTICE`).

كل الإعدادات في `brain.ini` (اتفرّج على `brain.ini.example`). وأهمها
`model`: ابدأ بـ `small`، ولو `bench` قال إن `medium` أو `large-v3-turbo` سريعين على جهازك، استخدمهم لأنهم أدق في اللهجة المصرية.
