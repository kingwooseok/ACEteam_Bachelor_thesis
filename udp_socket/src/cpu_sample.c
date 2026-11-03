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

int main(int argc, char **argv)
{
	uint64_t interval_ms = 1000;
	const char *stat_file = "/proc/stat";
	static const struct option options[] = {
		{"interval-ms", required_argument, NULL, 'i'},
		{"stat-file", required_argument, NULL, 's'},
		{"help", no_argument, NULL, 'h'}, {NULL, 0, NULL, 0}
	};
	int option;
	while ((option = getopt_long(argc, argv, "i:s:h", options, NULL)) != -1) {
		if (option == 'h') {
			puts("Usage: cpu_sample [--interval-ms MS] [--stat-file FILE]\n"
			     "Write cumulative CPU ticks with CLOCK_MONOTONIC timestamps; TERM/INT saves a final sample.");
			return 0;
		}
		if (option == 's') stat_file = optarg;
		else if (option != 'i' || unsigned_value(optarg, &interval_ms) || !interval_ms || interval_ms > 3600000) return 2;
	}
	if (optind != argc) return 2;
	/* Pending stop signals are consumed atomically with the wait, including
	 * a signal received just after sampling and before sleeping. */
	sigset_t stops;
	sigemptyset(&stops); sigaddset(&stops, SIGTERM); sigaddset(&stops, SIGINT);
	if (sigprocmask(SIG_BLOCK, &stops, NULL)) { perror("block stop signals"); return 1; }
	if (puts("monotonic_ns,cpu,user,nice,system,idle,iowait,irq,softirq,steal") == EOF || snapshot(stat_file)) {
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
			if (snapshot(stat_file)) { perror("final CPU snapshot"); return 1; }
			return 0;
		}
		if (received < 0 && errno == EINTR) continue;
		if (received < 0 && errno != EAGAIN) { perror("CPU sample wait"); return 1; }
		if (snapshot(stat_file) || mono_ns(&now)) { perror("CPU snapshot"); return 1; }
		/* Do not emit a burst of catch-up reads after scheduler delays. */
		deadline += interval;
		if (deadline <= now) deadline = now + interval;
	}
}
