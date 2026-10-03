#!/usr/bin/env python3
"""One command: prompt -> music takes -> stitched mix -> pixel-art loop -> upload-ready video.

Usage (run with the FLUX venv, it has numpy/PIL):
  .venv-flux/bin/python make_video.py NAME "music prompt" TAKES SECONDS_PER_TAKE
      [--scene "art prompt"]        # defaults to the music prompt
      [--effects rain flicker ...]  # defaults are picked from keywords (rain, fireflies, traffic...)
      [--no-style]                  # skip the channel's lo-fi style (e.g. EDM)
      [--bpm 140]                   # default 72 (lo-fi); trap sits around 140
      [--loop-seconds 40] [--animation-seed 7]
      [--fresh]                     # otherwise resume the same settings
Output: releases/NAME/{video.mp4, thumbnail.png, youtube.md, captions.srt, manifest.json}
"""
import argparse, json, random, re, shutil, subprocess, time, urllib.request
from pathlib import Path
import numpy as np
from PIL import Image
import art, gen
from pipeline import (checkpoint, checkpoint_valid, load_state, media_valid, next_index,
                      now, record, release_lock, run, write_json)

ROOT = Path(__file__).parent
XF, TARGET_LUFS = 4, -14  # XF must match stitch.sh
SILENCE = "areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse"  # same trim as stitch.sh


def server_up():
    try:
        return urllib.request.urlopen(gen.API + "/health", timeout=5).status == 200
    except OSError:
        return False


def ensure_server():
    if server_up():
        return
    print("starting ACE-Step server…", flush=True)
    subprocess.Popen(["./start_api_server_macos.sh"], cwd=ROOT / "ACE-Step-1.5", start_new_session=True,
                     stdout=open("/tmp/acestep-api.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(180):
        time.sleep(5)
        if server_up():
            return
    raise SystemExit("ACE-Step server did not start, see /tmp/acestep-api.log")


def duration(path):
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path).stdout)


def trimmed_duration(path):  # length after stitch.sh's tail-silence trim
    err = run("ffmpeg", "-hide_banner", "-i", path, "-af", SILENCE, "-f", "null", "-").stderr
    h, m, s = re.findall(r"time=(\d+):(\d+):([\d.]+)", err)[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def loudness(path):
    err = run("ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128", "-f", "null", "-").stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])


