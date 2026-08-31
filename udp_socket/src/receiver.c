#define _GNU_SOURCE

/**
 * @file receiver.c
 * @brief 일반 Linux UDP socket의 OWD sample을 메모리에 기록하는 수신 프로그램.
 *
 * 실행 단계는 다음과 같음.
 *  1. CLI 인자를 검증하고 UDP socket을 bind함.
 *  2. tmpfs 파일을 전체 예상 sample 크기로 미리 확장함.
 *  3. 파일을 mmap하고 mlockall()과 pre-touch로 page fault를 사전에 처리함.
 *  4. recv() 직후 CLOCK_REALTIME RX 시각을 측정해 linear buffer에 기록함.
 *  5. run 종료 후 실제 record만 persistent samples.bin으로 복사하고 fsync함.
 *
 * 수신 hot path에는 disk I/O, 동적 메모리 할당, packet별 로그, mutex가 없음.
 * sequence number와 TX/RX 원시 시각만 저장하며 OWD 계산은 offline에서 수행함.
 */

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

/** @brief 한 번의 receiver run에 적용할 CLI와 저장 경로 설정. */
struct receiver_config {
	const char *bind_address;
	const char *tmpfs_path;
	const char *result_path;
	uint16_t port;
	uint32_t payload_size;
	uint64_t expected_samples;
};

/** @brief SIGINT/SIGTERM 수신 시 수신 loop 종료를 요청하는 플래그. */
static volatile sig_atomic_t stop;

/* =========================================================================
 * #### 종료 신호 처리
 *
 * signal handler는 플래그만 변경함. 이미 수신한 sample의 동기화와 최종 파일
 * 저장은 recv()가 EINTR로 반환된 뒤 main()의 정상 실행 경로에서 수행함.
 * ========================================================================= */

/**
 * @brief SIGINT 또는 SIGTERM 수신 사실을 수신 loop에 전달함.
 * @param signo 수신한 신호 번호. 현재는 종류와 관계없이 종료 요청으로 처리함.
 * @return 반환값 없음.
 */
static void on_signal(int signo)
{
	(void)signo;
	stop = 1;
}

/**
 * @brief SIGINT와 SIGTERM handler를 등록함.
 * @return 두 handler가 모두 등록되면 0, 실패하면 0이 아닌 값.
 */
static int install_signal_handlers(void)
{
	struct sigaction action = {
		.sa_handler = on_signal,
	};

	sigemptyset(&action.sa_mask);
	return sigaction(SIGINT, &action, NULL) ||
		sigaction(SIGTERM, &action, NULL);
}

/* =========================================================================
 * #### CLI 설정과 입력값 검증
 *
 * bind 주소, port, payload 크기, sample 수, staging/output 경로를 run마다
 * 지정할 수 있음. buffer 크기 overflow와 잘못된 범위는 setup 전에 차단함.
 * ========================================================================= */

/**
 * @brief receiver 실행 옵션과 기본값을 표준 오류에 출력함.
 * @param program 실행 파일 이름.
 * @return 반환값 없음.
 */
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

/**
 * @brief 문자열을 지정 범위의 부호 없는 64-bit 정수로 변환함.
 * @param text 변환할 문자열.
 * @param minimum 허용할 최솟값.
 * @param maximum 허용할 최댓값.
 * @param value 변환 결과를 저장할 위치.
 * @return 성공 시 0, 형식 오류·범위 초과 시 -1.
 */
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

/**
 * @brief receiver CLI 인자를 해석하여 실행 설정을 완성함.
 * @param argc main()에서 전달받은 인자 개수.
 * @param argv main()에서 전달받은 인자 배열.
 * @param config 기본값이 채워진 설정 구조체. 지정된 옵션만 덮어씀.
 * @return 유효한 설정이면 0, 잘못된 옵션이나 범위이면 -1.
 */
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

/* =========================================================================
 * #### 시각 측정과 종료 후 결과 저장
 *
 * realtime_ns()는 recv() 직후에만 호출됨. save_records()의 write/fsync는
 * 수신 loop가 완전히 끝난 다음에만 실행되어 측정 경로에 storage I/O를 넣지 않음.
 * ========================================================================= */

/**
 * @brief PTP 동기화 대상인 CLOCK_REALTIME을 ns 단위로 반환함.
 * @return 성공 시 Unix epoch 기준 ns, 실패 시 -1.
 */
static int64_t realtime_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_REALTIME, &now))
		return -1;
	return (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec;
}

/**
 * @brief mmap buffer의 유효 record를 persistent 결과 파일로 저장함.
 * @param path 생성할 결과 파일 경로.
 * @param records 저장할 첫 record 주소.
 * @param bytes 저장할 유효 byte 수.
 * @return write, fsync, close까지 성공하면 0, 실패하면 -1.
 * @note 이 함수는 수신 hot path가 종료된 뒤에만 호출함.
 */
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

/* =========================================================================
 * #### 수신 프로그램 실행 흐름
 *
 * socket과 전체 linear buffer를 먼저 준비한 뒤 수신 loop에 진입함. 예상 sample
 * 수에 도달하거나 종료 신호를 받으면 실제 기록량만 tmpfs와 결과 파일에 남김.
 * ========================================================================= */

/**
 * @brief UDP packet을 수신하여 고정 크기 sample record로 저장함.
 * @param argc CLI 인자 개수.
 * @param argv CLI 인자 배열.
 * @return 정상 완료 시 0, CLI 오류 시 2, setup·수신·저장 오류 시 1.
 */
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

	/* 일반 Linux IPv4 UDP socket을 생성하고 지정 주소와 port에 bind함. */
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

	/* 실험 전에 전체 run 크기의 덮어쓰기 없는 linear buffer를 tmpfs에 확보함. */
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
	/* mmap 영역의 모든 page를 미리 접근하여 측정 중 page fault를 방지함. */
	memset(records, 0, mapped_bytes);
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

	/* 측정 구간: recv 직후 시각을 얻고 record 대입과 index 증가만 수행함. */
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

		/* wire header를 정렬 안전하게 복사한 뒤 host byte order로 변환함. */
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

	/* 측정 종료 후에만 tmpfs 동기화와 persistent storage 저장을 수행함. */
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
