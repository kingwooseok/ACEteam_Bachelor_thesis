#ifndef ACE_RUN_CONFIG_H
#define ACE_RUN_CONFIG_H
#include <stdint.h>
#include <stdio.h>
#define RUN_MAX_RECORDS 1048576ULL
struct run_flow {
	int enabled, tx_cpu, rx_cpu, tx_policy, rx_policy, tx_priority, rx_priority;
	uint64_t period_ns, deadline_ns;
};
struct run_config {
	char run_id[65], address[64], interface[16], repo[4096], output[4096];
	char digest[65], modes[3][16], cpu_cpus[256], cpu_method[64];
	char memory_bytes[64], io_path[4096], network_protocol[8], network_direction[16];
	unsigned mode_count, repeats, control_port, ready_ms, stop_ms, start_delay_ms;
	unsigned drain_ms, settle_ms, monitor, cpu_percent, memory_workers, io_workers;
	unsigned network_port, network_parallel, load_on_sec, load_off_sec;
	int supervisor_cpu;
	uint64_t duration_ms, warmup_ms, network_bps;
	struct run_flow flows[3];
};
int read_run_config(const char *file, struct run_config *c);
uint64_t flow_count(const struct run_flow *f, uint64_t duration_ms);
void write_condition(FILE *out, const struct run_config *c, const char *mode,
		uint64_t duration_ms);
void json_string(FILE *out, const char *s);
#endif
