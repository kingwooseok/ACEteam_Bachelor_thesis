#!/usr/bin/env python3
"""Capture a reproducibility manifest without changing experiment state."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KERNEL_TREE = PROJECT_ROOT.parent / "rt-experiment" / "linux-6.18-rt"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ProvenanceError(RuntimeError):
    """Required source provenance could not be captured losslessly."""


def command(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        )
        return {
            "command": shlex.join(argv),
            "returncode": completed.returncode,
            "stdout": completed.stdout.rstrip(),
            "stderr": completed.stderr.rstrip(),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return {
            "command": shlex.join(argv),
            "error": type(error).__name__,
            "detail": str(error),
        }


def command_bytes(argv: list[str], cwd: Path | None = None) -> dict[str, Any]:
    """Run a command without decoding or trimming its stdout."""
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        return {
            "command": shlex.join(argv),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr.decode("utf-8", errors="replace"),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return {
            "command": shlex.join(argv),
            "error": type(error).__name__,
            "detail": str(error),
        }


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").rstrip()
    except OSError:
        return None


def sha256(path: Path) -> dict[str, Any] | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        status = path.stat()
        return {
            "path": str(path),
            "bytes": status.st_size,
            "sha256": digest.hexdigest(),
        }
    except OSError:
        return None


def untracked_digest(root: Path, name: str) -> dict[str, Any]:
    """Hash an untracked regular file or record an untracked symlink."""
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProvenanceError(f"unsafe untracked path from git: {name!r}")
    path = root / relative
    try:
        status = path.lstat()
    except OSError as error:
        raise ProvenanceError(f"cannot lstat untracked source {name!r}: {error}") from error

    if stat.S_ISLNK(status.st_mode):
        try:
            target_bytes = os.readlink(os.fsencode(path))
        except OSError as error:
            raise ProvenanceError(
                f"cannot read untracked symlink {name!r}: {error}"
            ) from error
        link_digest = hashlib.sha256(b"symlink\0" + target_bytes).hexdigest()
        result: dict[str, Any] = {
            "path": name,
            "type": "symlink",
            "mode": stat.S_IMODE(status.st_mode),
            "target_hex": target_bytes.hex(),
            "bytes": len(target_bytes),
            "sha256": link_digest,
        }
        try:
            result["target"] = target_bytes.decode("utf-8")
        except UnicodeDecodeError:
            pass
        return result
    if not stat.S_ISREG(status.st_mode):
        raise ProvenanceError(
            f"untracked source is not a regular file or symlink: {name!r}"
        )

    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ProvenanceError(f"cannot hash untracked source {name!r}: {error}") from error
    return {
        "path": name,
        "type": "regular",
        "mode": stat.S_IMODE(status.st_mode),
        "bytes": status.st_size,
        "sha256": digest.hexdigest(),
    }


def tree_sha256(root: Path, suffixes: tuple[str, ...]) -> dict[str, Any] | None:
    """Hash matching files in a tree without listing every digest."""
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    try:
        candidates = sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.name.endswith(suffixes)
        )
        for path in candidates:
            relative = path.relative_to(root).as_posix().encode()
            entry_digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    entry_digest.update(block)
                    total_bytes += len(block)
            digest.update(relative)
            digest.update(b"\0")
            digest.update(entry_digest.digest())
            file_count += 1
        return {
            "path": str(root),
            "suffixes": list(suffixes),
            "files": file_count,
            "bytes": total_bytes,
            "sha256": digest.hexdigest(),
        }
    except OSError as error:
        return {"path": str(root), "error": type(error).__name__, "detail": str(error)}


def git_repository(path: Path) -> tuple[dict[str, Any], bytes]:
    info: dict[str, Any] = {"path": str(path)}
    if not (path / ".git").exists():
        raise ProvenanceError(f"required source tree is not a git repository: {path}")

    info["available"] = True
    for name, argv in {
        "head": ["git", "rev-parse", "HEAD"],
        "branch": ["git", "branch", "--show-current"],
        "describe": ["git", "describe", "--always", "--dirty", "--tags"],
        "status": ["git", "status", "--short", "--untracked-files=all"],
        "submodules": ["git", "submodule", "status", "--recursive"],
    }.items():
        result = command(argv, path)
        if result.get("returncode") != 0:
            raise ProvenanceError(
                f"required git {name} capture failed in {path}: {result}"
            )
        info[name] = result.get("stdout", "")

    patch_argv = [
        "git", "diff", "--binary", "--no-ext-diff", "--no-textconv",
        "HEAD", "--",
    ]
    patch_result = command_bytes(
        patch_argv, path
    )
    if patch_result.get("returncode") != 0:
        raise ProvenanceError(f"required git diff capture failed in {path}: {patch_result}")
    patches: list[bytes] = [patch_result.get("stdout", b"")]

    # git diff omits untracked source.  Record both hashes and apply-able
    # no-index patches so a dirty experimental tree is still reconstructible.
    untracked_result = command_bytes(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], path
    )
    untracked: list[dict[str, Any]] = []
    if untracked_result.get("returncode") != 0:
        raise ProvenanceError(
            f"required untracked-source capture failed in {path}: {untracked_result}"
        )
    for raw_name in untracked_result.get("stdout", b"").split(b"\0"):
        if not raw_name:
            continue
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProvenanceError(
                f"untracked git path is not UTF-8 in {path}: {raw_name.hex()}"
            ) from error
        untracked.append(untracked_digest(path, name))
        item_argv = [
            "git", "diff", "--binary", "--no-ext-diff", "--no-textconv",
            "--no-index", "--", "/dev/null", name,
        ]
        item_patch = command_bytes(
            item_argv, path,
        )
        if item_patch.get("returncode") not in (0, 1):
            raise ProvenanceError(
                f"cannot patch untracked source {name!r} in {path}: {item_patch}"
            )
        patches.append(item_patch.get("stdout", b""))
    info["untracked_files"] = untracked
    info["dirty"] = bool(info.get("status"))
    return info, b"\n".join(part for part in patches if part)


def git_committed_series(
    path: Path, base_ref: str, expected_head: str | None = None
) -> tuple[dict[str, Any], bytes]:
    """Describe and export commits after the merge-base with base_ref."""
    details: dict[str, Any] = {"requested_base_ref": base_ref}
    head = command(["git", "rev-parse", "HEAD"], path)
    if head.get("returncode") != 0 or not head.get("stdout"):
        raise ProvenanceError(f"cannot resolve kernel HEAD: {head}")
    head_commit = head["stdout"]
    if expected_head is not None and head_commit != expected_head:
        raise ProvenanceError(
            f"kernel HEAD changed before committed-series capture: "
            f"expected {expected_head}, found {head_commit}"
        )
    resolved = command(
        ["git", "rev-parse", "--verify", "--end-of-options",
         f"{base_ref}^{{commit}}"],
        path,
    )
    if resolved.get("returncode") != 0:
        raise ProvenanceError(
            f"cannot resolve required kernel base ref {base_ref!r}: {resolved}"
        )

    merge_base = command(
        ["git", "merge-base", head_commit, resolved["stdout"]], path
    )
    if merge_base.get("returncode") != 0 or not merge_base.get("stdout"):
        raise ProvenanceError(
            f"cannot find kernel merge-base for {base_ref!r}: {merge_base}"
        )

    base_commit = merge_base["stdout"]
    revision_range = f"{base_commit}..{head_commit}"
    log = command(
        ["git", "log", "--reverse", "--format=%H%x09%P%x09%s", revision_range],
        path,
    )
    if log.get("returncode") != 0:
        raise ProvenanceError(f"cannot capture committed kernel log: {log}")
    commits = log.get("stdout", "").splitlines()
    series = command_bytes(
        ["git", "format-patch", "--stdout", "--binary", "--no-signature",
         revision_range],
        path,
    )
    details.update(
        {
            "available": True,
            "resolved_base_ref": resolved["stdout"],
            "base_commit": base_commit,
            "revision_range": revision_range,
            "commits": commits,
        }
    )
    if series.get("returncode") != 0:
        raise ProvenanceError(f"cannot capture committed kernel patch series: {series}")
    details["head_commit"] = head_commit
    return details, series.get("stdout", b"")


def boot_artifact_paths(kernel_tree: Path) -> list[Path]:
    paths = [
        kernel_tree / ".config",
        kernel_tree / "arch/arm64/boot/Image",
        kernel_tree / "arch/arm64/boot/Image.gz",
        kernel_tree / "arch/arm64/boot/dts/broadcom/bcm2712-rpi-5-b.dtb",
        Path("/sys/kernel/btf/vmlinux"),
        Path("/boot/firmware/config.txt"),
        Path("/boot/firmware/cmdline.txt"),
        Path("/boot/firmware/bcm2712-rpi-5-b.dtb"),
    ]

    boot_config = read_text(Path("/boot/firmware/config.txt")) or ""
    for line in boot_config.splitlines():
        setting = line.strip()
        if setting.startswith("kernel="):
            filename = setting.partition("=")[2].strip()
            if filename and Path(filename).name == filename:
                paths.append(Path("/boot/firmware") / filename)

    module_root = Path("/lib/modules") / os.uname().release
    if module_root.is_dir():
        paths.extend(sorted(module_root.glob("modules.*")))
    return paths


def interface_state(name: str) -> dict[str, Any]:
    base = Path("/sys/class/net") / name
    state: dict[str, Any] = {"name": name, "exists": base.exists()}
    if not base.exists():
        return state
    for item in ("ifindex", "mtu", "operstate", "address", "carrier"):
        state[item] = read_text(base / item)
    for item in ("device/driver", "device/ptp"):
        try:
            state[item.replace("/", "_")] = os.path.realpath(base / item)
        except OSError:
            state[item.replace("/", "_")] = None
    queues = base / "queues"
    state["queues"] = sorted(entry.name for entry in queues.iterdir()) if queues.exists() else []
    state["ip_link"] = command(["ip", "-details", "link", "show", "dev", name])
    state["ip_address"] = command(["ip", "address", "show", "dev", name])
    state["ethtool_driver"] = command(["ethtool", "-i", name])
    state["ethtool_features"] = command(["ethtool", "-k", name])
    state["ethtool_timestamping"] = command(["ethtool", "-T", name])
    state["ethtool_stats"] = command(["ethtool", "-S", name])
    return state


def exclusive_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def payload_digest(payload: bytes) -> dict[str, int | str]:
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def parse_setting(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise argparse.ArgumentTypeError("--set requires KEY=VALUE")
    key, value = text.split("=", 1)
    if not key or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in key):
        raise argparse.ArgumentTypeError("manifest setting key is empty or unsafe")
    return key, value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path, help="existing unique run directory")
    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--project-tree", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--kernel-tree", type=Path, default=DEFAULT_KERNEL_TREE)
    parser.add_argument("--kernel-base-ref", default="rpi-6.18.y",
                        help="ref whose merge-base anchors the committed kernel patch series")
    parser.add_argument("--run-id", help="defaults to output directory basename")
    parser.add_argument("--set", action="append", default=[], type=parse_setting,
                        metavar="KEY=VALUE", help="record an experiment parameter")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    if not output_dir.is_dir():
        parser.error("output_dir must be an existing directory")
    output_paths = {
        "manifest": output_dir / "manifest.json",
        "project_patch": output_dir / "project_worktree.patch",
        "kernel_patch": output_dir / "kernel_worktree.patch",
        "kernel_commits": output_dir / "kernel_commits.patch",
    }
    for output_path in output_paths.values():
        if output_path.exists() or output_path.is_symlink():
            parser.error(f"refusing to overwrite {output_path}")

    run_id = args.run_id or output_dir.name
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        parser.error(
            "run_id must start with an ASCII alphanumeric and contain only "
            "ASCII alphanumerics, '.', '_', or '-' (maximum 128 characters)"
        )
    parameters: dict[str, str] = {}
    for key, value in args.set:
        if key in parameters:
            parser.error(f"duplicate --set key: {key}")
        parameters[key] = value

    project_tree = args.project_tree.resolve()
    kernel_tree = args.kernel_tree.resolve()
    try:
        project_info, project_patch = git_repository(project_tree)
        kernel_info, kernel_patch = git_repository(kernel_tree)
        kernel_series_info, kernel_commits_patch = git_committed_series(
            kernel_tree, args.kernel_base_ref, kernel_info["head"]
        )
        kernel_info["committed_series"] = kernel_series_info
        artifacts = []
        artifact_candidates = boot_artifact_paths(kernel_tree) + [
            project_tree / "BPF/xdp_kern.o",
            project_tree / "BPF/xdp_loader",
            project_tree / "BPF/afxdp_recv",
            project_tree / "udp_socket/bin/sender",
            project_tree / "udp_socket/bin/receiver",
            project_tree / "udp_socket/bin/phc_calibrate",
        ]
        seen_artifacts: set[Path] = set()
        for candidate in artifact_candidates:
            candidate = Path(os.path.abspath(candidate))
            if candidate in seen_artifacts:
                continue
            seen_artifacts.add(candidate)
            digest = sha256(candidate)
            if digest:
                artifacts.append(digest)
        tree_hashes = []
        for root, suffixes in (
            (Path("/lib/modules") / os.uname().release,
             (".ko", ".ko.xz", ".ko.zst")),
            (Path("/boot/firmware/overlays"), (".dtbo",)),
        ):
            digest = tree_sha256(root.resolve(), suffixes)
            if digest:
                tree_hashes.append(digest)

    except ProvenanceError as error:
        print(f"cannot capture required source provenance: {error}", file=sys.stderr)
        return 1

    interrupts = read_text(Path("/proc/interrupts")) or ""
    matching_interrupts = [
        line for line in interrupts.splitlines()
        if args.interface.lower() in line.lower() or "macb" in line.lower()
        or "gem" in line.lower()
    ]
    versions = {}
    for name, argv in {
        "cc": ["cc", "--version"],
        "clang": ["clang", "--version"],
        "bpftool": ["bpftool", "version"],
        "ethtool": ["ethtool", "--version"],
        "iproute2": ["ip", "-Version"],
        "ptp4l": ["ptp4l", "-v"],
        "stress_ng": ["stress-ng", "--version"],
        "iperf3": ["iperf3", "--version"],
        "python": [sys.executable, "--version"],
    }.items():
        versions[name] = command(argv)

    manifest = {
        "manifest_version": 1,
        "run_id": run_id,
        "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "parameters": parameters,
        "system": {
            "uname": command(["uname", "-a"]),
            "kernel_release": read_text(Path("/proc/sys/kernel/osrelease")),
            "cmdline": read_text(Path("/proc/cmdline")),
            "boot_config_txt": read_text(Path("/boot/firmware/config.txt")),
            "boot_cmdline_txt": read_text(Path("/boot/firmware/cmdline.txt")),
            "realtime": read_text(Path("/sys/kernel/realtime")),
            "cpu_online": read_text(Path("/sys/devices/system/cpu/online")),
            "cpu_isolated": read_text(Path("/sys/devices/system/cpu/isolated")),
            "nohz_full": read_text(Path("/sys/devices/system/cpu/nohz_full")),
            "irq_lines": matching_interrupts,
        },
        "interface": interface_state(args.interface),
        "project_repository": project_info,
        "kernel_repository": kernel_info,
        "artifact_hashes": artifacts,
        "artifact_tree_hashes": tree_hashes,
        "tool_versions": versions,
    }

    if project_patch:
        manifest["project_repository"]["worktree_patch"] = (
            output_paths["project_patch"].name
        )
        manifest["project_repository"]["worktree_patch_digest"] = (
            payload_digest(project_patch)
        )
    if kernel_patch:
        manifest["kernel_repository"]["worktree_patch"] = (
            output_paths["kernel_patch"].name
        )
        manifest["kernel_repository"]["worktree_patch_digest"] = (
            payload_digest(kernel_patch)
        )
    if kernel_commits_patch:
        manifest["kernel_repository"]["committed_series"]["patch"] = (
            output_paths["kernel_commits"].name
        )
        manifest["kernel_repository"]["committed_series"]["patch_digest"] = (
            payload_digest(kernel_commits_patch)
        )
    encoded = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()

    created: list[Path] = []
    try:
        if project_patch:
            exclusive_write(output_paths["project_patch"], project_patch)
            created.append(output_paths["project_patch"])
        if kernel_patch:
            exclusive_write(output_paths["kernel_patch"], kernel_patch)
            created.append(output_paths["kernel_patch"])
        if kernel_commits_patch:
            exclusive_write(
                output_paths["kernel_commits"], kernel_commits_patch
            )
            created.append(output_paths["kernel_commits"])
        exclusive_write(output_paths["manifest"], encoded)
        created.append(output_paths["manifest"])
        fsync_directory(output_dir)
    except BaseException as error:
        for created_path in created:
            try:
                created_path.unlink()
            except OSError:
                pass
        try:
            fsync_directory(output_dir)
        except OSError:
            pass
        if isinstance(error, OSError):
            print(f"cannot write manifest: {error}", file=sys.stderr)
            return 1
        raise

    print(output_paths["manifest"])
    if project_info.get("dirty") or kernel_info.get("dirty"):
        print("warning: at least one source tree is dirty; inspect status and saved patches",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
