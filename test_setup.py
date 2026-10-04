"""Exercise setup recovery and file protection without installing tools/models."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
PIN = "ca1e85fe9430179831e6bc6be790c332190a3866"


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="music gen setup ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "existing checkout"
        self.project.mkdir()
        for name in ("setup.sh", "requirements-art.txt", "make_video.py"):
            shutil.copy(ROOT / name, self.project / name)
        (self.project / "docs").mkdir()
        shutil.copy(ROOT / "docs/acestep-macos-launcher.patch", self.project / "docs")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "commands.log"
        self.real_git = shutil.which("git")
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        TEST_LOG=str(self.log), TEST_TEMPLATE=str(self.project),
                        TEST_REAL_GIT=self.real_git)
        # Only external installation commands are stubbed. The shell script and
        # launcher patch run normally, including paths with spaces.
        self.stub("uname", 'if [[ "$1" == -s ]]; then echo "${TEST_OS:-Darwin}"; else echo arm64; fi')
        for tool in ("brew", "ffmpeg", "ffprobe"):
            self.stub(tool, f'echo "{tool} $*" >> "$TEST_LOG"')
        self.stub("git", f'''
echo "git $*" >> "$TEST_LOG"
if [[ "$1" == --version ]]; then echo "git version 2"; exit 0; fi
if [[ "$1" == clone ]]; then
    target="${{@: -1}}"
    mkdir -p "$target"
    if [[ "$target" == */ACE-Step-1.5 ]]; then
        printf 'PORT=8001\\nCHECK_UPDATE="true"\\n' > "$target/start_api_server_macos.sh"
        # The real pinned file supplies the context for git apply.
        cp "$TEST_LAUNCHER" "$target/start_api_server_macos.sh"
        chmod +x "$target/start_api_server_macos.sh"
    else
        cp -R "$TEST_TEMPLATE/." "$target/"
    fi
    exit 0
fi
if [[ "$1" == -C ]]; then
    target="$2"; shift 2
    if [[ "$1" == rev-parse && "$2" == --show-toplevel ]]; then echo "$target"; exit 0; fi
    if [[ "$1" == rev-parse && "$2" == HEAD ]]; then echo "${{TEST_ACE_HEAD:-{PIN}}}"; exit 0; fi
    if [[ "$1" == status ]]; then echo -n "${{TEST_ACE_DIRTY:-}}"; exit 0; fi
    if [[ "$1" == checkout ]]; then exit 0; fi
    exec "$TEST_REAL_GIT" -C "$target" "$@"
fi
exit 99
''')
        self.stub("uv", '''
echo "uv $*" >> "$TEST_LOG"
if [[ "$1" == venv ]]; then
    mkdir -p "$2/bin"
    printf '#!/bin/bash\\nexit 0\\n' > "$2/bin/python"
    cat > "$2/bin/hf" <<'HF'
#!/bin/bash
echo "hf $*" >> "$TEST_LOG"
if [[ "${TEST_FAIL_DOWNLOAD:-}" == 1 ]]; then echo 'download interrupted' >&2; exit 7; fi
HF
    chmod +x "$2/bin/python" "$2/bin/hf"
fi
''')
        # Restore the pinned launcher from the tracked patch's old side. No
        # vendor checkout or network is needed to run these tests.
        patch = (ROOT / "docs/acestep-macos-launcher.patch").read_text().splitlines()
        lines = []
        for line in patch:
            if line.startswith(" ") or (line.startswith("-") and not line.startswith("---")):
                lines.append(line[1:])
        self.launcher = self.base / "original-launcher.sh"
        self.launcher.write_text("\n".join(lines) + "\n")
        self.env["TEST_LAUNCHER"] = str(self.launcher)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -e\n" + body + "\n")
        path.chmod(0o755)

    def run_setup(self, bootstrap=False, **env):
        command = (["/bin/bash", "-c", (ROOT / "setup.sh").read_text()] if bootstrap
                   else ["/bin/bash", str(self.project / "setup.sh")])
        return subprocess.run(command, cwd=self.base, env={**self.env, **env},
                              capture_output=True, text=True)

    def test_bootstrap_and_rerun_reuse_checkout_and_environment(self):
        result = self.run_setup(bootstrap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.base / "music-gen"
        launcher = installed / "ACE-Step-1.5/start_api_server_macos.sh"
        self.assertIn("PORT=8002", launcher.read_text())
        self.assertIn('CHECK_UPDATE="false"', launcher.read_text())
        previous = launcher.read_bytes()
        result = self.run_setup(bootstrap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(launcher.read_bytes(), previous)
        commands = self.log.read_text().splitlines()
        self.assertEqual(sum(line.startswith("git clone ") for line in commands), 2)
        self.assertEqual(sum(line.startswith("uv venv ") for line in commands), 1)
        self.assertEqual(sum(line.startswith("hf download ") for line in commands), 2)

    def test_download_failure_can_be_retried(self):
        result = self.run_setup(TEST_FAIL_DOWNLOAD="1")
        self.assertEqual(result.returncode, 7)
        self.assertIn("Setup stopped while downloading artwork weights", result.stderr)
        self.assertNotIn("Ready!", result.stdout)
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().count("uv venv "), 1)

    def test_existing_unrelated_folder_is_preserved(self):
        unrelated = self.base / "music-gen"
        unrelated.mkdir()
        sentinel = unrelated / "important.txt"
        sentinel.write_text("keep this")
        result = self.run_setup(bootstrap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a music-gen checkout", result.stderr)
        self.assertEqual(sentinel.read_text(), "keep this")
        self.assertNotIn("uv ", self.log.read_text())

    def test_dirty_upstream_at_another_revision_is_preserved(self):
        ace = self.project / "ACE-Step-1.5"
        ace.mkdir()
        launcher = ace / "start_api_server_macos.sh"
        launcher.write_text("custom launcher")
        result = self.run_setup(TEST_ACE_HEAD="another-revision", TEST_ACE_DIRTY=" M launcher")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local changes at another revision", result.stderr)
        self.assertEqual(launcher.read_text(), "custom launcher")
        self.assertNotIn("hf download", self.log.read_text())

    def test_unsupported_platform_fails_before_installing(self):
        result = self.run_setup(TEST_OS="Linux")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("macOS on Apple Silicon", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
