#define _GNU_SOURCE
#include "run_config.h"
#include "ace_manifest.h"
#include "ace_run_control.h"
#include <arpa/inet.h>
#include <endian.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdarg.h>
#include <sys/random.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>

/* Direct Pi-to-Pi phase control. There is no control traffic during capture. */
enum { HELLO=1, RX_READY, TX_READY, START, STARTED, STOP, DONE, ACK, FAIL };
struct frame {
	uint32_t magic, version, type, index;
	unsigned char nonce[16];
	char digest[65], run_id[128];
	unsigned char reserved[31];
};
_Static_assert(sizeof(struct frame)==256,"control wire size");
struct child {
	pid_t pid;
	int done, status, stopping, persistent, completion_fd, measurement_done, barrier;
	char name[32], ready[4096], staging[4096];
};
struct context {
	struct run_config c;
	struct child child[16];
	unsigned children, index;
	int fd, receiver, owns_run;
	unsigned char nonce[16];
	char suite[4096], run_dir[4096], run_id[128], mode[16];
	unsigned repeat;
};
static volatile sig_atomic_t stopping;
static void signal_stop(int sig) { (void)sig; stopping=1; }
static void child_event(int sig) { (void)sig; }
static int pathf(char *out,size_t size,const char *format,...)
{
	va_list args; va_start(args,format); int n=vsnprintf(out,size,format,args); va_end(args);
	if (n<0 || (size_t)n>=size) { errno=ENAMETOOLONG; return -1; } return 0;
}
static int mkdir_parents(const char *path)
{
	char copy[4096]; if (pathf(copy,sizeof(copy),"%s",path)) return -1;
	for (char *p=copy+1;*p;p++) if (*p=='/') { *p=0; if (mkdir(copy,0755) && errno!=EEXIST) return -1; *p='/'; }
	if (mkdir(copy,0755) && errno!=EEXIST) return -1;
	struct stat st; if (stat(copy,&st) || !S_ISDIR(st.st_mode)) { errno=ENOTDIR; return -1; } return 0;
}
static int scheduler(int cpu,int policy,int priority)
{
	if (cpu>=0) { cpu_set_t set; CPU_ZERO(&set); CPU_SET(cpu,&set); if (sched_setaffinity(0,sizeof(set),&set)) return -1; }
	struct sched_param p={.sched_priority=priority}; return sched_setscheduler(0,policy,&p);
}
static int reap(struct context *x)
{
	int failed=0;
	for (unsigned i=0;i<x->children;i++) {
		struct child *c=&x->child[i];
		if (!c->done) { pid_t p=waitpid(c->pid,&c->status,WNOHANG); if (p==c->pid) c->done=1; else if (p<0 && errno!=EINTR) { c->done=1; c->status=1<<8; } }
		if (c->done && !c->stopping && (c->persistent || !WIFEXITED(c->status) || WEXITSTATUS(c->status))) {
			fprintf(stderr,"child failed: %s (status=%d)\n",c->name,c->status); failed=1;
		}
	}
	return failed ? -1 : 0;
}
static int spawn_pipe(struct context *x,const char *name,const char *ready,int cpu,int policy,int priority,int persistent,char *const args[],int notify_fd)
{
	if (x->children>=16) { errno=E2BIG; return -1; }
	char log[4096]; if (pathf(log,sizeof(log),"%s/%s.log",x->run_dir,name)) return -1;
	int fd=open(log,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0644); if (fd<0) return -1;
	pid_t pid=fork();
	if (pid==0) {
		setpgid(0,0); signal(SIGINT,SIG_DFL); signal(SIGTERM,SIG_DFL); signal(SIGCHLD,SIG_DFL);
		dup2(fd,STDOUT_FILENO); dup2(fd,STDERR_FILENO); close(fd);
		if (x->fd>=0) close(x->fd);
		if (notify_fd>=0 && fcntl(notify_fd,F_SETFD,0)) { perror("completion pipe"); _exit(126); }
		if (scheduler(cpu,policy,priority)) { perror("child scheduler"); _exit(126); }
		execvp(args[0],args); perror("exec child"); _exit(127);
	}
	close(fd); if (pid<0) return -1;
	setpgid(pid,pid);
	unsigned index=x->children++; struct child *c=&x->child[index]; memset(c,0,sizeof(*c));
	c->pid=pid; c->persistent=persistent;
	c->completion_fd=-1; c->barrier=notify_fd>=0;
	pathf(c->name,sizeof(c->name),"%s",name);
	if (ready) pathf(c->ready,sizeof(c->ready),"%s",ready);
	return (int)index;
}
static int spawn(struct context *x,const char *name,const char *ready,int cpu,int policy,int priority,int persistent,char *const args[])
{ return spawn_pipe(x,name,ready,cpu,policy,priority,persistent,args,-1); }
static int pause_ms(struct context *x,uint64_t ms)
{
	int64_t until=ace_mono_ns()+(int64_t)ms*1000000LL;
	while (!stopping) {
		if (reap(x)) return -1;
		int64_t left=until-ace_mono_ns(); if (left<=0) return 0;
		struct timespec ts={.tv_sec=0,.tv_nsec=left>10000000?10000000:(long)left}; nanosleep(&ts,NULL);
	}
	errno=EINTR; return -1;
}
static int ready_child(struct context *x,int index)
{
	int64_t until=ace_mono_ns()+(int64_t)x->c.ready_ms*1000000LL;
	while (!stopping) {
		if (reap(x)) return -1;
		struct child *c=&x->child[index];
		if (c->done) { errno=ECHILD; return -1; }
		struct stat st;
		if (!lstat(c->ready,&st)) { if (!S_ISREG(st.st_mode) || st.st_size<=0) { errno=EINVAL; return -1; } return 0; }
		if (errno!=ENOENT) return -1;
		if (ace_mono_ns()>=until) { errno=ETIMEDOUT; return -1; }
		if (pause_ms(x,10)) return -1;
	}
	errno=EINTR; return -1;
}
static void request_stop(struct context *x,int index)
{
	if (index<0) return;
	struct child *c=&x->child[index];
	if (!c->done && !c->stopping) { c->stopping=1; kill(-c->pid,SIGTERM); }
}
static int wait_child(struct context *x,int index,unsigned timeout_ms,int cleanup)
{
	int64_t until=ace_mono_ns()+(int64_t)timeout_ms*1000000LL;
	struct child *c=&x->child[index];
	while (!c->done) {
		if (!cleanup && stopping) { errno=EINTR; return -1; }
		if (reap(x) && !cleanup) return -1;
		if (c->done) break;
		int64_t now=ace_mono_ns();
		if (now>=until) { c->stopping=1; kill(-c->pid,SIGKILL); while (waitpid(c->pid,&c->status,0)<0 && errno==EINTR) {} c->done=1; errno=ETIMEDOUT; return -1; }
		struct timespec ts={.tv_nsec=10000000}; nanosleep(&ts,NULL);
	}
	int external_load=!strcmp(c->name,"monitor") || !strcmp(c->name,"network-load");
	if (WIFEXITED(c->status) && (WEXITSTATUS(c->status)==0 || (external_load && c->stopping && WEXITSTATUS(c->status)==130))) {
		if (WEXITSTATUS(c->status)==0 && *c->staging) { if (unlink(c->staging) && errno!=ENOENT) return -1; c->staging[0]=0; }
		return 0;
	}
	if (external_load && c->stopping && WIFSIGNALED(c->status) && WTERMSIG(c->status)==SIGTERM) return 0;
	errno=ECHILD; return -1;
}
static int stop_child(struct context *x,int index)
{ if (index<0) return 0; request_stop(x,index); return wait_child(x,index,x->c.stop_ms,1); }
static void cleanup_adopted_children(unsigned timeout_ms)
{
	/* Subreaper adoption keeps failed wrapper descendants ours, even if those
	 * tools created their own process group/session. Never scan unrelated PIDs. */
	char path[128]; snprintf(path,sizeof(path),"/proc/self/task/%ld/children",(long)getpid());
	int64_t until=ace_mono_ns()+(int64_t)timeout_ms*1000000LL;
	while (1) {
		FILE *in=fopen(path,"r"); if (!in) return;
		pid_t owned[512]; unsigned count=0; long pid;
		while (count<512 && fscanf(in,"%ld",&pid)==1) if (pid>0) owned[count++]=(pid_t)pid;
		fclose(in); if (!count) return;
		for (unsigned i=0;i<count;i++) kill(owned[i],SIGTERM);
		for (unsigned i=0;i<count;i++) {
			int status; pid_t result;
			do {
				result=waitpid(owned[i],&status,WNOHANG);
				if (result<0 && errno==EINTR) continue;
				if (result || ace_mono_ns()>=until) break;
				struct timespec ts={.tv_nsec=10000000}; nanosleep(&ts,NULL);
			} while (1);
			if (!result) { kill(owned[i],SIGKILL); while (waitpid(owned[i],&status,0)<0 && errno==EINTR) {} }
		}
	}
}
static void cleanup_children(struct context *x)
{
	/* Spawn order: loader, receivers, monitor. Receivers drain before the loader. */
	for (unsigned i=0;i<x->children;i++) if (strcmp(x->child[i].name,"xdp-loader")) request_stop(x,(int)i);
	for (unsigned i=0;i<x->children;i++) if (x->child[i].barrier && !x->child[i].done) kill(x->child[i].pid,SIGUSR1);
	for (unsigned i=0;i<x->children;i++) if (strcmp(x->child[i].name,"xdp-loader")) (void)wait_child(x,(int)i,x->c.stop_ms,1);
	for (unsigned i=0;i<x->children;i++) if (!strcmp(x->child[i].name,"xdp-loader")) (void)stop_child(x,(int)i);
	cleanup_adopted_children(x->c.stop_ms);
	for (unsigned i=0;i<x->children;i++) if (x->child[i].completion_fd>=0) { close(x->child[i].completion_fd); x->child[i].completion_fd=-1; }
}
static int transfer(struct context *x,void *buffer,size_t length,int writing,unsigned timeout_ms,int ignore_stop)
{
	unsigned char *cursor=buffer; int64_t until=ace_mono_ns()+(int64_t)timeout_ms*1000000LL;
	while (length) {
		sigset_t blocked,previous; sigemptyset(&blocked); sigaddset(&blocked,SIGCHLD); sigaddset(&blocked,SIGINT); sigaddset(&blocked,SIGTERM);
		if (sigprocmask(SIG_BLOCK,&blocked,&previous)) return -1;
		if (!ignore_stop && (stopping || reap(x))) { sigprocmask(SIG_SETMASK,&previous,NULL); errno=EINTR; return -1; }
		int64_t left=until-ace_mono_ns(); if (left<=0) { sigprocmask(SIG_SETMASK,&previous,NULL); errno=ETIMEDOUT; return -1; }
		struct pollfd p={.fd=x->fd,.events=writing?POLLOUT:POLLIN};
		struct timespec timeout={.tv_sec=(time_t)(left/1000000000LL),.tv_nsec=(long)(left%1000000000LL)};
		int result=ppoll(&p,1,&timeout,&previous); int poll_error=errno; sigprocmask(SIG_SETMASK,&previous,NULL); errno=poll_error;
		if (result<0) { if (errno==EINTR) continue; return -1; }
		if (!result) { errno=ETIMEDOUT; return -1; }
		ssize_t n=writing?send(x->fd,cursor,length,MSG_NOSIGNAL):recv(x->fd,cursor,length,0);
		if (n<0) { if (errno==EINTR || errno==EAGAIN) continue; return -1; }
		if (!n) { errno=ECONNRESET; return -1; }
		cursor+=n; length-=(size_t)n;
	}
	return 0;
}
static int message(struct context *x,unsigned type,int writing,unsigned timeout_ms)
{
	struct frame f={0};
	if (writing) {
		f.magic=htonl(0x41434552); f.version=htonl(1); f.type=htonl(type); f.index=htonl(x->index);
		memcpy(f.nonce,x->nonce,16); strcpy(f.digest,x->c.digest); strcpy(f.run_id,x->run_id);
	}
	if (transfer(x,&f,sizeof(f),writing,timeout_ms,type==FAIL)) return -1;
	if (writing) return 0;
	if (ntohl(f.magic)!=0x41434552 || ntohl(f.version)!=1 || ntohl(f.index)!=x->index ||
	    !memchr(f.digest,0,sizeof(f.digest)) || !memchr(f.run_id,0,sizeof(f.run_id)) ||
	    strcmp(f.digest,x->c.digest) || strcmp(f.run_id,x->run_id)) { errno=EPROTO; return -1; }
	for (unsigned i=0;i<sizeof(f.reserved);i++) if (f.reserved[i]) { errno=EPROTO; return -1; }
	if (type==HELLO && !x->receiver) memcpy(x->nonce,f.nonce,16);
	else if (!(type==HELLO && x->receiver) && memcmp(f.nonce,x->nonce,16)) { errno=EPROTO; return -1; }
	if (ntohl(f.type)!=type) { fprintf(stderr,"control phase mismatch: wanted %u received %u\n",type,ntohl(f.type)); errno=EPROTO; return -1; }
	return 0;
}
static int connect_peer(struct context *x)
{
	struct sockaddr_in address={.sin_family=AF_INET,.sin_port=htons(x->c.control_port)};
	inet_pton(AF_INET,x->c.address,&address.sin_addr);
	int fd=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0); if (fd<0) return -1;
	if (x->receiver) {
		int one=1; setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof(one));
		if (bind(fd,(struct sockaddr*)&address,sizeof(address)) || listen(fd,1)) { close(fd); return -1; }
		printf("receiver control ready: %s:%u\n",x->c.address,x->c.control_port); fflush(stdout);
		struct pollfd p={.fd=fd,.events=POLLIN}; int n;
		do { n=poll(&p,1,(int)x->c.ready_ms); } while (n<0 && errno==EINTR && !stopping);
		if (n<=0 || stopping) { close(fd); errno=ETIMEDOUT; return -1; }
		x->fd=accept4(fd,NULL,NULL,SOCK_CLOEXEC); close(fd); if (x->fd<0) return -1;
		if (getrandom(x->nonce,sizeof(x->nonce),0)!=(ssize_t)sizeof(x->nonce)) return -1;
		if (message(x,HELLO,0,x->c.ready_ms) || message(x,HELLO,1,x->c.ready_ms)) return -1;
	} else {
		close(fd); int64_t until=ace_mono_ns()+(int64_t)x->c.ready_ms*1000000LL;
		while (!stopping) {
			fd=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0); if (fd<0) return -1;
			int result=connect(fd,(struct sockaddr*)&address,sizeof(address));
			if (result && errno==EINPROGRESS) { struct pollfd p={.fd=fd,.events=POLLOUT}; if (poll(&p,1,100)>0) { int error=0; socklen_t length=sizeof(error); getsockopt(fd,SOL_SOCKET,SO_ERROR,&error,&length); if (!error) result=0; } }
			if (!result) { int flags=fcntl(fd,F_GETFL); fcntl(fd,F_SETFL,flags&~O_NONBLOCK); x->fd=fd; break; }
			close(fd); if (ace_mono_ns()>=until) { errno=ETIMEDOUT; return -1; } if (pause_ms(x,50)) return -1;
		}
		if (stopping) { errno=EINTR; return -1; }
		if (message(x,HELLO,1,x->c.ready_ms) || message(x,HELLO,0,x->c.ready_ms)) return -1;
	}
	return 0;
}
static int write_run(struct context *x,const char *state)
{
	char destination[4096],temporary[4096];
	if (pathf(destination,sizeof(destination),"%s/run.json",x->run_dir) || pathf(temporary,sizeof(temporary),"%s/run.json.tmp",x->run_dir)) return -1;
	FILE *out=ace_open_exclusive_stream(temporary); if (!out) return -1;
	char *condition=NULL; size_t size=0; FILE *memory=open_memstream(&condition,&size);
	if (!memory) { fclose(out); unlink(temporary); return -1; }
	write_condition(memory,&x->c,x->mode,x->c.duration_ms); fclose(memory);
	fprintf(out,"{\"schema_version\":1,\"run_id\":\"%s\",\"case_id\":\"%s\",\"repeat\":%u,\"role\":\"%s\",\"purpose\":\"%s\",\"state\":\"%s\",\"warmup_s\":%.3f,",x->run_id,x->c.run_id,x->repeat,x->receiver?"receiver":"sender",!strcmp(x->mode,"socket-only")?"smoke":"measurement",state,x->c.warmup_ms/1000.0);
	fputs(condition+1,out); fputc('\n',out); free(condition);
	if (ace_finish_stream(out)) { unlink(temporary); return -1; }
	return rename(temporary,destination);
}
static int marker(struct context *x,const char *name)
{
	char path[4096]; if (pathf(path,sizeof(path),"%s/%s",x->run_dir,name)) return -1;
	FILE *out=ace_open_exclusive_stream(path); if (!out) return -1;
	fprintf(out,"%s\n",x->run_id); return ace_finish_stream(out);
}
static int begin_run(struct context *x)
{
	x->children=0; x->owns_run=0;
	if (pathf(x->run_id,sizeof(x->run_id),"%s-%s-r%04u",x->c.run_id,x->mode,x->repeat) ||
	    pathf(x->run_dir,sizeof(x->run_dir),"%s/runs/%s",x->suite,x->run_id)) return -1;
	if (mkdir(x->run_dir,0755)) return -1;
	x->owns_run=1;
	if (write_run(x,"preparing")) return -1;
	char *condition=NULL; size_t size=0; FILE *out=open_memstream(&condition,&size); if (!out) return -1;
	write_condition(out,&x->c,x->mode,x->c.duration_ms); fclose(out);
	char manifest[4096]; pathf(manifest,sizeof(manifest),"%s/manifest.json",x->run_dir);
	int result=ace_write_manifest(manifest,x->run_id,x->c.run_id,x->c.interface,x->c.repo,condition); free(condition);
	return result;
}
static int calibrate(struct context *x,const char *phase)
{
	char output[4096],name[32]; pathf(name,sizeof(name),"phc-%s",phase); pathf(output,sizeof(output),"%s/%s.csv",x->run_dir,name);
	char *args[]={"./udp_socket/bin/phc_calibrate","--interface",x->c.interface,"--output",output,NULL};
	int child=spawn(x,name,NULL,-1,SCHED_OTHER,0,0,args); if (child<0) return -1;
	return wait_child(x,child,x->c.ready_ms,0);
}
/* 상태 수집은 캡처 준비/완료 경계 밖에서만 실행한다. 두 Pi 모두 파일을
 * 남기되 패킷마다 센서를 읽거나 Python/SSH로 관측하지 않는다. */
