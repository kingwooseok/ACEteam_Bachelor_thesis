#define _GNU_SOURCE

#include <arpa/inet.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/magic.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <time.h>
#include <unistd.h>

#include "config.h"
#include "protocol.h"

struct receiver_config {
	const char *bind_address;
	const char *tmpfs_path;
	const char *result_path;
	uint16_t port;
	uint32_t payload_size;
	uint64_t expected_samples;
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
		"Usage: %s [options]\n"
		"  -a, --bind ADDRESS       local IPv4 address (default: %s)\n"
		"  -p, --port PORT          UDP port (default: %d)\n"
		"  -s, --payload-size BYTES expected experiment payload (default: %d)\n"
		"  -n, --count COUNT        valid samples to receive (default: %d)\n"
		"  -t, --tmpfs PATH         staging file (default: %s)\n"
		"  -o, --output PATH        final binary file (default: %s)\n",
		program, ACE_DEFAULT_BIND_ADDRESS, ACE_DEFAULT_UDP_PORT,
		ACE_DEFAULT_PAYLOAD_SIZE,
		ACE_DEFAULT_SAMPLE_COUNT, ACE_DEFAULT_TMPFS_PATH,
		ACE_DEFAULT_RESULT_PATH);
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

static int parse_arguments(int argc, char **argv, struct receiver_config *config)
{
	static const struct option options[] = {
		{"bind", required_argument, NULL, 'a'},
		{"port", required_argument, NULL, 'p'},
		{"payload-size", required_argument, NULL, 's'},
		{"count", required_argument, NULL, 'n'},
		{"tmpfs", required_argument, NULL, 't'},
		{"output", required_argument, NULL, 'o'},
		{"help", no_argument, NULL, 'h'},
		{NULL, 0, NULL, 0},
	};
	int option;
	uint64_t value;

	while ((option = getopt_long(argc, argv, "a:p:s:n:t:o:h", options, NULL)) != -1) {
		switch (option) {
		case 'a':
			config->bind_address = optarg;
			break;
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
			if (parse_u64(optarg, 1, INT64_MAX / sizeof(struct sample_record),
				&config->expected_samples))
				return -1;
			break;
		case 't':
			config->tmpfs_path = optarg;
			break;
		case 'o':
			config->result_path = optarg;
			break;
		case 'h':
			usage(argv[0]);
			exit(0);
		default:
			return -1;
		}
	}
	return optind == argc ? 0 : -1;
}

static int64_t realtime_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_REALTIME, &now))
		return -1;
	return (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec;
}

static int save_records(const char *path, const struct sample_record *records,
		size_t bytes)
{
	const uint8_t *cursor = (const uint8_t *)records;
	size_t remaining = bytes;
	int saved_errno;
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);

	if (fd < 0)
		return -1;
	while (remaining > 0) {
		ssize_t written = write(fd, cursor, remaining);
		if (written < 0) {
			if (errno == EINTR)
				continue;
			close(fd);
			return -1;
		}
		if (written == 0) {
			close(fd);
			errno = EIO;
			return -1;
		}
		cursor += written;
		remaining -= (size_t)written;
	}
	if (fsync(fd)) {
		saved_errno = errno;
		close(fd);
		errno = saved_errno;
		return -1;
	}
	if (close(fd))
		return -1;
	return 0;
}

