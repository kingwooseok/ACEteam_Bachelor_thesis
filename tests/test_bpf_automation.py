#!/usr/bin/env python3
"""Offline policy/deadline tests and real binary CLI validation (no NIC attach)."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BpfRunControlTests(unittest.TestCase):
    def test_receiver_completion_and_flush_barrier(self):
        compiler = shutil.which(os.environ.get("CC", "cc"))
        pkg_config = shutil.which("pkg-config")
        if not compiler or not pkg_config:
            self.skipTest("C compiler and pkg-config required")
        flags = subprocess.run([pkg_config, "--cflags", "--libs", "libbpf", "libxdp"],
                               text=True, capture_output=True)
        if flags.returncode:
            self.skipTest("libbpf/libxdp development packages required")
        # Include the production implementation: no mock of signal/pipe logic.
        source = r'''#define main afxdp_program_main
#include "BPF/afxdp_recv.c"
#undef main
#include <assert.h>
#include <sys/wait.h>
int main(void) {
    int completion[2], output[2], status;
    unsigned char value;
    pid_t child;
    sigset_t blocked, original;
    alarm(5);
    assert(signal(SIGPIPE, SIG_IGN) != SIG_ERR);
    assert(signal(SIGUSR1, on_flush_signal) != SIG_ERR);
    assert(pipe(completion) == 0 && pipe(output) == 0);
    /* Exercise a blocked inherited SIGUSR1 mask as well as the notify/wait race. */
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGUSR1);
    assert(sigprocmask(SIG_BLOCK, &blocked, &original) == 0);
    child = fork();
    assert(child >= 0);
    if (!child) {
        alarm(5);
        close(completion[0]); close(output[0]);
        stop = 1; /* TERM already ended capture; it must not release storage I/O. */
        assert(notify_completion(&completion[1], false) == 0);
        assert(completion[1] == -1);
        assert(wait_flush_release() == 0);
        assert(write(output[1], "F", 1) == 1);
        _exit(0);
    }
    close(completion[1]); close(output[1]);
    assert(read(completion[0], &value, 1) == 1 && value == 0);
    assert(read(completion[0], &value, 1) == 0);
    {
        struct pollfd pending = { .fd = output[0], .events = POLLIN };
        assert(poll(&pending, 1, 50) == 0);
        assert(kill(child, SIGUSR1) == 0);
        assert(poll(&pending, 1, 1000) == 1);
    }
    assert(read(output[0], &value, 1) == 1 && value == 'F');
    assert(waitpid(child, &status, 0) == child);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    close(completion[0]); close(output[0]);
    assert(sigprocmask(SIG_SETMASK, &original, NULL) == 0);
    /* An early cleanup release stays latched until drain completion. */
    flush_requested = 0;
    assert(raise(SIGUSR1) == 0);
    assert(wait_flush_release() == 0 && flush_requested == 1);
    assert(pipe(completion) == 0);
    assert(notify_completion(&completion[1], true) == 0);
    assert(read(completion[0], &value, 1) == 1 && value == 1);
    close(completion[0]);
    /* A dead coordinator cannot terminate the receiver through SIGPIPE. */
    assert(pipe(completion) == 0);
    close(completion[0]);
    assert(notify_completion(&completion[1], false) == -1);
    assert(errno == EPIPE && completion[1] == -1);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "barrier.c"
            exe = Path(directory) / "barrier"
            src.write_text(source)
            subprocess.run([compiler, "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT), str(src), "-o", str(exe),
                            *shlex.split(flags.stdout)], check=True)
            subprocess.run([str(exe)], check=True, timeout=8)

    def test_mode_and_bounded_drain(self):
        compiler = shutil.which(os.environ.get("CC", "cc"))
        if not compiler:
            self.skipTest("C compiler unavailable")
        source = r'''#include <assert.h>
#include "BPF/metadata.h"
#include "BPF/afxdp_run_control.h"
int main(void) {
    struct ace_xsk_drain d = {0};
    for (unsigned short p = ACE_PATH_PASS; p <= ACE_PATH_XSK; ++p) {
        assert(ace_effective_requested_path(ACE_FORWARD_MIXED, p) == p);
        assert(ace_effective_requested_path(ACE_FORWARD_ALL_PASS, p) == ACE_PATH_PASS);
        assert(ace_effective_requested_path(ACE_FORWARD_PASS_XSK, p) ==
               (p == ACE_PATH_XSK ? ACE_PATH_XSK : ACE_PATH_PASS));
    }
    assert(ace_xsk_drain_poll_ms(&d, 1000000000) == 1000);
    ace_xsk_drain_begin(&d, 1000000000, 100);
    assert(ace_xsk_drain_poll_ms(&d, 1000000000) == 100);
    assert(ace_xsk_drain_poll_ms(&d, 1099999999) == 1);
    assert(ace_xsk_drain_poll_ms(&d, 1100000000) == -1);
    /* Repeated signals and ongoing packets cannot extend the deadline. */
    ace_xsk_drain_begin(&d, 1099000000, 100);
    assert(d.deadline_ns == 1100000000);
    assert(ace_xsk_drain_poll_ms(&d, 1200000000) == -1);
    d = (struct ace_xsk_drain){0};
    ace_xsk_drain_begin(&d, 1, 0);
    assert(ace_xsk_drain_poll_ms(&d, 1) == -1);
    d = (struct ace_xsk_drain){0};
    ace_xsk_drain_begin(&d, 1, 60000);
    assert(ace_xsk_drain_poll_ms(&d, 1) == 1000);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "control.c"
            exe = Path(directory) / "control"
            src.write_text(source)
            subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT), str(src), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

    def test_loader_configures_cpumap_only_for_mixed(self):
        compiler = shutil.which(os.environ.get("CC", "cc"))
        pkg_config = shutil.which("pkg-config")
        if not compiler or not pkg_config or not (ROOT / "BPF/xdp_kern.skel.h").exists():
            self.skipTest("C compiler, libbpf and generated BPF skeleton required")
        flags = subprocess.run([pkg_config, "--cflags", "--libs", "libbpf"],
                               text=True, capture_output=True)
        if flags.returncode:
            self.skipTest("libbpf development package required")
        # Exercise the loader's real parser and map setup without loading BPF,
        # attaching to a NIC, or creating a kernel forwarding thread.
        source = r'''#define main loader_program_main
#define bpf_map_update_elem fixture_map_update
#define bpf_map__fd fixture_map_fd
#define bpf_program__fd fixture_program_fd
#include "BPF/xdp_loader.c"
#undef main
#include <assert.h>
static int cpumap_updates, runtime_updates, remote_lookups;
static __u32 expected_flags;
int fixture_map_fd(const struct bpf_map *map) {
    return (int)(uintptr_t)map;
}
int fixture_program_fd(const struct bpf_program *program) {
    assert((uintptr_t)program == 77);
    remote_lookups++;
    return 77;
}
int fixture_map_update(int fd, const void *key, const void *value, __u64 flags) {
    assert(flags == BPF_ANY);
    if (fd == 21) {
        const struct bpf_cpumap_val *entry = value;
        assert(*(__u32 *)key == ACE_XDP_RT_CPU);
        assert(entry->qsize == ACE_XDP_CPU_MAP_QUEUE_SIZE);
        assert(entry->bpf_prog.fd == 77);
        cpumap_updates++;
    } else {
        const struct ace_runtime_config *runtime = value;
        assert(fd == 22 && *(__u32 *)key == 0);
        assert(runtime->flags == expected_flags);
        runtime_updates++;
    }
    return 0;
}
int main(void) {
    struct xdp_kern skel = {0};
    const char *modes[] = {"mixed", "all-pass", "pass-xsk"};
    const __u32 mode_flags[] = {0, ACE_RUNTIME_F_ALL_PASS, ACE_RUNTIME_F_PASS_XSK};
    skel.maps.cpu_map = (struct bpf_map *)(uintptr_t)21;
    skel.maps.runtime_config = (struct bpf_map *)(uintptr_t)22;
    skel.progs.xdp_cpumap_measure = (struct bpf_program *)(uintptr_t)77;
    for (__u32 mode = 0; mode < 3; mode++) {
        char *argv[] = {"loader", "--output-dir", "/unused", "--mode", (char *)modes[mode], NULL};
        struct loader_options options = {0};
        struct ace_runtime_config runtime = { .flags = mode_flags[mode] };
        optind = 0;
        assert(parse_options(5, argv, &options) == 0);
        assert(options.mode == mode);
        cpumap_updates = runtime_updates = remote_lookups = 0;
        expected_flags = runtime.flags;
        assert(configure_maps(&skel, &runtime) == 0);
        assert(runtime_updates == 1);
        assert(cpumap_updates == (mode == ACE_FORWARD_MIXED));
        assert(remote_lookups == (mode == ACE_FORWARD_MIXED));
    }
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "loader-modes.c"
            exe = Path(directory) / "loader-modes"
            src.write_text(source)
            subprocess.run([compiler, "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT), str(src), "-o", str(exe),
                            *shlex.split(flags.stdout)], check=True)
            subprocess.run([str(exe)], check=True, timeout=5)

    def binary(self, name):
        result = ROOT / "BPF" / name
        if not result.is_file() or not os.access(result, os.X_OK):
            self.skipTest(f"build BPF/{name} before CLI tests")
        return str(result)

    def test_loader_rejects_bad_mode_before_attach(self):
        loader = self.binary("xdp_loader")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([loader, "--output-dir", directory,
                                     "--mode", "invalid"], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid --mode", result.stderr)

    def test_loader_help_includes_modes_and_readiness(self):
        result = subprocess.run([self.binary("xdp_loader"), "--help"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--mode", result.stderr)
        self.assertIn("pass-xsk", result.stderr)
        self.assertIn("--ready-file", result.stderr)

    def test_recovery_rejects_ready_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([self.binary("xdp_loader"), "--output-dir", directory,
                                     "--recover", "--ready-file", directory + "/ready.json"],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--ready-file is not valid with --recover", result.stderr)
            self.assertFalse((Path(directory) / "ready.json").exists())

    def test_receiver_rejects_invalid_drain(self):
        receiver = self.binary("afxdp_recv")
        with tempfile.TemporaryDirectory() as directory:
            for value in ("-1", "60001", "invalid"):
                with self.subTest(value=value):
                    result = subprocess.run([receiver, "--output", directory + "/rx.csv",
                                             "--drain-ms", value], capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((Path(directory) / "rx.csv").exists())

    def test_receiver_completion_fd_validation(self):
        receiver = self.binary("afxdp_recv")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rx.csv"
            for value in ("-1", "2", "2147483648", "invalid"):
                with self.subTest(value=value):
                    result = subprocess.run([receiver, "--output", str(output),
                                             "--completion-fd", value], capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())
            target = Path(directory) / "not-a-pipe"
            with target.open("wb") as stream:
                result = subprocess.run([receiver, "--output", str(output), "--completion-fd",
                                         str(stream.fileno())], pass_fds=(stream.fileno(),),
                                        text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("inherited writable pipe", result.stderr)
            self.assertEqual(target.read_bytes(), b"")
            self.assertFalse(output.exists())

    def test_receiver_stats_collision_preserves_file(self):
        receiver = self.binary("afxdp_recv")
        if not Path("/sys/class/net/lo/queues/rx-0").exists():
            self.skipTest("loopback RX queue unavailable")
        with tempfile.TemporaryDirectory() as directory:
            stats = Path(directory) / "stats.json"
            stats.write_text("existing-run")
            result = subprocess.run([receiver, "--output", directory + "/rx.csv",
                                     "--stats-json", str(stats), "lo", "0"],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot create statistics file", result.stderr)
            self.assertEqual(stats.read_text(), "existing-run")


if __name__ == "__main__":
    unittest.main()
