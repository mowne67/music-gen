#!/usr/bin/env python3
"""Turn a still into a seamless pixel-art loop (40 seconds by default).

Usage:  .venv-flux/bin/python animate.py art/monsoon_window_2.png rain flicker
        [--seconds 40] [--seed 7] [--output-dir video]
Effects: rain, flicker, fireflies, sweep (passing light), grain
         rain=x0,y0,x1,y1 limits rain to a box, as fractions of the frame (e.g. a window)
Output: video/<stem>_loop.mp4 (1920x1080) + video/<stem>_thumb.png (pixelated still, for thumbnails)

Every effect repeats exactly once (or a whole number of times) per LOOP seconds,
so frame 0 follows the last frame with no visible seam.
"""
import argparse, subprocess, tempfile
from pathlib import Path
import numpy as np
from PIL import Image
from pipeline import write_json

W, H, SCALE = 480, 270, 4   # draw at 480x270, upscale 4x with nearest -> crisp 1920x1080
FPS, LOOP = 24, 40


def pixelate(path):
    img = Image.open(path).convert("RGB").resize((W, H), Image.LANCZOS)
    return np.asarray(img.quantize(64, dither=Image.Dither.NONE).convert("RGB"), dtype=np.float32) / 255


def rain_layer(drops, alpha, rng):
    layer = np.zeros((H, W), np.float32)
    for x, y, length in zip(rng.integers(0, W, drops), rng.integers(0, H, drops), rng.integers(3, 8, drops)):
        layer[(y + np.arange(length)) % H, x] = alpha  # wraps vertically, so the layer tiles
    return layer


def light_sweeps(seconds, seed=7):
    """Five varied events on a frame-aligned, circular timeline."""
    rng = np.random.default_rng(seed + 11)
    count = 5
    n = round(seconds * FPS)
    return [{"start_frame": round((i + rng.uniform(0.1, 0.8)) * n / count) % n,
             "duration_frames": max(2, round(rng.uniform(0.06, 0.14) * n)),
             "width": float(rng.uniform(25, 65)), "brightness": float(rng.uniform(0.05, 0.13))}
            for i in range(count)]


