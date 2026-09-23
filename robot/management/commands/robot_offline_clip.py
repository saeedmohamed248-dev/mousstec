"""
robot_offline_clip — make the WAV the robot plays when the internet is down.

Speech-to-text and text-to-speech run on the server, so an offline robot can't
answer. The bridge ESP32 plays `/offline.wav` from its SD card instead; this
writes that file (16 kHz mono 16-bit, what the amp plays):

    python manage.py robot_offline_clip --out offline.wav
    # then copy offline.wav to the root of the robot's microSD card
"""

from django.core.management.base import BaseCommand, CommandError

DEFAULT_TEXT = "النت فاصل دلوقتي ومش هقدر أرد. اسأل حد من الموظفين لو سمحت، وأنا راجع على طول."


class Command(BaseCommand):
    help = "Write the offline voice clip (WAV) for the robot's SD card."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="offline.wav")
        parser.add_argument("--text", default=DEFAULT_TEXT)

    def handle(self, *args, **opts):
        from robot import audio

        wav = audio.synthesize_wav(opts["text"])
        if not wav:
            raise CommandError("TTS غير متاح: لازم gTTS + ffmpeg + إنترنت.")
        with open(opts["out"], "wb") as fh:
            fh.write(wav)
        self.stdout.write(self.style.SUCCESS(
            f"✅ اتكتب {opts['out']} ({len(wav) // 1024} KB) — انسخه لكارت SD باسم offline.wav"
        ))