static int capture_state(struct context *x,const char *phase)
{
	char name[32],output[4096];
	if (pathf(name,sizeof(name),"system-%s",phase) ||
	    pathf(output,sizeof(output),"%s/%s.txt",x->run_dir,name)) return -1;
	char *args[]={"bash","./monitor/capture_state.sh",x->c.interface,output,NULL};
	int child=spawn(x,name,NULL,-1,SCHED_OTHER,0,0,args); if (child<0) return -1;
	return wait_child(x,child,x->c.ready_ms,0);
}
static int prepare_environment(struct context *x)
{
	if (!strcmp(x->mode,"socket-only")) return 0;
	char cpu[16]; snprintf(cpu,sizeof(cpu),"%u",x->c.rx_irq_cpu);
	char *args[]={"bash","./monitor/prepare_env.sh",x->receiver?"receiver":"sender",
	              x->c.interface,x->c.governor,cpu,NULL};
	int child=spawn(x,"environment-prepare",NULL,-1,SCHED_OTHER,0,0,args); if (child<0) return -1;
	return wait_child(x,child,x->c.ready_ms,0);
}
static int receiver_process(struct context *x,unsigned i)
{
	char name[32],ready[4096],stats[4096],output[4096],stage[4096],count[32],flow[16],port[16],drain[16];
	struct run_flow *f=&x->c.flows[i];
	pathf(name,sizeof(name),"receiver-flow%u",i+1); pathf(ready,sizeof(ready),"%s/%s.ready",x->run_dir,name); pathf(stats,sizeof(stats),"%s/%s.json",x->run_dir,name);
	snprintf(count,sizeof(count),"%llu",(unsigned long long)flow_count(f,x->c.duration_ms)); snprintf(flow,sizeof(flow),"%u",i+1); snprintf(port,sizeof(port),"%u",9000+i); snprintf(drain,sizeof(drain),"%u",x->c.drain_ms);
	int n;
	int pipes[2]; char notify[16]; if (pipe2(pipes,O_CLOEXEC)) return -1; snprintf(notify,sizeof(notify),"%d",pipes[1]);
	if (!strcmp(x->mode,"mixed") && i==2) {
		pathf(output,sizeof(output),"%s/afxdp.csv",x->run_dir);
		char *args[]={"./BPF/afxdp_recv","--output",output,"--records",count,"--ready-file",ready,"--stats-json",stats,"--drain-ms",drain,"--completion-fd",notify,"--defer-flush",x->c.interface,NULL};
		n=spawn_pipe(x,name,ready,f->rx_cpu,f->rx_policy,f->rx_priority,1,args,pipes[1]);
	} else {
		pathf(output,sizeof(output),"%s/udp-flow%u.bin",x->run_dir,i+1); pathf(stage,sizeof(stage),"/dev/shm/ace-%ld-%u-%u.tmp",(long)getpid(),x->index,i+1);
		char *args[]={"./udp_socket/bin/receiver","--flow-id",flow,"--port",port,"--count",count,"--tmpfs",stage,"--output",output,"--ready-file",ready,"--stats-json",stats,"--drain-ms",drain,"--completion-fd",notify,"--defer-flush",NULL};
		n=spawn_pipe(x,name,ready,f->rx_cpu,f->rx_policy,f->rx_priority,1,args,pipes[1]);
		if (n>=0) pathf(x->child[n].staging,sizeof(x->child[n].staging),"%s",stage);
	}
	close(pipes[1]); if (n<0) { close(pipes[0]); return -1; } x->child[n].completion_fd=pipes[0];
	if (n<0 || ready_child(x,n)) return -1;
	return n;
}
static void envnum(const char *key,uint64_t value)
{ char text[32]; snprintf(text,sizeof(text),"%llu",(unsigned long long)value); setenv(key,text,1); }
static int monitor_process(struct context *x)
{
	if (!x->c.monitor && !x->c.cpu_percent && !x->c.memory_workers && !x->c.io_workers && !x->c.network_bps) return -2;
	char directory[4096],ready[4096],cpu[16],duration[32]; pathf(directory,sizeof(directory),"%s/monitor",x->run_dir); pathf(ready,sizeof(ready),"%s/monitor.ready",x->run_dir);
	setenv("ACE_RUN_DIR",directory,1); setenv("ACE_RUN_ID",x->run_id,1); setenv("ACE_SKIP_MANIFEST","1",1); setenv("ACE_INTERFACE",x->c.interface,1); setenv("ACE_READY_FILE",ready,1);
	if (x->c.receiver_network_cpu<0) setenv("ACE_IPERF_CPU","inherit",1);
	else envnum("ACE_IPERF_CPU",(unsigned)x->c.receiver_network_cpu);
	envnum("ACE_MONITOR_ENABLED",x->c.monitor); envnum("ACE_IPERF_ENABLED",!!x->c.network_bps); envnum("ACE_IPERF_PORT",x->c.network_port); setenv("ACE_CPU_METHOD",x->c.cpu_method,1);
	envnum("ACE_MEMORY_WORKERS",x->c.memory_workers); setenv("ACE_MEMORY_BYTES",x->c.memory_bytes,1); envnum("ACE_IO_WORKERS",x->c.io_workers); setenv("ACE_IO_PATH",x->c.io_path,1);
	if (x->c.load_on_sec) { envnum("ACE_LOAD_ON_SEC",x->c.load_on_sec); envnum("ACE_LOAD_OFF_SEC",x->c.load_off_sec); } else { unsetenv("ACE_LOAD_ON_SEC"); unsetenv("ACE_LOAD_OFF_SEC"); }
	snprintf(cpu,sizeof(cpu),"%u",x->c.cpu_percent);
	snprintf(duration,sizeof(duration),"%llu",(unsigned long long)((x->c.duration_ms+x->c.warmup_ms+x->c.start_delay_ms+x->c.ready_ms+x->c.stop_ms+999)/1000+5));
	char *args[]={"bash","./monitor/controller.sh",cpu,"1",duration,x->c.cpu_cpus,NULL};
	int n=spawn(x,"monitor",ready,-1,SCHED_OTHER,0,1,args); if (n<0 || ready_child(x,n)) return -1; return n;
}
static int wait_completions(struct context *x,const int children[3],unsigned timeout_ms);
static int receiver_run(struct context *x)
{
	int loader=-1, receivers[3]={-1,-1,-1}, monitor=-1;
	int smoke=!strcmp(x->mode,"socket-only");
	if (prepare_environment(x) || capture_state(x,"before")) return -1;
	if (!smoke) {
		uint64_t capacity=0; for (unsigned i=0;i<3;i++) if (x->c.flows[i].enabled) capacity+=flow_count(&x->c.flows[i],x->c.duration_ms);
		char records[32],ready[4096]; snprintf(records,sizeof(records),"%llu",(unsigned long long)capacity); pathf(ready,sizeof(ready),"%s/loader.ready",x->run_dir);
		char *args[]={"./BPF/xdp_loader","--quiet","--mode",x->mode,"--output-dir",x->run_dir,"--prefix","xdp-","--records",records,"--ready-file",ready,x->c.interface,NULL};
		loader=spawn(x,"xdp-loader",ready,-1,SCHED_OTHER,0,1,args); if (loader<0 || ready_child(x,loader)) return -1;
	}
	for (unsigned i=0;i<3;i++) if (x->c.flows[i].enabled && (receivers[i]=receiver_process(x,i))<0) return -1;
	monitor=monitor_process(x); if (monitor==-1) return -1;
	if (message(x,RX_READY,1,x->c.ready_ms) || message(x,TX_READY,0,x->c.ready_ms)) return -1;
	if (pause_ms(x,x->c.warmup_ms)) return -1;
	if (!smoke && calibrate(x,"before")) return -1;
	if (write_run(x,"running") || message(x,START,1,x->c.ready_ms) || message(x,STARTED,0,x->c.ready_ms)) return -1;
	unsigned wait=(unsigned)x->c.duration_ms+x->c.start_delay_ms+x->c.ready_ms;
	if (message(x,STOP,0,wait) || pause_ms(x,x->c.settle_ms)) return -1;
	/* Keep both CPU and network load active through the final receiver drain. */
	for (unsigned i=0;i<3;i++) request_stop(x,receivers[i]);
	if (wait_completions(x,receivers,x->c.stop_ms)) return -1;
	for (unsigned i=0;i<3;i++) if (receivers[i]>=0) kill(x->child[receivers[i]].pid,SIGUSR1);
	for (unsigned i=0;i<3;i++) if (receivers[i]>=0 && wait_child(x,receivers[i],x->c.stop_ms,1)) return -1;
	if (!smoke && calibrate(x,"after")) return -1;
	if (stop_child(x,loader)) return -1;
	/* DONE releases sender flush; ACK confirms its network client has stopped. */
	if (message(x,DONE,1,x->c.stop_ms) || message(x,ACK,0,x->c.stop_ms)) return -1;
	/* Keep the iperf server alive until the peer has shut down its client. */
	if (stop_child(x,monitor) || capture_state(x,"after") || write_run(x,"completed")) return -1;
	return marker(x,".complete");
}
static int network_process(struct context *x)
{
	if (!x->c.network_bps) return -2;
	char rate[32],duration[32],port[16],parallel[16];
	snprintf(rate,sizeof(rate),"%llu",(unsigned long long)x->c.network_bps); snprintf(port,sizeof(port),"%u",x->c.network_port); snprintf(parallel,sizeof(parallel),"%u",x->c.network_parallel);
	snprintf(duration,sizeof(duration),"%llu",(unsigned long long)((x->c.duration_ms+x->c.warmup_ms+x->c.start_delay_ms+x->c.ready_ms+x->c.stop_ms+999)/1000+5));
	if (x->c.load_on_sec) { envnum("ACE_LOAD_ON_SEC",x->c.load_on_sec); envnum("ACE_LOAD_OFF_SEC",x->c.load_off_sec); } else { unsetenv("ACE_LOAD_ON_SEC"); unsetenv("ACE_LOAD_OFF_SEC"); }
	char *args[]={"bash","./monitor/network_load.sh",x->c.address,rate,parallel,x->c.network_protocol,x->c.network_direction,port,duration,NULL};
	return spawn(x,"network-load",NULL,x->c.sender_network_cpu,SCHED_OTHER,0,1,args);
}
static int wait_completions(struct context *x,const int children[3],unsigned timeout_ms)
{
	int64_t until=ace_mono_ns()+(int64_t)timeout_ms*1000000LL;
	while (1) {
		sigset_t blocked,previous; sigemptyset(&blocked); sigaddset(&blocked,SIGCHLD); sigaddset(&blocked,SIGINT); sigaddset(&blocked,SIGTERM);
		if (sigprocmask(SIG_BLOCK,&blocked,&previous)) return -1;
		int failed=stopping || reap(x), complete=1;
		struct pollfd p[4]={{.fd=x->fd,.events=POLLIN}};
		for (unsigned i=0;i<3;i++) {
			p[i+1].fd=-1; p[i+1].events=POLLIN;
			if (children[i]>=0) {
				struct child *c=&x->child[children[i]];
				if (!c->measurement_done) { complete=0; p[i+1].fd=c->completion_fd; }
			}
		}
		if (failed || complete) { sigprocmask(SIG_SETMASK,&previous,NULL); if (failed) { errno=ECHILD; return -1; } return 0; }
		int64_t left=until-ace_mono_ns();
		if (left<=0) { sigprocmask(SIG_SETMASK,&previous,NULL); errno=ETIMEDOUT; return -1; }
		/* Sleep until a child exits, a stop signal, peer failure, or the deadline. */
		struct timespec timeout={.tv_sec=(time_t)(left/1000000000LL),.tv_nsec=(long)(left%1000000000LL)};
		int result=ppoll(p,4,&timeout,&previous); int error=errno; sigprocmask(SIG_SETMASK,&previous,NULL);
		if (result>0) {
			if (p[0].revents) { errno=ECONNRESET; return -1; }
			for (unsigned i=0;i<3;i++) if (p[i+1].revents) {
				unsigned char outcome=1; ssize_t n=read(p[i+1].fd,&outcome,1);
				if (n<0 && errno==EINTR) continue;
				if (n!=1 || outcome) { errno=ECHILD; return -1; }
				struct child *c=&x->child[children[i]]; c->measurement_done=1; close(c->completion_fd); c->completion_fd=-1;
			}
			continue;
		}
		if (result==0) { errno=ETIMEDOUT; return -1; }
		if (error!=EINTR) { errno=error; return -1; }
	}
}
static int sender_run(struct context *x)
{
	if (prepare_environment(x)) return -1;
	if (message(x,RX_READY,0,x->c.ready_ms)) return -1;
	if (capture_state(x,"before")) return -1;
	int senders[3]={-1,-1,-1}; char start[4096]; pathf(start,sizeof(start),"%s/start.ns",x->run_dir);
	for (unsigned i=0;i<3;i++) {
		struct run_flow *f=&x->c.flows[i]; if (!f->enabled) continue;
		char name[32],ready[4096],stats[4096],flow[16],port[16],count[32],period[32];
		int pipes[2]; char notify[16]; if (pipe2(pipes,O_CLOEXEC)) return -1; snprintf(notify,sizeof(notify),"%d",pipes[1]);
		pathf(name,sizeof(name),"sender-flow%u",i+1); pathf(ready,sizeof(ready),"%s/%s.ready",x->run_dir,name); pathf(stats,sizeof(stats),"%s/%s.json",x->run_dir,name);
		snprintf(flow,sizeof(flow),"%u",i+1); snprintf(port,sizeof(port),"%u",9000+i); snprintf(count,sizeof(count),"%llu",(unsigned long long)flow_count(f,x->c.duration_ms)); snprintf(period,sizeof(period),"%llu",(unsigned long long)f->period_ns);
		char *args[]={"./udp_socket/bin/sender","--port",port,"--flow-id",flow,"--count",count,"--period-ns",period,"--ready-file",ready,"--stats-json",stats,"--start-file",start,"--completion-fd",notify,"--defer-flush",x->c.address,NULL};
		senders[i]=spawn_pipe(x,name,ready,f->tx_cpu,f->tx_policy,f->tx_priority,0,args,pipes[1]); close(pipes[1]);
		if (senders[i]<0) { close(pipes[0]); return -1; } x->child[senders[i]].completion_fd=pipes[0];
		if (ready_child(x,senders[i])) return -1;
	}
	int network=network_process(x); if (network==-1) return -1;
	if (network>=0 && pause_ms(x,100)) return -1;
	if (message(x,TX_READY,1,x->c.ready_ms) || message(x,START,0,x->c.ready_ms+(unsigned)x->c.warmup_ms)) return -1;
	if (write_run(x,"running")) return -1;
	char temporary[4096]; pathf(temporary,sizeof(temporary),"%s.tmp",start); FILE *out=ace_open_exclusive_stream(temporary); if (!out) return -1;
	int64_t now=ace_mono_ns(); if (now<0) { fclose(out); return -1; }
	fprintf(out,"%lld\n",(long long)(now+(int64_t)x->c.start_delay_ms*1000000LL));
	if (ace_finish_stream(out) || link(temporary,start)) { unlink(temporary); return -1; } unlink(temporary);
	if (message(x,STARTED,1,x->c.ready_ms) || wait_completions(x,senders,(unsigned)x->c.duration_ms+x->c.start_delay_ms+x->c.ready_ms)) return -1;
	if (message(x,STOP,1,x->c.stop_ms) || message(x,DONE,0,x->c.stop_ms*3+x->c.settle_ms+x->c.drain_ms)) return -1;
	for (unsigned i=0;i<3;i++) if (senders[i]>=0) kill(x->child[senders[i]].pid,SIGUSR1);
	for (unsigned i=0;i<3;i++) if (senders[i]>=0 && wait_child(x,senders[i],x->c.stop_ms,0)) return -1;
	if (stop_child(x,network) || capture_state(x,"after") || write_run(x,"completed") || message(x,ACK,1,x->c.stop_ms)) return -1;
	return marker(x,".complete");
}
static int preflight(struct context *x)
{
	const char *required[]={"udp_socket/bin/sender","udp_socket/bin/receiver","udp_socket/bin/ace_clock",NULL};
	for (unsigned i=0;required[i];i++) if (access(required[i],X_OK)) { fprintf(stderr,"missing executable: %s\n",required[i]); return -1; }
	if (access("monitor/capture_state.sh",R_OK)) { perror("missing state collector"); return -1; }
	if (access("monitor/prepare_env.sh",R_OK) || access("monitor/set_irq_affinity.sh",R_OK)) { perror("missing environment helpers"); return -1; }
	if (x->receiver) for (unsigned m=0;m<x->c.mode_count;m++) if (strcmp(x->c.modes[m],"socket-only")) {
		const char *bpf[]={"BPF/xdp_loader","udp_socket/bin/phc_calibrate",NULL};
		for (unsigned i=0;bpf[i];i++) if (access(bpf[i],X_OK)) { fprintf(stderr,"missing executable: %s\n",bpf[i]); return -1; }
		if (!strcmp(x->c.modes[m],"mixed") && x->c.flows[2].enabled && access("BPF/afxdp_recv",X_OK)) return -1;
	}
	return 0;
}
int main(int argc,char **argv)
{
	if (argc!=4 || strcmp(argv[2],"--config") || (strcmp(argv[1],"receiver") && strcmp(argv[1],"sender") && strcmp(argv[1],"plan"))) {
		fprintf(stderr,"Usage: ace-run {receiver|sender|plan} --config FILE\n"); return 2;
	}
	struct context x={.fd=-1};
	if (read_run_config(argv[3],&x.c)) { perror("configuration"); return 2; }
	if (!strcmp(argv[1],"plan")) {
		printf("run_id=%s sha256=%s repeats=%u\n",x.c.run_id,x.c.digest,x.c.repeats);
		for (unsigned m=0;m<x.c.mode_count;m++) { write_condition(stdout,&x.c,x.c.modes[m],x.c.duration_ms); putchar('\n'); }
		return 0;
	}
	x.receiver=!strcmp(argv[1],"receiver"); strcpy(x.run_id,x.c.run_id);
	struct sigaction stop_action={.sa_handler=signal_stop}, child_action={.sa_handler=child_event};
	sigemptyset(&stop_action.sa_mask); sigemptyset(&child_action.sa_mask);
	sigaction(SIGINT,&stop_action,NULL); sigaction(SIGTERM,&stop_action,NULL); sigaction(SIGCHLD,&child_action,NULL); signal(SIGPIPE,SIG_IGN);
	if (prctl(PR_SET_CHILD_SUBREAPER,1)) { perror("child subreaper"); return 1; }
	char output[4096]; if (!realpath(x.c.repo,output)) { perror("repository path"); return 1; } strcpy(x.c.repo,output);
	if (chdir(x.c.repo) || scheduler(x.receiver?x.c.receiver_supervisor_cpu:x.c.sender_supervisor_cpu,SCHED_OTHER,0)) { perror("runner environment"); return 1; }
	if (preflight(&x)) { perror("preflight"); return 1; }
	if (x.c.output[0]!='/') { if (pathf(output,sizeof(output),"%s/%s",x.c.repo,x.c.output)) return 1; strcpy(x.c.output,output); }
	if (pathf(x.suite,sizeof(x.suite),"%s/%s/%s",x.c.output,x.c.run_id,argv[1])) return 1;
	char runs[4096]; if (pathf(runs,sizeof(runs),"%s/runs",x.suite) || mkdir_parents(runs)) { perror("create run directories"); return 1; }
	if (connect_peer(&x)) { perror("control handshake"); if (x.fd>=0) close(x.fd); return 1; }
	int failed=0;
	for (unsigned r=1;r<=x.c.repeats && !failed;r++) for (unsigned m=0;m<x.c.mode_count && !failed;m++) {
		/* 반복마다 시작 모드를 뒤집어 항상 같은 모드가 먼저인 편향을 줄인다. */
		unsigned mode_index=x.c.alternate_mode_order && !(r%2)?x.c.mode_count-1-m:m;
		x.repeat=r; x.index++; strcpy(x.mode,x.c.modes[mode_index]);
		if (begin_run(&x)) { perror("prepare run"); failed=1; }
		else { printf("%s: %s\n",argv[1],x.run_id); fflush(stdout); if (x.receiver?receiver_run(&x):sender_run(&x)) { perror("run failed"); failed=1; } }
		if (failed) {
			(void)message(&x,FAIL,1,1000); cleanup_children(&x);
			if (x.owns_run) { (void)write_run(&x,"failed"); (void)marker(&x,".failed"); }
		}
	}
	close(x.fd); return failed?1:0;
}
