# Local music video pipeline

ACE-Step generates music, FFmpeg stitches and masters it, local FLUX generates
pixel artwork, and a repeating animation supplies the picture. Model files and
generated media stay outside Git.

The current ACE-Step checkout is pinned to
`ca1e85fe9430179831e6bc6be790c332190a3866` in
<https://github.com/ace-step/ACE-Step-1.5>. Keep its own Python environment and
checkpoints in `ACE-Step-1.5/`. Art environment versions are recorded in
`requirements-art.txt`; the existing `.venv-flux` runs these scripts. FFmpeg and
ffprobe must be on PATH.

## Run and resume

```sh
.venv-flux/bin/python make_video.py new_track "Instrumental music prompt" 3 300 \
  --no-style --bpm 140 --scene "Pixel-art scene" --effects rain sweep flicker grain
```

Rerun the same command to resume. Each take's task ID and output are checkpointed,
so a connection failure does not automatically submit another generation.
Completed, matching releases are skipped without loading music or image models.
Changed settings are rejected instead of silently replacing a release. Add
`--fresh` to explicitly start a new run: this allocates new take/art numbers,
archives the previous manifest and publishes a replacement video after rendering.

The existing monsoon batch is configured in `monsoon_train.json`:

```sh
.venv-flux/bin/python run_monsoon_train.py
```

**Legacy releases:** videos created before manifests, including the already
finished monsoon video, remain untouched. They cannot be reliably resumed from
file existence alone, so the default refuses to replace them. Use a new release
name, or explicitly use `--fresh` when you want a replacement. Joining legacy
parts is supported after checking their mix/video durations, with missing
provenance labelled `legacy_unrecorded` in the new manifest.

`join.py` also resumes matching inputs. Parts with failed/in-progress manifests,
changed artifacts or mix/video duration differences over 0.1 seconds are rejected.
Single-take releases now also save a canonical mix, so they can be joined.

API queries and audio downloads retry transient connection failures up to three
times. Task submission is not retried automatically, since an uncertain response
could otherwise enqueue a duplicate take. Polling defaults to a 30-minute limit
per task, including cold model loading; override with `--generation-timeout`
(seconds), or `GENERATION_TIMEOUT` for `gen.py`. A timed-out task remains recorded
and is checked on the next run before another timeout is raised. FFmpeg/model
command failures print the last 20 diagnostic lines.

## Animation

New loops default to **40 seconds** with five varied passing-light sweeps, rain,
flicker and grain. Timing and noise are deterministic for the saved animation
seed. The train platform can use just rain/flicker/grain when it should feel still.

```sh
.venv-flux/bin/python animate.py art/new_track_1.png rain sweep flicker grain \
  --seconds 40 --seed 7
```

`make_video.py` accepts `--loop-seconds` and `--animation-seed`. Existing videos
are not rerendered by a script update. Loops use H.264 without B-frames to reduce
timestamp overlaps when cutting and concatenating the repeated video.

## Provenance

Each new `releases/NAME/manifest.json` contains the requested prompt/settings,
selected take indexes and music seeds/model IDs, planned musical metadata,
art candidates and selected image, complete art prompts/settings/seeds,
animation effects/timing/seed, upstream commit, installed package versions,
script hashes, artifact fingerprints and checkpoint status. Joined manifests
include snapshots of their source manifests. Fingerprints use file size and
modification time to detect changed files without hashing multi-gigabyte videos.
Art and animation also have JSON sidecars next to their source artifacts.

Seeds and versions record the generation inputs; they do not guarantee identical
outputs across machines or the stochastic music planning stage.

## Verification

```sh
.venv-flux/bin/python -m unittest -v test_pipeline
```

Tests use tiny synthetic WAVs and images with real FFmpeg/ffprobe. They cover
interrupted runs, saved task recovery, corrupt downloads, file numbering gaps,
protected legacy outputs, manifests, completed-run skips, mix/video mismatches
and deterministic animation wraparound. No model generation or server is needed.
This is regression coverage for the implementation, not an automated musical
quality review or a new release-QA workflow.
