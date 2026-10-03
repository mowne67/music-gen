#!/usr/bin/env python3
"""Join finished releases into one long video (audio crossfaded, pictures spliced without re-encoding).

Usage: .venv-flux/bin/python join.py NAME part1 part2 ... --title "..." --blurb "..." --hashtags "#a #b" --tags "a, b"
Add --fresh to replace an existing release; otherwise the same inputs resume.
Output: releases/NAME/{video.mp4, thumbnail.png, youtube.md, captions.srt, manifest.json}
"""
import argparse, contextlib, json, re, shutil
from pathlib import Path
from make_video import ROOT, TARGET_LUFS, duration, loudness, run, stamp
from pipeline import checkpoint, checkpoint_valid, load_state, matches, media_valid, now, record, release_lock, write_json

XF = 6  # crossfade seconds between parts


def part_sources(name):
    out = ROOT / "releases" / name
    manifest = out / "manifest.json"
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved.get("status") != "complete" or saved.get("kind") != "part":
            raise RuntimeError(f"Part {name} is not a completed music release")
        if not matches(saved["mix"]) or not checkpoint_valid(
                saved, "publish", [ROOT / p for p in saved["final_inputs"]]):
            raise RuntimeError(f"Part {name} has changed since completion; resume it before joining")
        mix = ROOT / saved["mix"]["path"]
        provenance = saved
        dependencies = [manifest]
    else:
        mix = ROOT / "mixes" / f"{name}_mix.wav"
        provenance = {"status": "legacy_unrecorded", "name": name}
        dependencies = []
    video = out / "video.mp4"
    if not media_valid(mix, types=("audio",)) or not media_valid(video, types=("audio", "video")):
        raise RuntimeError(f"Part {name} has an invalid or missing mix/video")
    if abs(duration(mix) - duration(video)) > 0.1:
        raise RuntimeError(f"Part {name}: mix/video durations differ by more than 0.1s; refusing to join")
    return mix, video, provenance, dependencies + [mix, video, out / "thumbnail.png"]


def build_join(a):
    out = ROOT / "releases" / a.name
    out.mkdir(parents=True, exist_ok=True)
    # Hold source locks as well, so another run cannot replace a mix during the join.
    with contextlib.ExitStack() as locks:
        locks.enter_context(release_lock(out))
        for name in sorted(set(a.parts)):
            locks.enter_context(release_lock(ROOT / "releases" / name))
        return locked_join(a, out)


def locked_join(a, out):
    n = len(a.parts)
    sources = [part_sources(p) for p in a.parts]
    mixes, videos = [s[0] for s in sources], [s[1] for s in sources]
    inputs = [p for s in sources for p in s[3]]
    request = {"name": a.name, "parts": a.parts, "title": a.title, "blurb": a.blurb,
               "hashtags": a.hashtags, "tags": a.tags, "crossfade_seconds": XF,
               "target_lufs": TARGET_LUFS, "limiter": 0.708}
    state = load_state(out, request, "joined", a.fresh)
    manifest = out / "manifest.json"
    def save():
        write_json(manifest, state)
    if state["status"] == "complete" and checkpoint_valid(state, "publish", inputs) \
            and media_valid(out / "video.mp4", state["duration_seconds"], ("audio", "video")):
        print(f"✓ resume: {a.name} is already complete", flush=True)
        return
    state["status"] = "running"
    state.pop("error", None)
    state["parts"] = [{"name": p, "manifest": s[2], "mix": record(s[0]), "video": record(s[1])}
                      for p, s in zip(a.parts, sources)]
    save()
    try:
        finish_join(a, out, state, inputs, mixes, videos, save)
    except Exception as exc:
        state["status"], state["error"] = "failed", str(exc)
        save()
        raise