def frames(base, effects, seconds=LOOP, seed=7, start_frame=0, frame_count=None):
    n = round(FPS * seconds)
    if n <= 0:
        raise ValueError("Loop duration must be positive")
    rng = np.random.default_rng(seed)
    lum = base @ np.array([0.299, 0.587, 0.114], np.float32)
    glow = np.clip((lum - 0.55) / 0.3, 0, 1)[..., None]  # only bright areas (lamps, screens) flicker
    far, near = rain_layer(700, 0.3, rng), rain_layer(250, 0.5, rng)
    box = np.zeros((H, W, 1), np.float32)
    x0, y0, x1, y1 = effects.get("rain") or (0, 0, 1, 1)
    box[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)] = 1
    rain_rgb = np.array([0.8, 0.85, 0.95], np.float32)
    flies = rng.uniform([0, H * 0.3, 4, 0], [W, H, 14, 2 * np.pi], (14, 4))  # cx, cy, radius, phase
    xs = np.arange(W, dtype=np.float32)
    sweeps = light_sweeps(seconds, seed)
    for f in range(start_frame, start_frame + (n if frame_count is None else frame_count)):
        f %= n
        t = f / n  # 0..1 over one loop
        img = base.copy()
        if "flicker" in effects:
            k = (0.025 * np.sin(2 * np.pi * round(seconds / 5) * t)
                 + 0.012 * np.sin(2 * np.pi * round(seconds / 1.7) * t + 0.4))
            img *= 1 + glow * k
        if "sweep" in effects:
            for event in sweeps:
                elapsed = (f - event["start_frame"]) % n
                if elapsed < event["duration_frames"]:
                    u = elapsed / event["duration_frames"]
                    xc = -100 + (W + 200) * u
                    strength = event["brightness"] * np.sin(np.pi * u) ** 2
                    img += (strength * np.exp(-((xs - xc) / event["width"]) ** 2))[None, :, None] \
                        * np.array([1, 0.8, 0.5], np.float32)
        if "rain" in effects:
            far_shift = round(H * (max(1, round(seconds / 2)) * t + 0.2 * np.sin(2 * np.pi * t)))
            near_shift = round(H * (max(1, round(seconds * 0.75)) * t + 0.15 * np.sin(4 * np.pi * t)))
            a = np.maximum(np.roll(far, far_shift, 0), np.roll(near, near_shift, 0))[..., None] * box
            img = img * (1 - a) + rain_rgb * a
        if "fireflies" in effects:  # closed orbits with integer frequencies -> back to the start at t=1
            for cx, cy, r, ph in flies:
                x = int(cx + r * np.sin(2 * np.pi * t + ph)) % W
                y = int(np.clip(cy + r * np.cos(2 * np.pi * 2 * t + ph), 0, H - 1))
                b = 0.5 + 0.5 * np.sin(2 * np.pi * t + ph)
                img[y, x] = img[y, x] * (1 - b) + np.array([1, 0.95, 0.6]) * b
        if "grain" in effects:
            # Frame-indexed noise is reproducible even when rendering a subset.
            img += np.random.default_rng(seed + 1000 + f).normal(0, 0.012, (H, W, 1)).astype(np.float32)
        yield (np.clip(img, 0, 1) * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", type=Path); ap.add_argument("effects", nargs="*")
    ap.add_argument("--seconds", type=int, default=LOOP); ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "video")
    args = ap.parse_args()
    if args.seconds <= 0:
        ap.error("--seconds must be positive")
    if args.seed < 0:
        ap.error("--seed must be nonnegative")
    src = args.src
    effects = {k: tuple(map(float, v.split(","))) if v else None
               for k, _, v in (a.partition("=") for a in args.effects)}
    assert effects.keys() <= {"rain", "flicker", "fireflies", "sweep", "grain"}, f"unknown effect in {effects}"
    if effects.get("rain") is not None:
        x0, y0, x1, y1 = effects["rain"]
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            ap.error("rain bounds must be x0,y0,x1,y1 within the frame")
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    base = pixelate(src)
    thumb, dest = out / f"{src.stem}_thumb.png", out / f"{src.stem}_loop.mp4"
    thumb_tmp, tmp = thumb.with_suffix(".tmp.png"), dest.with_suffix(".tmp.mp4")
    Image.fromarray((base * 255).astype(np.uint8)).resize((W * SCALE, H * SCALE), Image.NEAREST).save(thumb_tmp)
    with tempfile.TemporaryFile(mode="w+t") as log:
        ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                           "-vf", f"scale={W * SCALE}:{H * SCALE}:flags=neighbor",
                           "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-bf", "0", "-pix_fmt", "yuv420p",
                           tmp], stdin=subprocess.PIPE, stderr=log)
        try:
            for fr in frames(base, effects, args.seconds, args.seed):
                ff.stdin.write(fr.tobytes())
            ff.stdin.close()
            if ff.wait() != 0:
                log.seek(0)
                raise RuntimeError("Animation FFmpeg failed:\n" + "\n".join(log.read().splitlines()[-20:]))
            tmp.replace(dest)
            thumb_tmp.replace(thumb)
        except BrokenPipeError:
            ff.wait()
            log.seek(0)
            raise RuntimeError("Animation FFmpeg failed:\n" + "\n".join(log.read().splitlines()[-20:])) from None
        finally:
            if ff.poll() is None:
                ff.terminate()
                ff.wait()
            tmp.unlink(missing_ok=True)
            thumb_tmp.unlink(missing_ok=True)
    write_json(dest.with_suffix(".json"), {"source": str(src), "seconds": args.seconds, "fps": FPS,
               "seed": args.seed, "effects": args.effects, "light_sweeps": light_sweeps(args.seconds, args.seed),
               "width": W * SCALE, "height": H * SCALE, "codec": "libx264", "crf": 16, "b_frames": 0})
    print(f"✓ {dest}  ({args.seconds}s, effects: {' '.join(args.effects)})")


if __name__ == "__main__":
    main()
