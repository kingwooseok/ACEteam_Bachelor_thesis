#!/usr/bin/env python3
"""Offline regression tests for source provenance capture."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from monitor import capture_manifest


class CaptureManifestTests(unittest.TestCase):
    def run_git(self, repository: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def test_dirty_and_committed_sources_are_reconstructible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "source"
            repository.mkdir()
            self.run_git(repository, "init", "-q")
            self.run_git(repository, "config", "user.name", "ACE Test")
            self.run_git(repository, "config", "user.email", "ace@example.invalid")

            tracked = repository / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            self.run_git(repository, "add", "tracked.txt")
            self.run_git(repository, "commit", "-qm", "base")
            self.run_git(repository, "branch", "experiment-base")

            tracked.write_bytes(b"feature\0binary\n")
            self.run_git(repository, "commit", "-qam", "feature")
            tracked.write_bytes(b"dirty trailing spaces  \n\xff\0")
            untracked_bytes = b"new\0source\xff\r\n"
            (repository / "untracked.bin").write_bytes(untracked_bytes)
            os.symlink("../outside-secret", repository / "external-link")

            info, dirty_patch = capture_manifest.git_repository(repository)
            series, committed_patch = capture_manifest.git_committed_series(
                repository, "experiment-base"
            )

            self.assertTrue(info["dirty"])
            self.assertEqual(
                [item["path"] for item in info["untracked_files"]],
                ["external-link", "untracked.bin"],
            )
            link_info = info["untracked_files"][0]
            self.assertEqual(link_info["type"], "symlink")
            self.assertEqual(link_info["target"], "../outside-secret")
            self.assertIn(b"untracked.bin", dirty_patch)
            self.assertTrue(dirty_patch.endswith(b"\n"))
            self.assertTrue(series["available"])
            self.assertEqual(len(series["commits"]), 1)
            self.assertIn(b"Subject: [PATCH] feature", committed_patch)

            series_path = root / "committed.patch"
            series_path.write_bytes(committed_patch)
            dirty_path = root / "dirty.patch"
            dirty_path.write_bytes(dirty_patch)
            reconstructed = root / "reconstructed"
            self.run_git(root, "clone", "-q", str(repository), str(reconstructed))
            self.run_git(reconstructed, "config", "user.name", "ACE Test")
            self.run_git(
                reconstructed, "config", "user.email", "ace@example.invalid"
            )
            self.run_git(reconstructed, "checkout", "-q", "experiment-base")
            self.run_git(reconstructed, "am", str(series_path))
            self.assertEqual(
                self.run_git(repository, "rev-parse", "HEAD^{tree}"),
                self.run_git(reconstructed, "rev-parse", "HEAD^{tree}"),
            )
            self.run_git(reconstructed, "apply", "--binary", str(dirty_path))
            self.assertEqual(reconstructed.joinpath("tracked.txt").read_bytes(),
                             tracked.read_bytes())
            self.assertEqual(
                reconstructed.joinpath("untracked.bin").read_bytes(),
                untracked_bytes,
            )
            self.assertTrue(reconstructed.joinpath("external-link").is_symlink())
            self.assertEqual(
                os.readlink(reconstructed / "external-link"), "../outside-secret"
            )

    def test_missing_base_ref_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.run_git(repository, "init", "-q")
            self.run_git(repository, "config", "user.name", "ACE Test")
            self.run_git(repository, "config", "user.email", "ace@example.invalid")
            (repository / "tracked").write_text("base\n", encoding="utf-8")
            self.run_git(repository, "add", "tracked")
            self.run_git(repository, "commit", "-qm", "base")

            with self.assertRaises(capture_manifest.ProvenanceError):
                capture_manifest.git_committed_series(repository, "missing-base")

    def test_exclusive_write_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "artifact"
            capture_manifest.exclusive_write(destination, b"first")
            with self.assertRaises(FileExistsError):
                capture_manifest.exclusive_write(destination, b"second")
            self.assertEqual(destination.read_bytes(), b"first")

    def test_tree_hash_is_ordered_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "b.ko").write_bytes(b"second")
            (root / "a.ko").write_bytes(b"first")
            (root / "ignored.txt").write_bytes(b"ignored")

            first = capture_manifest.tree_sha256(root, (".ko",))
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(first["files"], 2)
            self.assertEqual(first["bytes"], 11)

            (root / "a.ko").write_bytes(b"changed")
            second = capture_manifest.tree_sha256(root, (".ko",))
            self.assertIsNotNone(second)
            assert second is not None
            self.assertNotEqual(first["sha256"], second["sha256"])



if __name__ == "__main__":
    unittest.main()
