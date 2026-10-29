"""Native manifest helper contract. Run: python3 -m unittest tests.test_native_manifest."""
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    "BPF/xdp_loader", "BPF/afxdp_recv", "udp_socket/bin/sender",
    "udp_socket/bin/receiver", "udp_socket/bin/phc_calibrate",
    "udp_socket/bin/ace_clock", "experiment/ace-run",
)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux native helper")
class NativeManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which(os.environ.get("CC", "cc"))
        cls.git = shutil.which("git")
        if not cls.compiler or not cls.git or not shutil.which("sha256sum"):
            raise unittest.SkipTest("cc, git and sha256sum are required")
        cls.build_dir = tempfile.TemporaryDirectory(prefix="ace-manifest-build-")
        cls.addClassCleanup(cls.build_dir.cleanup)
        base = Path(cls.build_dir.name)
        harness = base / "main.c"
        harness.write_text(r'''#include "ace_manifest.h"
#include <errno.h>
#include <stdio.h>
int main(int argc, char **argv) {
    int result;
    if (argc != 7) return 2;
    result = ace_write_manifest(argv[1], argv[2], argv[3], argv[4], argv[5], argv[6]);
    if (result) {
        fprintf(stderr, "return=%d errno=%d\n", result, errno);
        return result == -1 ? 1 : 3;
    }
    return 0;
}
''', encoding="utf-8")
        cls.executable = base / "test-manifest"
        subprocess.run([
            cls.compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2",
            "-I", str(ROOT / "experiment"), str(harness),
            str(ROOT / "experiment/ace_manifest.c"), "-o", str(cls.executable),
        ], check=True, capture_output=True, text=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ace-manifest-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        # Metacharacters and non-ASCII paths exercise argv execution and JSON encoding.
        self.repo = self.directory / "repo space;'한글"
        self.repo.mkdir()
        subprocess.run([self.git, "init", "-q", str(self.repo)], check=True)
        (self.repo / "tracked").write_text("source\n", encoding="utf-8")
        subprocess.run([self.git, "-C", str(self.repo), "add", "tracked"], check=True)
        subprocess.run([
            self.git, "-C", str(self.repo), "-c", "user.name=test",
            "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture",
        ], check=True)
        for relative in ARTIFACTS:
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((relative + "\n").encode())
        self.condition = '조건 "one"\n😀'
        self.interface = 'eth"0\\\t'
        self.parameters = {"mode": "mixed", "payload": 64}
        self.output = self.directory / "manifest.json"

    def arguments(self, output=None, run_id="test-001", condition=None):
        return [
            str(self.executable), str(output or self.output), run_id,
            self.condition if condition is None else condition,
            self.interface, str(self.repo), json.dumps(self.parameters),
        ]

    def invoke(self, output=None, **kwargs):
        return subprocess.run(self.arguments(output), capture_output=True, text=True,
                              timeout=15, **kwargs)

    def assert_failure(self, result, expected_errno):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"return=-1 errno={expected_errno}", result.stderr)

    def test_schema_unicode_and_seven_sha256_digests(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.output.read_bytes().decode("ascii"))
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["run_id"], "test-001")
        self.assertEqual(manifest["condition_id"], self.condition)
        self.assertEqual(manifest["interface"]["name"], self.interface)
        self.assertEqual(manifest["parameters"], self.parameters)
        self.assertTrue(manifest["system"]["kernel_release"])
        self.assertEqual(len(manifest["artifact_hashes"]), len(ARTIFACTS))
        for artifact, relative in zip(manifest["artifact_hashes"], ARTIFACTS):
            data = (self.repo / relative).read_bytes()
            self.assertEqual(artifact, {
                "path": str(self.repo / relative), "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })

    def test_existing_file_is_preserved_with_eexist(self):
        self.output.write_bytes(b"existing-run\n")
        self.assert_failure(self.invoke(), errno.EEXIST)
        self.assertEqual(self.output.read_bytes(), b"existing-run\n")
        self.assertEqual(list(self.directory.glob("*.tmp.*")), [])

    def test_existing_symlink_is_preserved(self):
        target = self.directory / "target"
        target.write_bytes(b"existing-target")
        self.output.symlink_to(target)
        self.assert_failure(self.invoke(), errno.EEXIST)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(target.read_bytes(), b"existing-target")

    def test_absent_artifact_is_explicit(self):
        (self.repo / ARTIFACTS[0]).unlink()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.output.read_text())
        self.assertEqual(manifest["artifact_hashes"][0], {
            "path": str(self.repo / ARTIFACTS[0]), "missing": True,
        })

    def test_missing_sha256sum_fails_without_publication(self):
        binaries = self.directory / "bin"
        binaries.mkdir()
        (binaries / "git").symlink_to(self.git)
        result = self.invoke(env=dict(os.environ, PATH=str(binaries)))
        self.assert_failure(result, errno.ENOENT)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.directory.glob("*.tmp.*")), [])

    def test_invalid_run_id_fails_without_publication(self):
        result = subprocess.run(self.arguments(run_id="../escape"), capture_output=True,
                                text=True, timeout=15)
        self.assert_failure(result, errno.EINVAL)
        self.assertFalse(self.output.exists())

    def test_invalid_utf8_cleans_temporary_file(self):
        argv = [os.fsencode(item) for item in self.arguments()]
        argv[3] = b"invalid-\xff"
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        self.assert_failure(result, errno.EILSEQ)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.directory.glob("*.tmp.*")), [])

    def test_concurrent_publish_is_atomic_and_exclusive(self):
        children = [subprocess.Popen(self.arguments(), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            deadline = time.monotonic() + 15
            while not self.output.exists() and time.monotonic() < deadline:
                time.sleep(0.002)
            self.assertTrue(self.output.exists())
            # The first visible final file must already contain a complete document.
            first = self.output.read_bytes()
            self.assertEqual(json.loads(first)["run_id"], "test-001")
            results = [child.communicate(timeout=15) for child in children]
            self.assertEqual(sorted(child.returncode for child in children), [0, 1])
            loser = next(index for index, child in enumerate(children) if child.returncode)
            self.assertIn(f"return=-1 errno={errno.EEXIST}", results[loser][1])
            self.assertEqual(self.output.read_bytes(), first)
            self.assertEqual(list(self.directory.glob("*.tmp.*")), [])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()


if __name__ == "__main__":
    unittest.main()
