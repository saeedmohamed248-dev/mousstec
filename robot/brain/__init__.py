"""«مخ موس» — the robot's brain on a real computer (a mini PC, or a Mac to try it).

The ESP32 bridge sent every question's audio to the server, where Gemini
turned it into words: 3.5–4.8 s of the 5–11 s before each answer, and a
cut-off mic or a slow line made it deaf. Here a USB mic, speakers and the
computer do the listening themselves (Whisper, on the machine): only the
words go to the server, whose own brain (stock, retail prices, Egyptian
numbers) answers in ~10 ms and sends back the spoken reply.

Self-contained on purpose — no Django, nothing else from the ERP — so this
folder alone is what a robot needs (see README.md).
"""

VERSION = "brain-0.9.9"
