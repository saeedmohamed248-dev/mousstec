"""python -m brain [run | devices | meter | echo | say <text> | hear | bench | play <kind> | day]

  run       the robot (the default)
  devices   the mics and speakers this computer sees (for brain.ini)
  meter     5 s of mic levels: stay quiet 2 s, then talk
  echo      3 s from the mic played back: hear what the robot hears
  say TEXT  the robot's voice, from the server
  hear      one sentence → the words, on this computer (no server)
  bench     one sentence through each Whisper size: how fast, how right
  play KIND the Quran or calm music from [media] (quran | calm), until Ctrl+C
  day       today's schedule from [day] / [routine …]
"""

import argparse
import sys
import time

import numpy as np

from . import VERSION, wavs
from .config import load
from .vad import FRAME, Segmenter


def _ears(cfg, model=None, compute=None):
    from .ears import OFF, Ears
    from .vosk_ears import VoskEars, is_vosk
    model = model or cfg.model
    if is_vosk(model):                   # an old CPU (no AVX2): Kaldi, not Whisper
        ears = VoskEars(model, home=cfg.home)
        print(f"[EARS] loading Vosk {ears.model_name} (the first time it's downloaded)…", flush=True)
        t0 = time.monotonic()
        loaded = ears.load(log=lambda line: print(line, flush=True))
    else:
        ears = Ears(model, device=cfg.device, compute_type=compute or cfg.compute_type,
                    language=cfg.language, beam_size=cfg.beam_size)
        if ears.model_name.strip().lower() not in OFF:
            print(f"[EARS] loading Whisper {ears.model_name} (the first time it's downloaded)…", flush=True)
        t0 = time.monotonic()
        loaded = ears.load()
    if loaded:
        print(f"[EARS] ready in {time.monotonic() - t0:.1f} s", flush=True)
    else:
        print(f"[EARS] can't load it: {ears.error}", flush=True)
    return ears


def _one_sentence(cfg, mic):
    seg = Segmenter(min_start=cfg.min_start, max_start=cfg.max_start,
                    end_silence_ms=cfg.end_silence_ms)
    print(f"[MIC] say one sentence into «{mic.name}»…", flush=True)
    for frame in mic.frames():
        utt = seg.feed(frame)
        if utt is not None:
            print(f"[MIC] got {utt.speech_ms} ms (level {utt.quietest}–{utt.loudest})", flush=True)
            if utt.loudest < 1000:
                # A whisper to the ears, whichever model: no model hears much in it.
                print("[MIC] that's very quiet (a voice is usually 1000+): talk closer to the mic, "
                      "or raise it in Windows sound settings", flush=True)
            return utt


def cmd_meter(cfg):
    from .sound import Mic
    with Mic(cfg.mic) as mic:
        print(f"[MIC] «{mic.name}» at {mic.rate} Hz. 5 s: stay quiet 2 s, then talk.", flush=True)
        levels, frames = [], mic.frames()
        for i in range(25):
            chunk = np.concatenate([next(frames) for _ in range(6)])
            level = wavs.rms(chunk)
            levels.append(level)
            print(f"[MIC] {level:6d} {'#' * min(40, level // 100)}", flush=True)
    quiet, loud = min(levels[:10]), max(levels[10:])
    print(f"[MIC] quiet {quiet}, talking up to {loud}: "
          + ("good" if loud > max(4 * quiet, cfg.min_start) else
             "your voice barely rises above the room — closer to the mic, or another mic"))


