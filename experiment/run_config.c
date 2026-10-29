#define _GNU_SOURCE
#include "run_config.h"
#include "ace_run_control.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <sched.h>
#include <stdbool.h>
#include <sys/wait.h>

struct entry { char key[80], value[4096]; };
struct parser { struct entry entries[100]; unsigned count; int error; };
static const char *lookup(struct parser *p, const char *key, const char *fallback)
{
	for (unsigned i = 0; i < p->count; i++)
		if (!strcmp(p->entries[i].key, key)) return p->entries[i].value;
	if (!fallback) { fprintf(stderr, "required configuration: %s\n", key); p->error = 1; return ""; }
	return fallback;
}
static uint64_t number(struct parser *p, const char *key, const char *fallback,
		uint64_t lo, uint64_t hi)
{
	uint64_t value = 0;
	if (ace_parse_u64(lookup(p, key, fallback), lo, hi, &value)) {
		fprintf(stderr, "invalid integer: %s (%llu..%llu)\n", key,
			(unsigned long long)lo, (unsigned long long)hi); p->error = 1;
	}
	return value;
}
static void string(struct parser *p, const char *key, const char *fallback, char *out, size_t size)
{
	const char *value = lookup(p, key, fallback);
	if (strlen(value) >= size || !*value) { fprintf(stderr, "invalid value: %s\n", key); p->error = 1; return; }
	strcpy(out, value);
}
static int cpu(struct parser *p, const char *key, const char *fallback)
{
	const char *value = lookup(p, key, fallback);
	if (!strcmp(value, "inherit")) return -1;
	uint64_t n;
	if (ace_parse_u64(value, 0, CPU_SETSIZE - 1, &n)) { p->error = 1; fprintf(stderr, "invalid CPU: %s\n", key); return -1; }
	return (int)n;
}
static int policy(struct parser *p, const char *key)
{
	const char *value = lookup(p, key, NULL);
	if (!strcmp(value, "other")) return SCHED_OTHER;
	if (!strcmp(value, "fifo")) return SCHED_FIFO;
	if (!strcmp(value, "rr")) return SCHED_RR;
	p->error = 1; fprintf(stderr, "invalid scheduler policy: %s\n", key); return SCHED_OTHER;
}
static int allowed(const char *key)
{
	static const char *keys[] = {"run_id","receiver_address","control_port","repo","output_root","interface",
		"modes","repeats","duration_ms","warmup_ms","ready_timeout_ms","stop_timeout_ms",
		"start_delay_ms","drain_ms","settle_ms","supervisor_cpu","monitor",
		"environment.governor","environment.rx_irq_cpu",
		"load.cpu_percent","load.cpu_cpus","load.cpu_method","load.memory_workers","load.memory_bytes",
		"load.io_workers","load.io_path","load.network_bps","load.network_protocol","load.network_direction",
		"load.network_port","load.network_parallel","load.on_sec","load.off_sec"};
	for (unsigned i = 0; i < sizeof(keys) / sizeof(*keys); i++) if (!strcmp(key, keys[i])) return 1;
	if (strlen(key) < 7 || strncmp(key, "flow", 4) || key[4] < '1' || key[4] > '3' || key[5] != '.') return 0;
	static const char *fields[] = {"enabled","period_ns","deadline_ns","tx_cpu","rx_cpu","tx_policy","rx_policy","tx_priority","rx_priority"};
	for (unsigned i = 0; i < sizeof(fields) / sizeof(*fields); i++) if (!strcmp(key + 6, fields[i])) return 1;
	return 0;
}
static char *trim(char *s)
{
	while (*s && isspace((unsigned char)*s)) s++;
	size_t n = strlen(s);
	while (n && isspace((unsigned char)s[n - 1])) s[--n] = 0;
	return s;
}
static int config_hash(const char *path, char digest[65])
{
	int pipefd[2];
	if (pipe2(pipefd, O_CLOEXEC)) return -1;
	pid_t pid = fork();
	if (pid == 0) { dup2(pipefd[1], STDOUT_FILENO); close(pipefd[0]); close(pipefd[1]); execlp("sha256sum", "sha256sum", "--", path, (char *)NULL); _exit(127); }
	close(pipefd[1]);
	if (pid < 0) { close(pipefd[0]); return -1; }
	FILE *stream = fdopen(pipefd[0], "r");
	char line[8192] = {0};
	int okay = stream && fgets(line, sizeof(line), stream);
	if (stream) { while (fgetc(stream) != EOF) {} fclose(stream); } else close(pipefd[0]);
	int status = 0; while (waitpid(pid, &status, 0) < 0) if (errno != EINTR) return -1;
	if (!okay || !WIFEXITED(status) || WEXITSTATUS(status) || strlen(line) < 64) return -1;
	for (unsigned i = 0; i < 64; i++) if (!isxdigit((unsigned char)line[i])) return -1;
	memcpy(digest, line, 64); digest[64] = 0; return 0;
}
uint64_t flow_count(const struct run_flow *f, uint64_t duration_ms)
{ return (duration_ms * 1000000ULL + f->period_ns - 1) / f->period_ns; }

