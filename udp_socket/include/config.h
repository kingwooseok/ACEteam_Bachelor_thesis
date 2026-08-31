#ifndef ACE_UDP_CONFIG_H
#define ACE_UDP_CONFIG_H

/* Experiment defaults. Per-run values can be overridden with CLI arguments. */
enum {
	ACE_DEFAULT_UDP_PORT = 9000,
	ACE_DEFAULT_PAYLOAD_SIZE = 64,
	ACE_DEFAULT_PERIOD_NS = 1000000,
	ACE_DEFAULT_SAMPLE_COUNT = 200000,
	ACE_MAX_UDP_PAYLOAD = 65507,
};

#define ACE_DEFAULT_BIND_ADDRESS "0.0.0.0"
#define ACE_DEFAULT_TMPFS_PATH   "/dev/shm/ace_udp_samples.tmp"
#define ACE_DEFAULT_RESULT_PATH  "./samples.bin"

#endif
