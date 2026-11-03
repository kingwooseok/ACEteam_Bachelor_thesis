#ifndef ACE_RUN_CONFIG_H
#define ACE_RUN_CONFIG_H
#include <stdint.h>
#include <stdio.h>
#include <sched.h>
#define RUN_MAX_RECORDS 1048576ULL
#define RUN_CPU_LIST_SIZE 4096
struct run_flow {
	int enabled, tx_cpu, rx_cpu, tx_policy, rx_policy, tx_priority, rx_priority;
	int all_pass_rx_cpu;
	uint64_t period_ns, deadline_ns;
};
struct run_config {
	char run_id[65], address[64], interface[16], repo[4096], output[4096];
	char digest[65], modes[4][16], cpu_cpus[256], cpu_method[64];
	char cpu_layout[16];
	char memory_bytes[64], io_path[4096], network_protocol[8], network_direction[16];
	char governor[32];
	unsigned alternate_mode_order, verify_placement;
	unsigned mode_count, repeats, control_port, ready_ms, stop_ms, start_delay_ms;
	unsigned drain_ms, settle_ms, monitor, cpu_percent, memory_workers, io_workers;
	unsigned cpu_workers;
	unsigned xsk_busy, phc_tracking;
	int rx_coalesce_usecs;
	unsigned network_port, network_parallel, load_on_sec, load_off_sec;
	unsigned rx_irq_cpu;
	int supervisor_cpu, receiver_supervisor_cpu, sender_supervisor_cpu;
	int receiver_network_cpu, sender_network_cpu;
	char receiver_shared_cpus[RUN_CPU_LIST_SIZE];
	cpu_set_t receiver_shared_mask;
	int receiver_cpumap_cpu;
	uint64_t duration_ms, warmup_ms, network_bps;
	struct run_flow flows[3];
};
int read_run_config(const char *file, struct run_config *c);
uint64_t flow_count(const struct run_flow *f, uint64_t duration_ms);
const char *run_flow_path(const char *mode, unsigned flow_index);
int run_rx_cpu(const struct run_flow *f, const char *mode);
int run_parse_cpus(const char *text, cpu_set_t *set);
int run_format_cpus(const cpu_set_t *set, char *text, size_t size);
int run_cpumap_active(const struct run_config *c, const char *mode);
void run_general_cpu_set(const struct run_config *c, const char *mode, cpu_set_t *set);
int run_load_cpus(const struct run_config *c, const char *mode, char *text, size_t size);
void run_rx_cpu_set(const struct run_config *c, unsigned flow_index,
		const char *mode, cpu_set_t *set);
unsigned run_mode_index(const struct run_config *c, unsigned repeat, unsigned position);
void write_condition(FILE *out, const struct run_config *c, const char *mode,
		uint64_t duration_ms);
void json_string(FILE *out, const char *s);
#endif
