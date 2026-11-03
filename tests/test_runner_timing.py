"""Exercise the runner's real safety-timer calculation without device changes."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('cc'),
                     'Linux C compiler required')
class RunnerTimingTests(unittest.TestCase):
    def test_load_outlives_long_settle_and_bounded_shutdown(self):
        with tempfile.TemporaryDirectory(prefix='ace-timing-') as temporary:
            directory = Path(temporary)
            source = directory / 'timing.c'
            source.write_text('''#define main ace_runner_main
#include "ace-run.c"
#undef main
int main(void) {
    struct run_config c = {.duration_ms=1000, .warmup_ms=0,
        .start_delay_ms=250, .ready_ms=5000, .stop_ms=5000,
        .settle_ms=30000, .drain_ms=100};
    printf("%llu\\n", (unsigned long long)load_lifetime_seconds(&c));
    c.duration_ms=86400000; c.warmup_ms=3600000;
    c.ready_ms=c.stop_ms=600000; c.settle_ms=c.drain_ms=60000;
    printf("%llu\\n", (unsigned long long)load_lifetime_seconds(&c));
    return 0;
}
''', encoding='utf-8')
            binary = directory / 'timing'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-I'+str(ROOT/'experiment'), '-I'+str(ROOT/'include'),
                            str(source), str(ROOT/'experiment/run_config.c'),
                            str(ROOT/'experiment/ace_manifest.c'), '-o', str(binary)],
                           check=True, capture_output=True, text=True)
            values = list(map(int, subprocess.check_output([str(binary)], text=True).split()))
            # The previous 17-second timer expired during the 30-second settle.
            self.assertGreater(values[0], 31)
            self.assertGreaterEqual(values[0]*1000, 1000+250+30000+100+3*5000)
            self.assertGreater(values[1], 90000)


if __name__ == '__main__':
    unittest.main()