def cmd_echo(cfg):
    from .sound import Mic, Speaker
    speaker = Speaker(cfg.speaker, cfg.volume)
    with Mic(cfg.mic) as mic:
        print("[MIC] echo: talk for 3 s…", flush=True)
        frames = mic.frames()
        samples = np.concatenate([next(frames) for _ in range(3 * wavs.RATE // FRAME)])
    peak = max(1, int(np.abs(samples.astype(np.int32)).max()))
    gain = min(8.0, max(1.0, 24000 / peak))
    print(f"[MIC] level {wavs.rms(samples)}, peak {peak}; playing it back"
          + (f" ×{gain:.1f}" if gain > 1.05 else ""), flush=True)
    speaker.play(np.clip(samples * gain, -32768, 32767).astype(np.int16), wavs.RATE)


def _server(cfg):
    from .server import Server
    missing = cfg.missing()
    if missing:
        sys.exit(f"brain.ini needs {', '.join(missing)} under [server] (read: {cfg.source or 'none found'})")
    return Server(cfg.api_base, cfg.token)


def cmd_say(cfg, text):
    from .sound import Speaker
    wav = _server(cfg).speak(text)
    if not wav:
        sys.exit("[VOICE] the server gave no voice for that")
    Speaker(cfg.speaker, cfg.volume).play_wav(wav)


def cmd_hear(cfg):
    from .sound import Mic
    ears = _ears(cfg)
    if not ears.ready:
        sys.exit(1)
    with Mic(cfg.mic) as mic:
        utt = _one_sentence(cfg, mic)
    text, took = ears.timed(utt.samples, f"يا {cfg.name}")
    print(f'[EARS] "{text}" in {took:.1f} s')


def cmd_bench(cfg, models, computes=()):
    """One sentence through each model (× each compute type: on an old CPU
    with no AVX2, int8 can be the slow one and float32 the fast one)."""
    from .sound import Mic
    with Mic(cfg.mic) as mic:
        utt = _one_sentence(cfg, mic)
    seconds = len(utt.samples) / wavs.RATE
    computes = list(computes) or [cfg.compute_type]
    print(f"[BENCH] {seconds:.1f} s of speech, on each model:")
    from .vosk_ears import is_vosk
    for model in models:
        for compute in (computes[:1] if is_vosk(model) else computes):
            ears = _ears(cfg, model, compute)
            if not ears.ready:
                continue
            ears.timed(utt.samples, f"يا {cfg.name}")       # the first clip warms it up: not counted
            text, took = ears.timed(utt.samples, f"يا {cfg.name}")
            several = len(computes) > 1 and not is_vosk(model)
            label = f"{model}/{compute}" if several else model
            print(f'[BENCH] {label:22s} {took:5.1f} s  ({took / seconds:.2f}× the speech)  "{text}"', flush=True)
            if len(computes) > 1:
                print(f"[BENCH]   → brain.ini [ears]: model = {model}"
                      + (f", compute_type = {compute}" if several else ""), flush=True)


def _media(cfg):
    if not cfg.media:
        return None
    from .media import MediaPlayer
    from .sound import pick
    return MediaPlayer(cfg.media, volume=cfg.media_volume, device=pick(cfg.speaker, "output"),
                       log=lambda line: print(line, flush=True))


def _routines(cfg):
    if cfg.ini is None or not any(s.lower().startswith("routine") for s in cfg.ini.sections()):
        return None
    from .routines import Routines
    return Routines.from_ini(cfg.ini, state_path=cfg.home / "day.json",
                             log=lambda line: print(line, flush=True))


def cmd_play(cfg, kind):
    player = _media(cfg)
    if player is None or not player.play(kind):
        sys.exit(f"[MEDIA] set [media] {kind} = a folder of mp3 files (or {kind}_url = a stream) in brain.ini")
    print("[MEDIA] Ctrl+C to stop", flush=True)
    try:
        while player.kind:
            time.sleep(0.5)
    finally:
        player.stop()


def cmd_day(cfg):
    day = _routines(cfg)
    if day is None:
        sys.exit("[DAY] no [routine …] sections in brain.ini (see brain.ini.example)")
    for r in day.routines:
        start = f"{r.start // 60:02d}:{r.start % 60:02d}"
        if r.end is None:
            print(f"[DAY] {start}         {r.say or 'play ' + r.play}   ({r.name})")
        else:
            print(f"[DAY] {start}–{r.end // 60:02d}:{r.end % 60:02d}   play {r.play}   ({r.name})")
    print("[DAY] every day" if day.days is None else f"[DAY] days: {sorted(day.days)} (0 = Monday)")


def cmd_run(cfg):
    from .loop import Brain
    from .server import Server
    from .sound import Mic, Speaker
    server = _server(cfg)
    ears = _ears(cfg)
    speaker = Speaker(cfg.speaker, cfg.volume)
    print(f"[BOOT] Mouss Tec {VERSION} — config {cfg.source or 'from the environment'}", flush=True)
    with Mic(cfg.mic) as mic:
        Brain(cfg, ears=ears, server=server, mic=mic, speaker=speaker,
              monitor=Server(cfg.api_base, cfg.token), media=_media(cfg), routines=_routines(cfg),
              log=lambda line: print(line, flush=True)).run()


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m brain", description="Mouss Tec robot brain")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "devices", "meter", "echo", "say", "hear", "bench", "play", "day"])
    p.add_argument("text", nargs="*", help="for say: what to say")
    p.add_argument("--config", help="brain.ini to use")
    p.add_argument("--models", default="small,medium,large-v3-turbo", help="for bench")
    p.add_argument("--compute", default="", help="for bench: e.g. int8,float32,int16")
    args = p.parse_args(argv)
    cfg = load(args.config)
    try:
        if args.command == "devices":
            from .sound import devices
            print(devices())
        elif args.command == "meter":
            cmd_meter(cfg)
        elif args.command == "echo":
            cmd_echo(cfg)
        elif args.command == "say":
            cmd_say(cfg, " ".join(args.text) or f"أهلاً، أنا {cfg.name}")
        elif args.command == "hear":
            cmd_hear(cfg)
        elif args.command == "bench":
            cmd_bench(cfg, [m.strip() for m in args.models.split(",") if m.strip()],
                      [c.strip() for c in args.compute.split(",") if c.strip()])
        elif args.command == "play":
            cmd_play(cfg, (args.text or ["quran"])[0])
        elif args.command == "day":
            cmd_day(cfg)
        else:
            cmd_run(cfg)
    except KeyboardInterrupt:
        print("\n[BOOT] stopped")


if __name__ == "__main__":
    main()
