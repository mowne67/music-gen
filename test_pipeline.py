"""Small media fixtures: exercise recovery without loading ML models or servers."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import wave

import numpy as np
from PIL import Image
import animate
import art
import gen
import join
import make_video
import pipeline

SOURCE = Path(__file__).resolve().parent


def wav(path, seconds=7, frequency=220):
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = (7000 * np.sin(2 * np.pi * frequency * np.arange(16000 * seconds) / 16000)).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(samples.tobytes())


class HelpersTest(unittest.TestCase):
    def test_numbering_survives_gaps_and_partial_files(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("track_1.wav", "track_3.wav", "track_5.wav.part", "track_7.json", "track_other.wav"):
                (Path(d) / n).touch()
            self.assertEqual(pipeline.next_index(d, "track"), 8)

    def test_failed_command_exposes_its_diagnostic(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(subprocess.CalledProcessError):
            pipeline.run("ffmpeg", "-v", "error", "-i", "/missing/pipeline-fixture.wav", "-f", "null", "-")
        self.assertIn("pipeline-fixture.wav", err.getvalue())

    def test_network_retry_is_bounded(self):
        failure = urllib.error.URLError("connection reset")
        with patch("gen.time.sleep"), patch("sys.stderr", io.StringIO()):
            from unittest.mock import Mock
            operation = Mock(side_effect=[failure, failure, "ok"])
            self.assertEqual(gen.retry(operation), "ok")
            self.assertEqual(operation.call_count, 3)
            operation = Mock(side_effect=failure)
            with self.assertRaises(urllib.error.URLError):
                gen.retry(operation)
            self.assertEqual(operation.call_count, 4)

    def test_animation_wraps_and_is_reproducible(self):
        base = np.full((animate.H, animate.W, 3), 0.35, dtype=np.float32)
        effects = {"rain": None, "sweep": None, "flicker": None, "grain": None, "fireflies": None}
        first = next(animate.frames(base, effects, seconds=40, seed=9, frame_count=1))
        wrap = next(animate.frames(base, effects, seconds=40, seed=9, start_frame=960, frame_count=1))
        np.testing.assert_array_equal(first, wrap)
        events = animate.light_sweeps(40, 9)
        self.assertEqual(len({e["start_frame"] for e in events}), 5)
        self.assertGreater(len({e["duration_frames"] for e in events}), 1)
        self.assertTrue(all(isinstance(e["start_frame"], int) for e in events))


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.out = Path(self.folder.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(gen, "OUT", self.out))
        self.stack.enter_context(patch.object(gen, "DURATION", 2))

    def tearDown(self):
        self.stack.close(); self.folder.cleanup()

    def result(self):
        return {"data": [{"status": 1, "result": json.dumps([{"file": "/fixture.wav", "seed_value": "42"}])}]}

    def test_expired_running_task_times_out_without_resubmitting(self):
        with patch("gen.call", return_value={"data": [{"status": 0}]}) as call:
            with self.assertRaisesRegex(TimeoutError, "task ID is saved"):
                gen.generate("fixture", ("prompt", 72), 1,
                             task={"task_id": "saved-task", "started_at": time.time() - 50}, timeout=1)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0], "/query_result")

    def test_completed_expired_task_recovers_download(self):
        source = self.out / "source.wav"
        wav(source, seconds=2)
        with patch("gen.call", return_value=self.result()) as call, \
                patch("gen.urllib.request.urlopen", return_value=io.BytesIO(source.read_bytes())):
            gen.generate("fixture", ("prompt", 72), 1,
                         task={"task_id": "saved-task", "started_at": 0}, timeout=1)
        self.assertEqual(call.call_args.args[0], "/query_result")
        self.assertTrue(pipeline.media_valid(self.out / "fixture_1.wav", 2, ("audio",)))
        detail = json.loads((self.out / "fixture_1.json").read_text())
        self.assertEqual(detail["request"]["prompt"], "prompt")
        self.assertFalse((self.out / "fixture_1.wav.part").exists())

    def test_truncated_download_never_becomes_a_take(self):
        source = self.out / "source.wav"
        wav(source, seconds=2)
        with patch("gen.call", return_value=self.result()), \
                patch("gen.urllib.request.urlopen", return_value=io.BytesIO(source.read_bytes()[:100])), \
                self.assertRaisesRegex(RuntimeError, "incomplete"):
            gen.generate("fixture", ("prompt", 72), 1, task={"task_id": "saved", "started_at": 0})
        self.assertFalse((self.out / "fixture_1.wav").exists())
        self.assertFalse((self.out / "fixture_1.json").exists())
        self.assertFalse((self.out / "fixture_1.wav.part").exists())

    def test_existing_take_is_never_overwritten(self):
        dest = self.out / "fixture_1.wav"
        dest.write_bytes(b"approved")
        with patch("gen.call") as call, self.assertRaises(FileExistsError):
            gen.generate("fixture", "prompt", 1)
        call.assert_not_called()
        self.assertEqual(dest.read_bytes(), b"approved")


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="music-gen-test-")
        self.root = Path(self.folder.name)
        self.stack = contextlib.ExitStack()
        for module in (pipeline, make_video, join, art):
            self.stack.enter_context(patch.object(module, "ROOT", self.root))
        self.stack.enter_context(patch.object(gen, "OUT", self.root / "candidates"))
        self.stack.enter_context(patch.object(gen, "BPM", gen.BPM))
        self.stack.enter_context(patch.object(gen, "DURATION", gen.DURATION))
        self.stack.enter_context(patch.object(art, "OUT", self.root / "art"))
        self.stack.enter_context(patch("pipeline.environment", return_value={"fixture": True}))
        shutil.copy(SOURCE / "stitch.sh", self.root / "stitch.sh")
        self.args = argparse.Namespace(name="fixture", prompt="test instrumental", takes=3, seconds=7,
                                      bpm=72, scene="test window", effects=["rain", "sweep"], no_style=True,
                                      loop_seconds=2, animation_seed=7, generation_timeout=60, fresh=False)
        self.generated = []

    def tearDown(self):
        self.stack.close(); self.folder.cleanup()

    def generate(self, name, prompt, index, task=None, on_task=None, timeout=None):
        self.generated.append(index)
        task = task or {"task_id": f"fixture-{index}", "started_at": time.time()}
        if on_task:
            on_task(task)
        dest = gen.OUT / f"{name}_{index}.wav"
        wav(dest, seconds=gen.DURATION, frequency=220 + index * 20)
        pipeline.write_json(dest.with_suffix(".json"), {"request": gen.request_settings(prompt),
                            "seed_value": str(index), "lm_model": "fixture-lm", "dit_model": "fixture-dit"})

    def artwork(self, name, scene, start, n, seeds=None):
        paths = []
        for idx, seed in zip(range(start, start + n), seeds):
            dest = art.OUT / f"{name}_{idx}.png"
            Image.new("RGB", (96, 54), (50, 100, 150)).save(dest)
            pipeline.write_json(dest.with_suffix(".json"), {"seed": seed, "prompt": f"{scene} {art.STYLE}",
                                "model": "fixture-image", "steps": 0})
            paths.append(dest)
        return paths

    def commands(self, *cmd):
        if len(cmd) > 1 and str(cmd[1]).endswith("animate.py"):
            cmd = (sys.executable, SOURCE / "animate.py", *cmd[2:], "--output-dir", self.root / "video")
        return pipeline.run(*cmd)

    def build(self, art_effect=None):
        with patch("make_video.ensure_server") as server, patch("gen.generate", side_effect=self.generate), \
                patch("art.generate", side_effect=art_effect or self.artwork), \
                patch("make_video.run", side_effect=self.commands):
            make_video.build_release(self.args)
        return server

    def test_art_failure_resumes_without_regenerating_audio_and_join_resumes(self):
        with self.assertRaisesRegex(RuntimeError, "art unavailable"):
            self.build(art_effect=RuntimeError("art unavailable"))
        self.assertEqual(self.generated, [1, 2, 3])
        mix = self.root / "mixes/fixture_mix.wav"
        previous_mix = pipeline.record(mix)
        manifest = self.root / "releases/fixture/manifest.json"
        self.assertEqual(json.loads(manifest.read_text())["status"], "failed")
        self.build().assert_not_called()
        self.assertEqual(self.generated, [1, 2, 3])
        self.assertEqual(pipeline.record(mix), previous_mix)
        saved = json.loads(manifest.read_text())
        self.assertEqual(saved["status"], "complete")
        self.assertEqual([t["seed"] for t in saved["takes"]], ["1", "2", "3"])
        self.assertEqual(saved["animation"]["seconds"], 2)
        with patch("gen.generate") as music, patch("art.generate") as image, patch("make_video.ensure_server") as server:
            make_video.build_release(self.args)
            music.assert_not_called(); image.assert_not_called(); server.assert_not_called()
        self.args.prompt = "different prompt"
        with self.assertRaisesRegex(RuntimeError, "settings differ"):
            make_video.build_release(self.args)
        # A manifested part plus a legacy part, with real FFmpeg joining and resume.
        legacy = self.root / "releases/legacy"
        legacy.mkdir()
        shutil.copy(mix, self.root / "mixes/legacy_mix.wav")
        for name in ("video.mp4", "thumbnail.png"):
            shutil.copy(manifest.parent / name, legacy / name)
        args = argparse.Namespace(name="joined", parts=["fixture", "legacy"], title="fixture", blurb="fixture",
                                  hashtags="#fixture", tags="fixture", fresh=False)
        original_run = join.run
        def fail_mux(*cmd):
            if "concat" in cmd:
                raise RuntimeError("mux interrupted")
            return original_run(*cmd)
        with patch("join.run", side_effect=fail_mux), self.assertRaisesRegex(RuntimeError, "mux interrupted"):
            join.build_join(args)
        joined_audio = self.root / "releases/joined/audio.wav"
        audio_before = pipeline.record(joined_audio)
        observed = []
        def resumed_join(*cmd):
            observed.append(cmd)
            if "concat" in cmd:
                self.assertEqual(pipeline.record(joined_audio), audio_before)
            return original_run(*cmd)
        with patch("join.run", side_effect=resumed_join):
            join.build_join(args)
        self.assertFalse(any("-filter_complex" in cmd for cmd in observed))
        self.assertFalse(joined_audio.exists())
        joined = self.root / "releases/joined/manifest.json"
        detail = json.loads(joined.read_text())
        self.assertEqual(detail["parts"][0]["manifest"]["takes"][0]["seed"], "1")
        self.assertEqual(detail["parts"][1]["manifest"]["status"], "legacy_unrecorded")
        with patch("join.run") as external:
            join.build_join(args)
            external.assert_not_called()
        wav(self.root / "mixes/legacy_mix.wav", seconds=2)
        with self.assertRaisesRegex(RuntimeError, "durations differ"):
            join.part_sources("legacy")

    def test_pending_task_is_reused_after_a_generation_failure(self):
        original = self.generate
        def fail_second(name, prompt, index, task=None, on_task=None, timeout=None):
            if index == 2:
                on_task({"task_id": "pending-2", "started_at": time.time()})
                raise RuntimeError("connection dropped")
            return original(name, prompt, index, task, on_task, timeout)
        with patch("gen.generate", side_effect=fail_second), patch("make_video.ensure_server"), \
                self.assertRaisesRegex(RuntimeError, "connection dropped"):
            make_video.build_release(self.args)
        manifest = json.loads((self.root / "releases/fixture/manifest.json").read_text())
        self.assertEqual(manifest["takes"][1]["task"]["task_id"], "pending-2")
        tasks = []
        def resumed(*args, **kwargs):
            tasks.append(kwargs.get("task"))
            return original(*args, **kwargs)
        with patch("gen.generate", side_effect=resumed), patch("make_video.ensure_server"), \
                patch("art.generate", side_effect=RuntimeError("stop before artwork")), \
                self.assertRaisesRegex(RuntimeError, "stop before artwork"):
            make_video.build_release(self.args)
        self.assertEqual(tasks[0]["task_id"], "pending-2")
        self.assertEqual(self.generated, [1, 2, 3])

    def test_legacy_video_is_protected(self):
        out = self.root / "releases/fixture"
        out.mkdir(parents=True)
        (out / "video.mp4").write_bytes(b"existing video")
        with patch("gen.generate") as music, self.assertRaisesRegex(RuntimeError, "predates manifests"):
            make_video.build_release(self.args)
        music.assert_not_called()
        self.assertEqual((out / "video.mp4").read_bytes(), b"existing video")

    def test_single_take_has_a_joinable_mix(self):
        self.args.takes = 1
        self.build()
        mix, video, detail, _ = join.part_sources("fixture")
        self.assertTrue(mix.is_file())
        self.assertEqual(len(detail["takes"]), 1)
        args = argparse.Namespace(name="single_join", parts=["fixture"], title="fixture", blurb="fixture",
                                  hashtags="#fixture", tags="fixture", fresh=False)
        join.build_join(args)
        self.assertTrue(pipeline.media_valid(self.root / "releases/single_join/video.mp4", 7, ("audio", "video")))


if __name__ == "__main__":
    unittest.main()