def letterboxed(path):  # FLUX sometimes paints black cinema bars
    g = np.asarray(Image.open(path).convert("L"), np.float32) / 255
    rows = max(1, len(g) // 60)
    return any(b.mean() < 0.03 and b.std() < 0.03 for b in (g[:rows], g[-rows:]))


def auto_effects(text):
    t, fx = text.lower(), ["flicker", "grain"]
    if re.search(r"rain|monsoon|storm|drizzle", t): fx.append("rain")
    if re.search(r"firefl|insect|cricket", t): fx.append("fireflies")
    if re.search(r"\b(bus|train|drive|driving|car|cars|traffic|highway)\b", t): fx.append("sweep")
    return fx


def stamp(sec, srt=False):
    h, m, s = int(sec // 3600), int(sec % 3600 // 60), sec % 60
    if srt:
        return f"{h:02}:{m:02}:{int(s):02},{int(s % 1 * 1000):03}"
    return f"{h}:{m:02}:{int(s):02}" if h else f"{m}:{int(s):02}"


def metadata(name, prompt, lofi, chapters, total):
    pretty, mins = name.replace("_", " ").title(), round(total / 60)
    rainy = re.search(r"rain|monsoon|storm", prompt.lower())
    vibe = "warm, dusty lo-fi" if lofi else "instrumental music"
    tags = (["#lofi", "#lofihiphop", "#studymusic", "#chillbeats", "#codingmusic", "#lofibeats", "#focusmusic",
             "#beatstostudyto"] if lofi else ["#instrumental", "#music", "#nocopyrightmusic"]) \
        + (["#rainyday", "#rainynight"] if rainy else ["#relaxingmusic"])
    chap = "\n".join(f"{stamp(t)} {pretty}, Part {i}" for i, t in enumerate(chapters, 1))
    return f"""# Title

{pretty} {'🌧️' if rainy else '🌙'} {'lo-fi beats to study, code & relax to' if lofi else 'instrumental mix'} [{mins} min]

# Description

{pretty}: {mins} minute{"s" * (mins != 1)} of {vibe} with no vocals, for studying, coding, reading or winding down.

{prompt}
{f'''
🎧 Chapters
{chap}
''' if chap else ''}
☕ Put it on in the background and let the hours go by.
🔔 Subscribe for more.

All music is original and generated locally with ACE-Step 1.5. Artwork generated locally with FLUX.1 schnell.

{' '.join(tags)}

# Tags (YouTube Studio → Show more → Tags)

{', '.join(t[1:] for t in tags)}, {pretty.lower()}, no vocals, instrumental, background music
"""


def build_release(a):
    out = ROOT / "releases" / a.name
    out.mkdir(parents=True, exist_ok=True)
    gen.DURATION, gen.BPM = a.seconds, a.bpm
    prompt = (a.prompt, a.bpm) if a.no_style else a.prompt
    scene = a.scene or a.prompt
    fx = a.effects or auto_effects(f"{a.prompt} {scene}")
    request = {"name": a.name, "music": gen.request_settings(prompt), "takes": a.takes,
               "scene": scene, "art_style": art.STYLE, "effects": fx,
               "loop_seconds": a.loop_seconds, "animation_seed": a.animation_seed,
               "crossfade_seconds": XF, "target_lufs": TARGET_LUFS, "limiter": 0.708}
    with release_lock(out):
        state = load_state(out, request, "part", a.fresh)
        manifest = out / "manifest.json"
        def save():
            write_json(manifest, state)
        if state["status"] == "complete" and checkpoint_valid(
                state, "publish", [ROOT / p for p in state["final_inputs"]]) \
                and media_valid(out / "video.mp4", state["duration_seconds"], ("audio", "video")):
            print(f"✓ resume: {a.name} is already complete", flush=True)
            return
        state["status"] = "running"
        state.pop("error", None)
        if "takes" not in state:
            start = next_index(gen.OUT, a.name)
            state["takes"] = [{"index": i} for i in range(start, start + a.takes)]
        save()
        try:
            # Each task ID is persisted before polling, and completed WAVs survive a retry.
            for take in state["takes"]:
                dest = gen.OUT / f"{a.name}_{take['index']}.wav"
                if not dest.exists():
                    ensure_server()
                    def save_task(task):
                        take["task"] = task
                        save()
                    gen.generate(a.name, prompt, take["index"], task=take.get("task"),
                                 on_task=save_task, timeout=a.generation_timeout)
                if not media_valid(dest, a.seconds, ("audio",)):
                    raise RuntimeError(f"Invalid saved take: {dest}. Move it aside or use --fresh.")
                item = json.loads(dest.with_suffix(".json").read_text())
                if item.get("request") != request["music"]:
                    raise RuntimeError(f"Saved take settings differ: {dest}. Use --fresh.")
                take.update({"audio": record(dest), "metadata": record(dest.with_suffix(".json")),
                             "seed": item.get("seed_value"), "models": {
                                 "lm": item.get("lm_model"), "dit": item.get("dit_model")},
                             "planned_metadata": item.get("metas", {})})
                save()
            idxs = [t["index"] for t in state["takes"]]
            takes = [gen.OUT / f"{a.name}_{i}.wav" for i in idxs]
            mix = ROOT / "mixes" / f"{a.name}_mix.wav"
            mix.parent.mkdir(exist_ok=True)
            if not checkpoint_valid(state, "mix", takes):
                if len(takes) > 1:
                    run(ROOT / "stitch.sh", a.name, *idxs)
                else:
                    tmp = mix.with_suffix(".tmp.wav")
                    shutil.copyfile(takes[0], tmp)
                    tmp.replace(mix)
                if not media_valid(mix, types=("audio",)):
                    raise RuntimeError(f"Invalid stitched mix: {mix}")
                checkpoint(state, "mix", takes, [mix])
                save()
            total = duration(mix)
            audio = out / "audio.wav"
            if not checkpoint_valid(state, "audio", [mix]):
                gain = TARGET_LUFS - loudness(mix)
                tmp = audio.with_suffix(".tmp.wav")
                run("ffmpeg", "-y", "-i", mix, "-af",
                    f"volume={gain:.2f}dB,alimiter=limit=0.708:level=false,afade=t=out:st={max(0, total - 6):.3f}:d=6", tmp)
                tmp.replace(audio)
                state["mastering_gain_db"] = gain
                checkpoint(state, "audio", [mix], [audio])
                save()
                print(f"✓ mastered {stamp(total)} ({gain:+.1f} dB)", flush=True)
            art.OUT.mkdir(exist_ok=True)
            if "art_candidates" not in state:
                start = next_index(art.OUT, a.name)
                state["art_candidates"] = [{"index": i, "seed": random.randrange(2**31)}
                                           for i in range(start, start + 2)]
                save()
            pending = [c for c in state["art_candidates"] if not (art.OUT / f"{a.name}_{c['index']}.png").exists()]
            if len(pending) == 2:
                art.generate(a.name, scene, pending[0]["index"], 2, seeds=[c["seed"] for c in pending])
            elif pending:
                art.generate(a.name, scene, pending[0]["index"], 1, seeds=[pending[0]["seed"]])
            cands = [art.OUT / f"{a.name}_{c['index']}.png" for c in state["art_candidates"]]
            for cand, planned in zip(cands, state["art_candidates"]):
                with Image.open(cand) as img:
                    img.verify()
                detail = json.loads(cand.with_suffix(".json").read_text())
                if detail["seed"] != planned["seed"] or detail["prompt"] != f"{scene} {art.STYLE}":
                    raise RuntimeError(f"Saved artwork settings differ: {cand}")
                planned.update({"image": record(cand), "settings": detail})
            pick = next((c for c in cands if not letterboxed(c)), cands[0])
            state["art_pick"] = record(pick)
            save()
            loop = ROOT / "video" / f"{pick.stem}_loop.mp4"
            thumb = ROOT / "video" / f"{pick.stem}_thumb.png"
            if not checkpoint_valid(state, "animation", [pick]):
                print(run(ROOT / ".venv-flux/bin/python", ROOT / "animate.py", pick, *fx,
                          "--seconds", a.loop_seconds, "--seed", a.animation_seed).stdout.strip(), flush=True)
                if not media_valid(loop, a.loop_seconds, ("video",)):
                    raise RuntimeError(f"Invalid animation: {loop}")
                checkpoint(state, "animation", [pick], [loop, thumb, loop.with_suffix(".json")])
                save()
            state["animation"] = json.loads(loop.with_suffix(".json").read_text())
            if not checkpoint_valid(state, "mux", [mix, loop]):
                tmp = out / "video.tmp.mp4"
                run("ffmpeg", "-y", "-stream_loop", "-1", "-i", loop, "-i", audio,
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
                    "-t", f"{total:.3f}", "-movflags", "+faststart", tmp)
                if not media_valid(tmp, total, ("audio", "video")):
                    raise RuntimeError(f"Invalid assembled video: {tmp}")
                tmp.replace(out / "video.mp4")
                checkpoint(state, "mux", [mix, loop], [out / "video.mp4"])
                save()
            shutil.copy(thumb, out / "thumbnail.png")
            chapters = [0.0]
            for t in takes[:-1]:
                chapters.append(chapters[-1] + trimmed_duration(t) - XF)
            chapters = chapters if len(chapters) >= 3 else []
            cues = list(zip(chapters or [0.0], (chapters[1:] if chapters else []) + [total]))
            (out / "captions.srt").write_text("".join(
                f"{i}\n{stamp(s, True)} --> {stamp(e, True)}\n[♪ calm instrumental music, no vocals ♪]\n\n"
                for i, (s, e) in enumerate(cues, 1)))
            (out / "youtube.md").write_text(metadata(a.name, a.prompt, not a.no_style, chapters, total))
            inputs = takes + [p.with_suffix(".json") for p in takes] + [mix, pick, pick.with_suffix(".json"), loop, loop.with_suffix(".json")]
            state["final_inputs"] = [record(p)["path"] for p in inputs]
            state["mix"] = record(mix)
            state["duration_seconds"], state["chapters"] = total, chapters
            checkpoint(state, "publish", inputs, [out / n for n in ("video.mp4", "thumbnail.png", "youtube.md", "captions.srt")])
            state["status"], state["completed_at"] = "complete", now()
            save()
            audio.unlink(missing_ok=True)
            print(f"✓ releases/{a.name}/  video.mp4 ({stamp(total)}), manifest.json", flush=True)
        except Exception as exc:
            state["status"], state["error"] = "failed", str(exc)
            save()
            raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name"); ap.add_argument("prompt")
    ap.add_argument("takes", type=int); ap.add_argument("seconds", type=int)
    ap.add_argument("--scene"); ap.add_argument("--effects", nargs="+"); ap.add_argument("--no-style", action="store_true")
    ap.add_argument("--bpm", type=int, default=gen.BPM)
    ap.add_argument("--loop-seconds", type=int, default=40); ap.add_argument("--animation-seed", type=int, default=7)
    ap.add_argument("--generation-timeout", type=int, default=gen.GENERATION_TIMEOUT)
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9_]+", a.name):
        ap.error("NAME must be lowercase_with_underscores")
    if min(a.takes, a.seconds, a.bpm, a.loop_seconds, a.generation_timeout) <= 0:
        ap.error("takes, durations, BPM and timeout must be positive")
    if a.animation_seed < 0:
        ap.error("--animation-seed must be nonnegative")
    build_release(a)


if __name__ == "__main__":
    main()
