"""Shared subprocess, artifact and checkpoint helpers for the local pipeline."""
import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def run(*cmd):
    try:
        return subprocess.run([str(c) for c in cmd], check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = "\n".join((exc.stderr or exc.stdout or "No diagnostic output").splitlines()[-20:])
        print(f"{cmd[0]} failed (exit {exc.returncode}):\n{detail}", file=sys.stderr, flush=True)
        raise


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def next_index(directory, name):
    # Include sidecars and partial files, so a gap never causes an overwrite.
    pattern = re.compile(re.escape(name) + r"_(\d+)\.")
    indices = [int(m[1]) for p in Path(directory).glob(name + "_*") if (m := pattern.match(p.name))]
    return max(indices, default=0) + 1


def record(path):
    path = Path(path).resolve()
    stat = path.stat()
    try:
        name = str(path.relative_to(ROOT))
    except ValueError:
        name = str(path)
    return {"path": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def matches(saved):
    try:
        return record(ROOT / saved["path"]) == saved
    except (OSError, KeyError):
        return False


def probe(path):
    info = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path).stdout)
    seconds = float(info["format"]["duration"])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"Invalid media duration: {path}")
    return info


def media_valid(path, seconds=None, types=()):
    if not Path(path).is_file() or Path(path).stat().st_size == 0:
        return False
    try:
        info = probe(path)
        present = {s["codec_type"] for s in info["streams"]}
        return set(types) <= present and (seconds is None or abs(float(info["format"]["duration"]) - seconds) <= 0.15)
    except (ValueError, KeyError, subprocess.CalledProcessError):
        return False


def checkpoint_valid(state, stage, inputs):
    saved = state.get("checkpoints", {}).get(stage)
    if not saved:
        return False
    try:
        return saved["inputs"] == [record(p) for p in inputs] and all(matches(o) for o in saved["outputs"])
    except OSError:
        return False


def checkpoint(state, stage, inputs, outputs):
    state.setdefault("checkpoints", {})[stage] = {
        "inputs": [record(p) for p in inputs], "outputs": [record(p) for p in outputs],
    }


def environment():
    versions = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()
                if dist.metadata.get("Name")}
    versions = dict(sorted(versions.items(), key=lambda item: item[0].lower()))
    def revision(directory):
        result = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    scripts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("*.py")}
    scripts.update({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("*.sh")})
    return {"python": sys.version.split()[0], "packages": versions,
            "pipeline_commit": revision(ROOT), "acestep_commit": revision(ROOT / "ACE-Step-1.5"),
            "script_sha256": scripts, "ffmpeg": run("ffmpeg", "-version").stdout.splitlines()[0]}


@contextlib.contextmanager
def release_lock(out):
    Path(out).mkdir(parents=True, exist_ok=True)
    with (Path(out) / ".pipeline.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another pipeline is working on {out}") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_state(out, request, kind, fresh=False):
    path = Path(out) / "manifest.json"
    if path.exists() and not fresh:
        state = json.loads(path.read_text())
        if state.get("schema_version") != 1 or state.get("kind") != kind or state.get("request") != request:
            raise RuntimeError(f"Saved settings differ for {out}. Use --fresh for a new run.")
        return state
    if (Path(out) / "video.mp4").exists() and not path.exists() and not fresh:
        raise RuntimeError(f"{out} predates manifests; refusing to replace it. Use --fresh for a new run.")
    if path.exists():
        old = json.loads(path.read_text())
        write_json(Path(out) / ("manifest." + old["run_id"] + ".json"), old)
    import uuid
    return {"schema_version": 1, "kind": kind, "run_id": uuid.uuid4().hex,
            "created_at": now(), "status": "running", "request": request,
            "environment": environment(), "checkpoints": {}}