int read_run_config(const char *file, struct run_config *c)
{
	struct parser p = {0}; char line[8192]; unsigned lineno = 0;
	FILE *input = fopen(file, "r");
	if (!input) return -1;
	while (fgets(line, sizeof(line), input)) {
		lineno++;
		if (!strchr(line, '\n') && !feof(input)) { p.error = 1; break; }
		char *key = trim(line);
		if (!*key || *key == '#') continue;
		char *equal = strchr(key, '=');
		if (!equal || p.count == 100) { p.error = 1; break; }
		*equal = 0; char *value = trim(equal + 1); key = trim(key);
		if (!allowed(key) || strlen(key) >= sizeof(p.entries[0].key) || strlen(value) >= sizeof(p.entries[0].value)) { p.error = 1; break; }
		for (char *v = value; *v; v++) if ((unsigned char)*v < 32 || (unsigned char)*v > 126) p.error = 1;
		for (unsigned i = 0; i < p.count; i++) if (!strcmp(p.entries[i].key, key)) p.error = 1;
		if (p.error) break;
		strcpy(p.entries[p.count].key, key); strcpy(p.entries[p.count++].value, value);
	}
	if (ferror(input)) p.error = 1;
	fclose(input);
	if (p.error) { fprintf(stderr, "invalid, duplicate or unknown config entry at line %u\n", lineno); return -1; }
	memset(c, 0, sizeof(*c));
	string(&p,"run_id",NULL,c->run_id,sizeof(c->run_id));
	if (!isalnum((unsigned char)c->run_id[0])) p.error=1;
	for (const char *q = c->run_id; *q; q++) if (!isalnum((unsigned char)*q) && *q != '-' && *q != '_') p.error = 1;
	string(&p,"receiver_address",NULL,c->address,sizeof(c->address));
	struct in_addr address; if (inet_pton(AF_INET,c->address,&address) != 1) p.error = 1;
	string(&p,"interface","eth0",c->interface,sizeof(c->interface));
	string(&p,"repo","AUTO",c->repo,sizeof(c->repo));
	if (!strcmp(c->repo,"AUTO")) {
		ssize_t n = readlink("/proc/self/exe",c->repo,sizeof(c->repo)-1);
		if (n < 0) p.error = 1;
		else { c->repo[n] = 0; char *end = strrchr(c->repo,'/'); if (end) *end=0; end=strrchr(c->repo,'/'); if (end) *end=0; else p.error=1; }
	}
	string(&p,"output_root","results/experiment",c->output,sizeof(c->output));
	c->control_port=number(&p,"control_port","49090",1024,65535);
	c->repeats=number(&p,"repeats",NULL,1,10000);
	c->duration_ms=number(&p,"duration_ms",NULL,1,86400000);
	c->warmup_ms=number(&p,"warmup_ms",NULL,0,3600000);
	c->ready_ms=number(&p,"ready_timeout_ms","60000",100,600000);
	c->stop_ms=number(&p,"stop_timeout_ms","60000",100,600000);
	c->start_delay_ms=number(&p,"start_delay_ms","250",10,60000);
	c->drain_ms=number(&p,"drain_ms","100",0,60000);
	if (c->stop_ms<c->drain_ms+100) { fprintf(stderr,"stop_timeout_ms must exceed drain_ms by at least 100 ms\n"); p.error=1; }
	c->settle_ms=number(&p,"settle_ms",NULL,0,60000);
	c->supervisor_cpu=cpu(&p,"supervisor_cpu","inherit");
	/* C08/C10은 실제 측정 준비에서 적용한다. socket-only 검증은 장치를 바꾸지 않는다. */
	string(&p,"environment.governor","performance",c->governor,sizeof(c->governor));
	for (const char *q=c->governor;*q;q++) if (!isalnum((unsigned char)*q) && *q!='_' && *q!='-') p.error=1;
	c->rx_irq_cpu=number(&p,"environment.rx_irq_cpu","2",0,CPU_SETSIZE-1);
	c->monitor=number(&p,"monitor",NULL,0,1);
	c->cpu_percent=number(&p,"load.cpu_percent",NULL,0,100);
	const char *cpus=lookup(&p,"load.cpu_cpus",c->cpu_percent ? NULL : "");
	if (strlen(cpus)>=sizeof(c->cpu_cpus)) p.error=1; else strcpy(c->cpu_cpus,cpus);
	for (const char *q=cpus;*q;q++) if (!isdigit((unsigned char)*q) && *q!=',') p.error=1;
	if ((c->cpu_percent && !*cpus) || (*cpus && (*cpus==',' || cpus[strlen(cpus)-1]==',' || strstr(cpus,",,")))) p.error=1;
	string(&p,"load.cpu_method","all",c->cpu_method,sizeof(c->cpu_method));
	for (const char *q=c->cpu_method;*q;q++) if (!isalnum((unsigned char)*q) && *q!='_' && *q!='-') p.error=1;
	c->memory_workers=number(&p,"load.memory_workers","0",0,256);
	string(&p,"load.memory_bytes",c->memory_workers ? NULL : "0",c->memory_bytes,sizeof(c->memory_bytes));
	c->io_workers=number(&p,"load.io_workers","0",0,256);
	string(&p,"load.io_path",c->io_workers ? NULL : "/tmp",c->io_path,sizeof(c->io_path));
	c->network_bps=number(&p,"load.network_bps",NULL,0,10000000000ULL);
	c->network_port=number(&p,"load.network_port","5201",1024,65535);
	c->network_parallel=number(&p,"load.network_parallel","1",1,128);
	if (c->network_bps && c->network_bps<c->network_parallel) p.error=1;
	string(&p,"load.network_protocol","udp",c->network_protocol,sizeof(c->network_protocol));
	string(&p,"load.network_direction","forward",c->network_direction,sizeof(c->network_direction));
	if (strcmp(c->network_protocol,"udp") && strcmp(c->network_protocol,"tcp")) p.error=1;
	if (strcmp(c->network_direction,"forward") && strcmp(c->network_direction,"reverse") && strcmp(c->network_direction,"bidir")) p.error=1;
	c->load_on_sec=number(&p,"load.on_sec","0",0,3600);
	c->load_off_sec=number(&p,"load.off_sec","0",0,3600);
	if (!!c->load_on_sec != !!c->load_off_sec) p.error=1;
	if (c->control_port==c->network_port || (c->control_port>=9000 && c->control_port<=9002) || (c->network_port>=9000 && c->network_port<=9002)) p.error=1;
	char modes[64]; string(&p,"modes",NULL,modes,sizeof(modes)); char *save=NULL;
	for (char *m=strtok_r(modes,",",&save);m;m=strtok_r(NULL,",",&save)) {
		if (c->mode_count>=3 || (strcmp(m,"mixed") && strcmp(m,"all-pass") && strcmp(m,"socket-only"))) { p.error=1; break; }
		for (unsigned j=0;j<c->mode_count;j++) if (!strcmp(c->modes[j],m)) p.error=1;
		strcpy(c->modes[c->mode_count++],m);
	}
	if (!c->mode_count) p.error=1;
	unsigned enabled=0; uint64_t total=0;
	for (unsigned i=0;i<3;i++) {
		char key[80]; struct run_flow *f=&c->flows[i];
#define FIELD(s) (snprintf(key,sizeof(key),"flow%u.%s",i+1,(s)),key)
		f->enabled=number(&p,FIELD("enabled"),NULL,0,1);
		if (!f->enabled) continue;
		enabled++;
		f->period_ns=number(&p,FIELD("period_ns"),NULL,1,1000000000000ULL);
		f->deadline_ns=number(&p,FIELD("deadline_ns"),"0",0,1000000000000ULL);
		f->tx_cpu=cpu(&p,FIELD("tx_cpu"),NULL); f->rx_cpu=cpu(&p,FIELD("rx_cpu"),NULL);
		f->tx_policy=policy(&p,FIELD("tx_policy")); f->rx_policy=policy(&p,FIELD("rx_policy"));
		f->tx_priority=number(&p,FIELD("tx_priority"),NULL,f->tx_policy==SCHED_OTHER?0:1,f->tx_policy==SCHED_OTHER?0:99);
		f->rx_priority=number(&p,FIELD("rx_priority"),NULL,f->rx_policy==SCHED_OTHER?0:1,f->rx_policy==SCHED_OTHER?0:99);
		if (f->period_ns) total+=flow_count(f,c->duration_ms);
#undef FIELD
	}
	if (!enabled || !total || total>RUN_MAX_RECORDS) { fprintf(stderr,"total record capacity must be 1..%llu\n",RUN_MAX_RECORDS); p.error=1; }
	if (p.error) { errno=EINVAL; return -1; }
	if (config_hash(file,c->digest)) { fprintf(stderr,"configuration SHA256 failed\n"); return -1; }
	return 0;
}
void json_string(FILE *out,const char *s)
{
	fputc('"',out); for (;*s;s++) { if (*s=='"' || *s=='\\') fputc('\\',out); if ((unsigned char)*s<32) fprintf(out,"\\u%04x",(unsigned char)*s); else fputc(*s,out); } fputc('"',out);
}
void write_condition(FILE *out,const struct run_config *c,const char *mode,uint64_t duration_ms)
{
	fprintf(out,"{\"mode\":\"%s\",\"duration_s\":%.3f,\"flows\":[",mode,duration_ms/1000.0);
	unsigned emitted=0;
	for (unsigned i=0;i<3;i++) { const struct run_flow *f=&c->flows[i]; if (!f->enabled) continue;
		fprintf(out,"%s{\"flow_id\":%u,\"port\":%u,\"count\":%llu,\"period_ns\":%llu,\"deadline_ns\":",emitted++?",":"",i+1,9000+i,(unsigned long long)flow_count(f,duration_ms),(unsigned long long)f->period_ns);
		if (f->deadline_ns) fprintf(out,"%llu",(unsigned long long)f->deadline_ns); else fputs("null",out);
		fprintf(out,",\"requested_path\":\"%s\",\"tx_cpu\":%d,\"rx_cpu\":%d,\"tx_policy\":%d,\"rx_policy\":%d,\"tx_priority\":%d,\"rx_priority\":%d}",!strcmp(mode,"mixed")?(i==0?"pass":i==1?"cpumap":"xsk"):"pass",f->tx_cpu,f->rx_cpu,f->tx_policy,f->rx_policy,f->tx_priority,f->rx_priority);
	}
	fprintf(out,"],\"load\":{\"cpu_percent\":%u,\"cpu_cpus\":",c->cpu_percent); json_string(out,c->cpu_cpus);
	fprintf(out,",\"memory_workers\":%u,\"memory_bytes\":",c->memory_workers); json_string(out,c->memory_bytes);
	fprintf(out,",\"io_workers\":%u,\"network_bps\":%llu,\"network_protocol\":\"%s\",\"network_direction\":\"%s\",\"network_parallel\":%u,\"on_sec\":%u,\"off_sec\":%u},\"environment\":{\"governor\":",c->io_workers,(unsigned long long)c->network_bps,c->network_protocol,c->network_direction,c->network_parallel,c->load_on_sec,c->load_off_sec);
	json_string(out,c->governor);
	fprintf(out,",\"rx_irq_cpu\":%u},\"config\":{\"sha256\":\"%s\",\"warmup_ms\":%llu,\"settle_ms\":%u,\"drain_ms\":%u,\"monitor\":%u}}",c->rx_irq_cpu,c->digest,(unsigned long long)c->warmup_ms,c->settle_ms,c->drain_ms,c->monitor);
}
