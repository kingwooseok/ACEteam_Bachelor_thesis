#define _GNU_SOURCE

#include <arpa/inet.h>
#include <endian.h>
#include <errno.h>
#include <getopt.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "config.h"
#include "protocol.h"

struct sender_config {
	const char *destination;
	uint16_t port;
	uint32_t payload_size;
	uint64_t period_ns;
	uint64_t packet_count;
};

static volatile sig_atomic_t stop;

static void on_signal(int signo)
{
	(void)signo;
	stop = 1;
}

static int install_signal_handlers(void)
{
	struct sigaction action = {
		.sa_handler = on_signal,
	};

	sigemptyset(&action.sa_mask);
	return sigaction(SIGINT, &action, NULL) ||
		sigaction(SIGTERM, &action, NULL);
}

static void usage(const char *program)
{
	fprintf(stderr,
		"Usage: %s [options] DESTINATION_IPV4\n"
		"  -p, --port PORT          UDP port (default: %d)\n"
		"  -s, --payload-size BYTES experiment payload bytes (default: %d)\n"
		"  -n, --count COUNT        packets to send (default: %d)\n"
		"  -i, --period-ns NS       send period (default: %d)\n",
		program, ACE_DEFAULT_UDP_PORT, ACE_DEFAULT_PAYLOAD_SIZE,
		ACE_DEFAULT_SAMPLE_COUNT, ACE_DEFAULT_PERIOD_NS);
}

static int parse_u64(const char *text, uint64_t minimum, uint64_t maximum,
		uint64_t *value)
{
	char *end;
	unsigned long long parsed;

	errno = 0;
	parsed = strtoull(text, &end, 10);
	if (errno || text == end || *end != '\0' || parsed < minimum ||
		parsed > maximum)
		return -1;
	*value = (uint64_t)parsed;
	return 0;
}

static int parse_arguments(int argc, char **argv, struct sender_config *config)
{
	static const struct option options[] = {
		{"port", required_argument, NULL, 'p'},
		{"payload-size", required_argument, NULL, 's'},
		{"count", required_argument, NULL, 'n'},
		{"period-ns", required_argument, NULL, 'i'},
		{"help", no_argument, NULL, 'h'},
		{NULL, 0, NULL, 0},
	};
	int option;
	uint64_t value;

	while ((option = getopt_long(argc, argv, "p:s:n:i:h", options, NULL)) != -1) {
		switch (option) {
		case 'p':
			if (parse_u64(optarg, 1, UINT16_MAX, &value))
				return -1;
			config->port = (uint16_t)value;
			break;
		case 's':
			if (parse_u64(optarg, 0,
				ACE_MAX_UDP_PAYLOAD - sizeof(struct packet_header), &value))
				return -1;
			config->payload_size = (uint32_t)value;
			break;
		case 'n':
			if (parse_u64(optarg, 1, UINT64_MAX, &config->packet_count))
				return -1;
			break;
		case 'i':
			if (parse_u64(optarg, 1, INT64_MAX, &config->period_ns))
				return -1;
			break;
		case 'h':
			usage(argv[0]);
			exit(0);
		default:
			return -1;
		}
	}
	if (optind + 1 != argc)
		return -1;
	config->destination = argv[optind];
	return 0;
}

static int64_t realtime_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_REALTIME, &now))
		return -1;
	return (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec;
}

static void add_ns(struct timespec *time, uint64_t nanoseconds)
{
	time->tv_sec += (time_t)(nanoseconds / 1000000000ULL);
	time->tv_nsec += (long)(nanoseconds % 1000000000ULL);
	if (time->tv_nsec >= 1000000000L) {
		time->tv_sec++;
		time->tv_nsec -= 1000000000L;
	}
}

static int sleep_until(const struct timespec *deadline)
{
	int result;

	do {
		result = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, deadline, NULL);
	} while (result == EINTR && !stop);
	return result;
}

int main(int argc, char **argv)
{
	struct sender_config config = {
		.port = ACE_DEFAULT_UDP_PORT,
		.payload_size = ACE_DEFAULT_PAYLOAD_SIZE,
		.period_ns = ACE_DEFAULT_PERIOD_NS,
		.packet_count = ACE_DEFAULT_SAMPLE_COUNT,
	};
	struct sockaddr_in destination = {
		.sin_family = AF_INET,
	};
	uint8_t packet[ACE_MAX_UDP_PAYLOAD];
	size_t packet_size;
	struct timespec next_send;
	uint64_t sent = 0, errors = 0;
	int socket_fd;

	if (parse_arguments(argc, argv, &config)) {
		usage(argv[0]);
		return 2;
	}
	destination.sin_port = htons(config.port);
	packet_size = sizeof(struct packet_header) + config.payload_size;
	if (inet_pton(AF_INET, config.destination, &destination.sin_addr) != 1) {
		fprintf(stderr, "invalid destination IPv4 address: %s\n",
			config.destination);
		return 2;
	}

	socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (socket_fd < 0) {
		perror("socket");
		return 1;
	}
	memset(packet, 0, sizeof(packet));
	if (mlockall(MCL_CURRENT | MCL_FUTURE)) {
		perror("mlockall");
		close(socket_fd);
		return 1;
	}
	if (install_signal_handlers()) {
		perror("sigaction");
		close(socket_fd);
		return 1;
	}

	printf("role: sender\n"
	       "destination: %s\n"
	       "UDP port: %u\n"
	       "period: %llu ns\n"
	       "payload size: %u bytes\n"
	       "UDP datagram payload: %zu bytes (header + payload)\n"
	       "packet count: %llu\n"
	       "clock: CLOCK_REALTIME (TX), CLOCK_MONOTONIC (pacing)\n",
		config.destination, config.port,
		(unsigned long long)config.period_ns, config.payload_size, packet_size,
		(unsigned long long)config.packet_count);

	if (clock_gettime(CLOCK_MONOTONIC, &next_send)) {
		perror("clock_gettime(CLOCK_MONOTONIC)");
		close(socket_fd);
		return 1;
	}

	for (uint64_t seq = 0; seq < config.packet_count && !stop; seq++) {
		struct packet_header header;
		int64_t tx_ns;
		ssize_t result;

		if (seq != 0) {
			add_ns(&next_send, config.period_ns);
			int sleep_result = sleep_until(&next_send);
			if (sleep_result && sleep_result != EINTR) {
				errno = sleep_result;
				perror("clock_nanosleep");
				errors++;
				break;
			}
			if (stop)
				break;
		}

		tx_ns = realtime_ns();
		if (tx_ns < 0) {
			errors++;
			break;
		}
		header.seq = htobe64(seq);
		header.tx_ns = (int64_t)htobe64((uint64_t)tx_ns);
		memcpy(packet, &header, sizeof(header));
		result = sendto(socket_fd, packet, packet_size, 0,
			(const struct sockaddr *)&destination, sizeof(destination));
		if (result == (ssize_t)packet_size)
			sent++;
		else
			errors++;
	}

	printf("completed: sent=%llu errors=%llu\n",
		(unsigned long long)sent, (unsigned long long)errors);
	close(socket_fd);
	munlockall();
	return errors ? 1 : 0;
}
