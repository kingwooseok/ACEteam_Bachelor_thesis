#define _POSIX_C_SOURCE 200809L
#include <ctype.h>
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* One observer replaces mpstat. Packet paths do not call this program.
 * The caller directs stdout to tmpfs and copies it after capture completes. */
struct cpu_ticks {
	char name[24];
	uint64_t ticks[8];
};

static int mono_ns(uint64_t *value)
{
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
	*value = (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
	return 0;
}

static int unsigned_value(const char *text, uint64_t *value)
{
	if (!text || !*text) return -1;
	for (const char *p = text; *p; p++) if (!isdigit((unsigned char)*p)) return -1;
	errno = 0;
	char *end;
	unsigned long long parsed = strtoull(text, &end, 10);
	if (errno || *end) return -1;
	*value = (uint64_t)parsed;
	return 0;
}

static int snapshot(const char *path)
{
	uint64_t before, after = 0;
	if (mono_ns(&before)) return -1;
	FILE *input = fopen(path, "r");
	if (!input) return -1;
	struct cpu_ticks *rows = NULL;
	size_t count = 0, capacity = 0, line_capacity = 0;
	char *line = NULL;
	int error = 0;
	while (getline(&line, &line_capacity, input) >= 0) {
		char *save = NULL, *label = strtok_r(line, " \t\r\n", &save);
		if (!label || strncmp(label, "cpu", 3)) continue;
		uint64_t cpu_number;
		if (label[3] && unsigned_value(label + 3, &cpu_number)) { error = EINVAL; break; }
		const char *name = label[3] ? label + 3 : "all";
		if (strlen(name) >= sizeof(rows[0].name) || count == 16384) { error = EINVAL; break; }
		if (count == capacity) {
			size_t next = capacity ? capacity * 2 : 16;
			struct cpu_ticks *replacement = realloc(rows, next * sizeof(*rows));
			if (!replacement) { error = ENOMEM; break; }
			rows = replacement; capacity = next;
		}
		for (size_t i = 0; i < count; i++) if (!strcmp(rows[i].name, name)) { error = EINVAL; break; }
		if (error) break;
		strcpy(rows[count].name, name);
		for (size_t i = 0; i < 8; i++) {
			if (unsigned_value(strtok_r(NULL, " \t\r\n", &save), &rows[count].ticks[i])) { error = EINVAL; break; }
		}
		if (error) break;
		/* guest and guest_nice already contribute to user/nice; never add them. */
		count++;
	}
	if (ferror(input) && !error) error = EIO;
	if (fclose(input) && !error) error = errno;
	free(line);
	if (!error && (!count || strcmp(rows[0].name, "all"))) error = EINVAL;
	if (!error && mono_ns(&after)) error = errno;
	if (!error) {
		uint64_t timestamp = before + (after - before) / 2;
		for (size_t i = 0; i < count; i++) {
			if (printf("%" PRIu64 ",%s", timestamp, rows[i].name) < 0) { error = EIO; break; }
			for (size_t j = 0; j < 8; j++) {
				if (printf(",%" PRIu64, rows[i].ticks[j]) < 0) { error = EIO; break; }
			}
			if (error || putchar('\n') == EOF) { error = EIO; break; }
		}
		if (fflush(stdout) && !error) error = errno;
	}
	free(rows);
	if (error) { errno = error; return -1; }
	return 0;
}

static int network_snapshot(const char *path, const char *interface, FILE *output)
{
	if (!output) return 0;
	uint64_t before, after = 0, counters[16];
	if (mono_ns(&before)) return -1;
	FILE *input = fopen(path, "r");
	if (!input) return -1;
	char *line = NULL;
	size_t capacity = 0;
	int found = 0, error = 0;
	while (getline(&line, &capacity, input) >= 0) {
		char *colon = strrchr(line, ':');
		if (!colon) continue;
		*colon = '\0';
		char *name = line;
		while (isspace((unsigned char)*name)) name++;
		char *end = colon;
		while (end > name && isspace((unsigned char)end[-1])) *--end = '\0';
		if (strcmp(name, interface)) continue;
		if (found++) { error = EINVAL; break; }
		char *save = NULL;
		for (size_t i = 0; i < 16; i++) {
			if (unsigned_value(strtok_r(i ? NULL : colon + 1, " \t\r\n", &save), &counters[i])) {
				error = EINVAL; break;
			}
		}
		if (error || strtok_r(NULL, " \t\r\n", &save)) { error = EINVAL; break; }
	}
	if (ferror(input) && !error) error = EIO;
	if (fclose(input) && !error) error = errno;
	free(line);
	if (!error && !found) error = ENODEV;
	if (!error && mono_ns(&after)) error = errno;
	if (!error) {
		uint64_t timestamp = before + (after - before) / 2;
		if (fprintf(output, "%" PRIu64 ",%s", timestamp, interface) < 0) error = EIO;
		static const unsigned columns[] = {0, 1, 2, 3, 8, 9, 10, 11};
		for (size_t i = 0; i < sizeof(columns) / sizeof(columns[0]) && !error; i++)
			if (fprintf(output, ",%" PRIu64, counters[columns[i]]) < 0) error = EIO;
		if (!error && fputc('\n', output) == EOF) error = EIO;
		if (fflush(output) && !error) error = errno;
	}
	if (error) { errno = error; return -1; }
	return 0;
}

static int sample(const char *stat_file, const char *net_file, const char *interface, FILE *network)
{
	if (snapshot(stat_file)) { perror("CPU snapshot"); return -1; }
	if (network_snapshot(net_file, interface, network)) { perror("NIC snapshot"); return -1; }
	return 0;
}

int main(int argc, char **argv)
{
	uint64_t interval_ms = 1000;
	const char *stat_file = "/proc/stat";
	const char *net_file = "/proc/net/dev", *interface = NULL, *network_path = NULL;
	static const struct option options[] = {
		{"interval-ms", required_argument, NULL, 'i'},
		{"stat-file", required_argument, NULL, 's'},
		{"interface", required_argument, NULL, 'n'},
		{"network-output", required_argument, NULL, 'o'},
		{"net-dev-file", required_argument, NULL, 'f'},
		{"help", no_argument, NULL, 'h'}, {NULL, 0, NULL, 0}
	};
	int option;
	while ((option = getopt_long(argc, argv, "i:s:n:o:f:h", options, NULL)) != -1) {
		if (option == 'h') {
			puts("Usage: cpu_sample [--interval-ms MS] [--stat-file FILE]\n"
			     "                  [--interface IFACE --network-output CSV] [--net-dev-file FILE]\n"
			     "Write cumulative CPU/NIC counters with CLOCK_MONOTONIC timestamps; TERM/INT saves final samples.");
			return 0;
		}
		if (option == 's') stat_file = optarg;
		else if (option == 'n') interface = optarg;
		else if (option == 'o') network_path = optarg;
		else if (option == 'f') net_file = optarg;
		else if (option != 'i' || unsigned_value(optarg, &interval_ms) || !interval_ms || interval_ms > 3600000) return 2;
	}
	if (optind != argc || (!!interface != !!network_path)) return 2;
	if (interface) {
		if (!*interface || strlen(interface) > 15 || !*network_path) return 2;
		for (const char *p = interface; *p; p++)
			if (!isalnum((unsigned char)*p) && !strchr("_.:-", *p)) return 2;
	}
	/* Pending stop signals are consumed atomically with the wait, including
	 * a signal received just after sampling and before sleeping. */
	sigset_t stops;
	sigemptyset(&stops); sigaddset(&stops, SIGTERM); sigaddset(&stops, SIGINT);
	if (sigprocmask(SIG_BLOCK, &stops, NULL)) { perror("block stop signals"); return 1; }
	FILE *network = network_path ? fopen(network_path, "wx") : NULL;
	if (network_path && !network) { perror("create NIC samples"); return 1; }
	if (network && fputs("monotonic_ns,interface,rx_bytes,rx_packets,rx_errors,rx_dropped,tx_bytes,tx_packets,tx_errors,tx_dropped\n", network) == EOF) {
		perror("NIC header"); fclose(network); return 1;
	}
	if (puts("monotonic_ns,cpu,user,nice,system,idle,iowait,irq,softirq,steal") == EOF || sample(stat_file, net_file, interface, network)) {
		perror("CPU snapshot"); return 1;
	}
	uint64_t now;
	if (mono_ns(&now)) { perror("CLOCK_MONOTONIC"); return 1; }
	const uint64_t interval = interval_ms * 1000000ULL;
	uint64_t deadline = now + interval;
	for (;;) {
		if (mono_ns(&now)) { perror("CLOCK_MONOTONIC"); return 1; }
		uint64_t left = deadline > now ? deadline - now : 0;
		struct timespec timeout = {.tv_sec = (time_t)(left / 1000000000ULL), .tv_nsec = (long)(left % 1000000000ULL)};
		int received = sigtimedwait(&stops, NULL, &timeout);
		if (received == SIGTERM || received == SIGINT) {
			if (sample(stat_file, net_file, interface, network)) return 1;
			if (network && fclose(network)) { perror("close NIC samples"); return 1; }
			return 0;
		}
		if (received < 0 && errno == EINTR) continue;
		if (received < 0 && errno != EAGAIN) { perror("CPU sample wait"); return 1; }
		if (sample(stat_file, net_file, interface, network) || mono_ns(&now)) { perror("CPU snapshot"); return 1; }
		/* Do not emit a burst of catch-up reads after scheduler delays. */
		deadline += interval;
		if (deadline <= now) deadline = now + interval;
	}
}
