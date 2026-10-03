#!/usr/bin/env python3
"""Generate pixel-art stills with local FLUX.1 schnell (mflux).

Usage:  .venv-flux/bin/python art.py [candidates_per_scene] [scene ...]
Output: art/<scene>_<n>.png  (seed is in the filename's .txt sidecar)
"""
import random, sys, tempfile
from pathlib import Path
from pipeline import next_index, run, write_json

ROOT = Path(__file__).parent
OUT, MODEL = ROOT / "art", ROOT / "models/flux-schnell-4bit"
MFLUX = ROOT / ".venv-flux/bin/mflux-generate"

# The channel's shared look. Every scene gets this appended.
STYLE = ("16-bit pixel art, detailed retro game scene, crisp pixels, lo-fi aesthetic, cozy and calm, "
         "soft muted palette, no people, no text")

SCENES = {  # keys match gen.py TRACKS
    "monsoon_window": "Cozy bedroom window at night, heavy monsoon rain streaking down the glass, warm desk lamp glow, "
                      "rhodes keyboard on a wooden desk, potted plants, blurred city lights outside, blue and amber tones.",
    "217_am": "Dark bedroom desk at 2am, glowing monitor, electric guitar leaning on the wall, moonlight through "
              "window blinds, deep purple and teal tones, quiet and lonely.",
    "last_bus_home": "Inside an empty city bus at night, rows of seats, rain-streaked windows, passing streetlights and "
                     "neon signs outside, warm interior lights, nostalgic, blue and orange tones.",
    "terrace_after_rain": "Rooftop terrace at night after rain, wet tiles reflecting lights, potted plants, string "
                          "lights, city skyline, clearing clouds, stars, calm and hopeful, teal and warm yellow tones.",
    "tea_shop_closing": "Small Indian tea stall at dusk, steaming kettle, glass tea cups on the counter, warm hanging "
                        "bulb, quiet street outside, cozy, amber and green tones.",
}


def generate(name, prompt, start, n, seeds=None):  # -> list of new image paths
    seeds = seeds if seeds is not None else [random.randrange(2**31) for _ in range(n)]
    if len(seeds) != n:
        raise ValueError("One seed is required for each artwork candidate")
    OUT.mkdir(parents=True, exist_ok=True)
    dests = [OUT / f"{name}_{idx}.png" for idx in range(start, start + n)]
    if any(p.exists() for p in dests):
        raise FileExistsError("Refusing to overwrite an existing artwork candidate")
    # one call with many seeds loads the model once (~1 min) instead of per image
    with tempfile.TemporaryDirectory(prefix="_batch_", dir=OUT) as folder:
        tmp = Path(folder)
        run(MFLUX, "--model", MODEL, "--base-model", "schnell", "--steps", "4",
            "--width", "1024", "--height", "576", "--seed", *map(str, seeds),
            "--prompt", f"{prompt} {STYLE}", "--output", tmp / "img.png")
        for dest, seed in zip(dests, seeds):
            (src,) = tmp.glob(f"*{seed}*.png") if n > 1 else [tmp / "img.png"]
            write_json(dest.with_suffix(".json"), {
                "seed": seed, "prompt": f"{prompt} {STYLE}", "model": str(MODEL.relative_to(ROOT)),
                "base_model": "schnell", "steps": 4, "width": 1024, "height": 576,
            })
            src.rename(dest)
            dest.with_suffix(".txt").write_text(f"seed={seed}\n")
            print(f"✓ {dest.name}  seed={seed}", flush=True)
    return dests


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    names = sys.argv[2:] or list(SCENES)
    OUT.mkdir(exist_ok=True)
    for name in names:
        generate(name, SCENES[name], next_index(OUT, name), n)
