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
    def test_nic_loader_and_server_outlive_peer_network_ack(self):
        with tempfile.TemporaryDirectory(prefix='ace-shutdown-') as temporary:
            directory = Path(temporary)
            source = directory / 'shutdown.c'
            source.write_text(r'''#define main ace_runner_main
#include "ace-run.c"
#undef main
#include <assert.h>
static int event_fd;
static char stopped_child;
static void record_stop(int sig) {
    (void)sig;
    (void)write(event_fd, &stopped_child, 1);
    _exit(0);
}
static pid_t worker(char name, int ready_fd) {
    pid_t pid=fork();
    assert(pid>=0);
    if (!pid) {
        setpgid(0,0); stopped_child=name;
        signal(SIGTERM,record_stop);
        assert(write(ready_fd,"R",1)==1);
        for (;;) pause();
    }
    setpgid(pid,pid);
    return pid;
}
int main(void) {
    int control[2],events[2],ready[2];
    assert(!socketpair(AF_UNIX,SOCK_STREAM,0,control));
    assert(!pipe(events) && !pipe(ready));
    event_fd=events[1];
    struct context x={.fd=control[0],.children=2};
    x.c.stop_ms=2000;
    x.child[0].pid=worker('L',ready[1]);
    strcpy(x.child[0].name,"xdp-loader"); x.child[0].persistent=1;
    x.child[1].pid=worker('M',ready[1]);
    strcpy(x.child[1].name,"monitor"); x.child[1].persistent=1;
    for (int i=0;i<2;i++) { char byte; assert(read(ready[0],&byte,1)==1); }
    pid_t peer=fork(); assert(peer>=0);
    if (!peer) {
        struct context tx={.fd=control[1]};
        assert(!message(&tx,DONE,0,2000));
        assert(!kill(x.child[0].pid,0) && !kill(x.child[1].pid,0));
        struct timespec delay={.tv_nsec=100000000}; nanosleep(&delay,NULL);
        assert(!kill(x.child[0].pid,0) && !kill(x.child[1].pid,0));
        assert(write(event_fd,"A",1)==1);
        assert(!message(&tx,ACK,1,2000));
        _exit(0);
    }
    assert(!stop_receiver_pipeline(&x,1,0));
    int status; assert(waitpid(peer,&status,0)==peer);
    assert(WIFEXITED(status) && WEXITSTATUS(status)==0);
    char order[3]; assert(read(events[0],order,sizeof(order))==3);
    assert(!memcmp(order,"AML",3));
    assert(x.child[0].done && x.child[1].done);
    return 0;
}
''', encoding='utf-8')
            binary = directory / 'shutdown'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-I'+str(ROOT/'experiment'), '-I'+str(ROOT/'include'),
                            str(source), str(ROOT/'experiment/run_config.c'),
                            str(ROOT/'experiment/ace_manifest.c'), '-o', str(binary)],
                           check=True, capture_output=True, text=True)
            subprocess.run([str(binary)], check=True, timeout=8,
                           capture_output=True, text=True)

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
