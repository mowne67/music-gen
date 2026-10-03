#!/usr/bin/env python3
"""Generate lo-fi candidates with the local ACE-Step API.

Start the server first:  cd ACE-Step-1.5 && ./start_api_server_macos.sh
Usage:  [DURATION=240] python3 gen.py [candidates_per_track] [track_name ...]
Output: candidates/<track>_<n>.wav (+ .json with seed/metas to reproduce)
"""
import json, os, sys, time, urllib.request, urllib.error
from pathlib import Path
from pipeline import media_valid, next_index, write_json

API = "http://127.0.0.1:8002"
OUT = Path(__file__).parent / "candidates"

# The channel's shared sound. Every track prompt gets this appended.
STYLE = ("Instrumental lo-fi hip hop, warm and dusty, soft swung drums, deep relaxed bass, "
         "subtle vinyl crackle and tape hiss, mellow and hypnotic, minimal repetitive melody, "
         "for studying and coding, no vocals.")

TRACKS = {
    "monsoon_window": "Late rainy night. Warm Rhodes electric piano, mellow jazz chords, gentle rain ambience. Melancholic but comforting.",
    "217_am": "2 AM stillness. Muted clean electric guitar, soft tape-saturated bass, sparse brushed drums. Lonely, quiet, intimate.",
    "last_bus_home": "Dreamy felt piano, distant city traffic ambience, sparse lazy beat. Nostalgic and tired, end of a long day.",
    "terrace_after_rain": ("Late evening on a rooftop terrace just after the rain. Warm Rhodes electric piano playing soft "
                           "extended seventh and ninth chords with gentle tremolo, a lush warm analog synth pad slowly swelling "
                           "underneath, round mellow sub bass, laid-back dusty drums with soft rimshots and brushed hi-hats. "
                           "Crickets and nighttime insects chirping, water droplets dripping from the eaves, distant thunder "
                           "fading away. Calm, open, peaceful, gently hopeful, fresh cool night air."),
    "tea_shop_closing": "Gentle jazz guitar, soft hand percussion, faint street ambience at dusk. Warm, cozy, unhurried.",
    # Off-channel tracks: a (prompt, bpm) tuple skips the lo-fi STYLE.
    "edm_1": ("Instrumental EDM, progressive house festival anthem. Punchy four-on-the-floor kick, "
              "bright supersaw lead, sidechained synth pads, rolling bassline, crisp claps and open hi-hats. "
              "Atmospheric intro, rising build-up with snare roll and riser, big euphoric drop, breakdown, "
              "second build and drop, outro. Energetic, uplifting, polished club mix, no vocals.", 126),
}
BPM, DURATION = 72, int(os.environ.get("DURATION", 180))  # seconds per take
GENERATION_TIMEOUT = int(os.environ.get("GENERATION_TIMEOUT", 1800))


def request_settings(prompt):
    prompt, bpm = prompt if isinstance(prompt, tuple) else (f"{prompt} {STYLE}", BPM)
    return {"prompt": prompt, "lyrics": "[Instrumental]", "bpm": bpm,
            "audio_duration": DURATION, "audio_format": "wav", "thinking": True,
            "use_cot_caption": False, "batch_size": 1}


def retry(operation, retries=3):
    for attempt in range(retries + 1):
        try:
            return operation()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == retries:
                raise
            print(f"API connection failed; retry {attempt + 1}/{retries}: {exc}", file=sys.stderr, flush=True)
            time.sleep(2 ** attempt)


def call(path, body=None, retries=3):
    req = urllib.request.Request(API + path, json.dumps(body).encode() if body else None,
                                 {"Content-Type": "application/json"})
    def send():
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    return retry(send, retries)


def generate(name, prompt, idx, task=None, on_task=None, timeout=None):
    settings = request_settings(prompt)
    dest = OUT / f"{name}_{idx}.wav"
    if dest.exists():
        raise FileExistsError(f"Refusing to overwrite {dest}")
    OUT.mkdir(parents=True, exist_ok=True)
    if task is None:
        # Retrying an ambiguous submission could queue a second generation.
        task = {"task_id": call("/release_task", settings, retries=0)["data"]["task_id"],
                "started_at": time.time()}
        if on_task:
            on_task(task)
    limit = timeout if timeout is not None else GENERATION_TIMEOUT
    while True:
        res = call("/query_result", {"task_id_list": [task["task_id"]]})["data"][0]
        if res["status"] == 2:
            raise RuntimeError(f"{name}_{idx} failed: {res.get('result')}")
        if res["status"] == 1:
            break
        if res["status"] != 0:
            raise RuntimeError(f"{name}_{idx}: unknown task status {res['status']}")
        if time.time() - task["started_at"] >= limit:
            raise TimeoutError(f"{name}_{idx}: task {task['task_id']} exceeded {limit}s. "
                               "The task ID is saved; rerun to check it without submitting another take.")
        time.sleep(5)
    item = json.loads(res["result"])[0]
    tmp = dest.with_suffix(".wav.part")
    def download():
        # Each retry starts a new file, never appending a partially received body.
        with urllib.request.urlopen(API + item["file"], timeout=60) as src, tmp.open("wb") as dst:
            import shutil
            shutil.copyfileobj(src, dst)
    try:
        retry(download)
        if not media_valid(tmp, DURATION, ("audio",)):
            raise RuntimeError(f"Downloaded audio is incomplete or has the wrong duration: {tmp}")
        item["request"] = settings
        write_json(dest.with_suffix(".json"), item)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"✓ {dest.name}  seed={item.get('seed_value')}", flush=True)
    return item


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    names = sys.argv[2:] or list(TRACKS)
    OUT.mkdir(exist_ok=True)
    for name in names:
        start = next_index(OUT, name)
        for i in range(start, start + n):
            generate(name, TRACKS[name], i)