def finish_join(a, out, state, inputs, mixes, videos, save):
    n = len(a.parts)
    lens = [duration(m) for m in mixes]
    if n > 1 and min(lens) <= XF:
        raise RuntimeError(f"Each part must be longer than the {XF}s crossfade")
    gains = [TARGET_LUFS - loudness(m) for m in mixes]
    total = sum(lens) - XF * (n - 1)

    # audio: match loudness, crossfade parts, fade out the end
    filt = "".join(f"[{i}]volume={g:.2f}dB[a{i}];" for i, g in enumerate(gains))
    prev = "a0"
    for i in range(1, n):
        filt += f"[{prev}][a{i}]acrossfade=d={XF}:c1=tri:c2=tri[x{i}];"; prev = f"x{i}"
    # Leave headroom for inter-sample peaks introduced by AAC encoding.
    filt += f"[{prev}]alimiter=limit=0.708:level=false,afade=t=out:st={max(0, total - 6):.3f}:d=6[out]"
    audio = out / "audio.wav"
    if not checkpoint_valid(state, "audio", mixes):
        tmp = audio.with_suffix(".tmp.wav")
        run("ffmpeg", "-y", *[x for m in mixes for x in ("-i", m)], "-filter_complex", filt, "-map", "[out]", tmp)
        tmp.replace(audio)
        checkpoint(state, "audio", mixes, [audio])
        save()

    # picture: cut each part's video at the middle of its crossfade, then splice (no re-encode)
    segs = [lens[i] - (XF / 2 if i in (0, n - 1) else XF) if n > 1 else lens[i] for i in range(n)]
    starts = [sum(segs[:i]) for i in range(n)]  # chapter i begins here (video cut = middle of crossfade)
    listing = out / "segs.txt"
    if not checkpoint_valid(state, "mux", inputs):
        quoted = [str(p).replace("'", "'\\''") for p in videos]
        listing.write_text("".join(f"file '{p}'\noutpoint {s:.3f}\n" for p, s in zip(quoted, segs)))
        tmp = out / "video.tmp.mp4"
        run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing, "-i", audio, "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", "-t", f"{total:.3f}", "-movflags", "+faststart", tmp)
        if not media_valid(tmp, total, ("audio", "video")):
            raise RuntimeError(f"Invalid joined video: {tmp}")
        tmp.replace(out / "video.mp4")
        checkpoint(state, "mux", inputs, [out / "video.mp4"])
        save()
    shutil.copy(ROOT / "releases" / a.parts[0] / "thumbnail.png", out / "thumbnail.png")

    names = [p.replace("_", " ").title() for p in a.parts]
    bounds = starts + [total]
    (out / "captions.srt").write_text("".join(
        f"{i}\n{stamp(bounds[i - 1], True)} --> {stamp(bounds[i], True)}\n[♪ {nm}: instrumental music, no vocals ♪]\n\n"
        for i, nm in enumerate(names, 1)))
    chapters = "\n".join(f"{stamp(t)} {nm}" for t, nm in zip(starts, names))
    (out / "youtube.md").write_text(f"""# Title

{a.title}

# Description

{a.blurb}

🎧 Chapters
{chapters}

☕ Put it on in the background and let the hours go by.
🔔 Subscribe for more.

All music is original and generated locally with ACE-Step 1.5. Artwork generated locally with FLUX.1 schnell.

{a.hashtags}

# Tags (YouTube Studio → Show more → Tags)

{a.tags}
""")
    state["duration_seconds"], state["chapters"] = total, starts
    checkpoint(state, "publish", inputs, [out / f for f in ("video.mp4", "thumbnail.png", "youtube.md", "captions.srt")])
    state["status"], state["completed_at"] = "complete", now()
    save()
    listing.unlink(missing_ok=True); audio.unlink(missing_ok=True)
    print(f"✓ releases/{a.name}/  video.mp4 ({stamp(total)}), chapters at {', '.join(stamp(t) for t in starts)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name"); ap.add_argument("parts", nargs="+")
    ap.add_argument("--title", required=True); ap.add_argument("--blurb", required=True)
    ap.add_argument("--hashtags", required=True); ap.add_argument("--tags", required=True)
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    if not all(re.fullmatch(r"[a-z0-9_]+", name) for name in [a.name] + a.parts):
        ap.error("release names must be lowercase_with_underscores")
    if a.name in a.parts:
        ap.error("a release cannot be joined into itself")
    build_join(a)


if __name__ == "__main__":
    main()
