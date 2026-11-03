#define _GNU_SOURCE
#include <getopt.h>
#include <stdint.h>
#include <stdio.h>
#include "ace_run_control.h"

int main(int argc, char **argv)
{
	uint64_t after_ms = 0;
	static const struct option options[] = {
		{"after-ms", required_argument, NULL, 'a'},
		{"help", no_argument, NULL, 'h'}, {NULL, 0, NULL, 0}
	};
	int option;
	while ((option = getopt_long(argc, argv, "a:h", options, NULL)) != -1) {
		if (option == 'h') {
			puts("Usage: ace_clock [--after-ms MS]\nPrint this host's CLOCK_MONOTONIC deadline in nanoseconds.");
			return 0;
		}
		if (option != 'a' || ace_parse_u64(optarg, 0, INT64_MAX / 1000000ULL, &after_ms))
			return 2;
	}
	if (optind != argc) return 2;
	int64_t now = ace_mono_ns();
	if (now < 0) { perror("CLOCK_MONOTONIC"); return 1; }
	if (after_ms > ((uint64_t)INT64_MAX - (uint64_t)now) / 1000000ULL) return 2;
	if (printf("%llu\n", (unsigned long long)((uint64_t)now + after_ms * 1000000ULL)) < 0 || fflush(stdout))
		return 1;
	return 0;
}