int main(int argc, char **argv)
{
	struct receiver_config config = {
		.bind_address = ACE_DEFAULT_BIND_ADDRESS,
		.tmpfs_path = ACE_DEFAULT_TMPFS_PATH,
		.result_path = ACE_DEFAULT_RESULT_PATH,
		.port = ACE_DEFAULT_UDP_PORT,
		.payload_size = ACE_DEFAULT_PAYLOAD_SIZE,
		.expected_samples = ACE_DEFAULT_SAMPLE_COUNT,
	};
	struct sockaddr_in local_address = {
		.sin_family = AF_INET,
	};
	struct sample_record *records = MAP_FAILED;
	uint8_t packet[ACE_MAX_UDP_PAYLOAD];
	uint64_t received = 0, invalid = 0, receive_errors = 0;
	size_t expected_packet_size, mapped_bytes, result_bytes;
	int socket_fd = -1, tmpfs_fd = -1, exit_status = 1;

	if (parse_arguments(argc, argv, &config)) {
		usage(argv[0]);
		return 2;
	}
	if (!strcmp(config.tmpfs_path, config.result_path)) {
		fprintf(stderr, "tmpfs and output paths must be different\n");
		return 2;
	}
	local_address.sin_port = htons(config.port);
	expected_packet_size = sizeof(struct packet_header) + config.payload_size;
	if (inet_pton(AF_INET, config.bind_address, &local_address.sin_addr) != 1) {
		fprintf(stderr, "invalid bind IPv4 address: %s\n", config.bind_address);
		return 2;
	}
	mapped_bytes = (size_t)config.expected_samples * sizeof(*records);

	socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (socket_fd < 0) {
		perror("socket");
		goto out;
	}
	if (bind(socket_fd, (const struct sockaddr *)&local_address,
		sizeof(local_address))) {
		perror("bind");
		goto out;
	}

	/* Allocate the complete linear run buffer in tmpfs before receiving. */
	tmpfs_fd = open(config.tmpfs_path,
		O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
	if (tmpfs_fd < 0) {
		perror("open tmpfs staging file");
		goto out;
	}
	{
		struct statfs filesystem;

		if (fstatfs(tmpfs_fd, &filesystem)) {
			perror("fstatfs tmpfs staging file");
			goto out;
		}
		if ((unsigned long)filesystem.f_type != TMPFS_MAGIC) {
			fprintf(stderr, "staging path is not on tmpfs: %s\n",
				config.tmpfs_path);
			goto out;
		}
	}
	if (ftruncate(tmpfs_fd, (off_t)mapped_bytes)) {
		perror("ftruncate tmpfs staging file");
		goto out;
	}
	records = mmap(NULL, mapped_bytes, PROT_READ | PROT_WRITE, MAP_SHARED,
		tmpfs_fd, 0);
	if (records == MAP_FAILED) {
		perror("mmap tmpfs staging file");
		goto out;
	}
	if (mlockall(MCL_CURRENT | MCL_FUTURE)) {
		perror("mlockall");
		goto out;
	}
	memset(records, 0, mapped_bytes); /* Pre-touch every mapped page. */
	memset(packet, 0, sizeof(packet));
	if (install_signal_handlers()) {
		perror("sigaction");
		goto out;
	}

	printf("role: receiver\n"
	       "source/bind: %s\n"
	       "UDP port: %u\n"
	       "payload size: %u bytes\n"
	       "expected UDP datagram payload: %zu bytes\n"
	       "expected packet count: %llu\n"
	       "tmpfs location: %s\n"
	       "result location: %s\n"
	       "clock: CLOCK_REALTIME (RX)\n",
		config.bind_address, config.port, config.payload_size,
		expected_packet_size,
		(unsigned long long)config.expected_samples, config.tmpfs_path,
		config.result_path);

	while (received < config.expected_samples && !stop) {
		ssize_t length = recv(socket_fd, packet, sizeof(packet), 0);
		int64_t rx_ns;
		struct packet_header header;
		struct sample_record record;

		if (length < 0) {
			if (errno == EINTR)
				continue;
			receive_errors++;
			break;
		}
		rx_ns = realtime_ns();
		if (rx_ns < 0) {
			receive_errors++;
			break;
		}
		if ((size_t)length != expected_packet_size) {
			invalid++;
			continue;
		}

		memcpy(&header, packet, sizeof(header));
		record.seq = be64toh(header.seq);
		record.tx_ns = (int64_t)be64toh((uint64_t)header.tx_ns);
		record.rx_ns = rx_ns;
		record.len = (uint32_t)length;
		record.flags = 0;
		record.reserved = 0;
		records[received] = record;
		received++;
	}

	result_bytes = (size_t)received * sizeof(*records);
	if (result_bytes > 0 && msync(records, result_bytes, MS_SYNC)) {
		perror("msync tmpfs records");
		goto out;
	}
	if (save_records(config.result_path, records, result_bytes)) {
		perror("save result file");
		goto out;
	}
	if (ftruncate(tmpfs_fd, (off_t)result_bytes)) {
		perror("truncate tmpfs file to recorded samples");
		goto out;
	}

	printf("completed: samples=%llu invalid=%llu receive_errors=%llu bytes=%zu\n",
		(unsigned long long)received, (unsigned long long)invalid,
		(unsigned long long)receive_errors, result_bytes);
	exit_status = receive_errors ? 1 : 0;

out:
	if (records != MAP_FAILED)
		munmap(records, mapped_bytes);
	if (tmpfs_fd >= 0)
		close(tmpfs_fd);
	if (socket_fd >= 0)
		close(socket_fd);
	munlockall();
	return exit_status;
}
