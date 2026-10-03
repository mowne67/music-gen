"""Run the nine-take monsoon train release using the existing local pipeline."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv-flux/bin/python"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fresh", action="store_true", help="Generate new takes rather than resuming")
    args = ap.parse_args()
    config = json.loads((ROOT / "monsoon_train.json").read_text())
    for part in config["parts"]:
        print(f"START {part['name']}: {config['takes_per_prompt']} takes x {config['seconds_per_take']} seconds", flush=True)
        subprocess.run([
            str(PYTHON), str(ROOT / "make_video.py"), part["name"], part["prompt"],
            str(config["takes_per_prompt"]), str(config["seconds_per_take"]),
            "--no-style", "--bpm", str(part["bpm"]), "--scene", part["scene"],
            "--effects", *part["effects"],
            "--loop-seconds", str(config.get("loop_seconds", 40)),
            *(["--fresh"] if args.fresh else []),
        ], cwd=ROOT, check=True)
    subprocess.run([
        str(PYTHON), str(ROOT / "join.py"), config["name"],
        *[p["name"] for p in config["parts"]],
        "--title", "Last Train Through the Monsoon 🚆 Rainy Night Trap Lo-Fi [44 min mix]",
        "--blurb", "A late-night train journey in three chapters: Platform 02, Rain on the Glass, and Before the City Wakes. Nine instrumental takes with soft 808s, restrained trap drums, muted piano and hazy synths for studying, coding and winding down. Pixel-art train scenes with animated rain and passing light sweeps.",
        "--hashtags", "#lofitrap #trapbeats #lofi #lofihiphop #studymusic #codingmusic #rainynight #chillbeats #focusmusic #instrumental",
        "--tags", "lofi trap, trap lofi, rainy night music, train ambience, monsoon, 808, chill trap, study music, coding music, focus music, no vocals, instrumental, background music",
        *(["--fresh"] if args.fresh else []),
    ], cwd=ROOT, check=True)
    print("COMPLETE: releases/" + config["name"], flush=True)


if __name__ == "__main__":
    main()
