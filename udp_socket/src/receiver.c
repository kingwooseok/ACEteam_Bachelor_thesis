#define _GNU_SOURCE

/**
 * @file receiver.c
 * @brief 일반 Linux UDP socket의 OWD sample을 메모리에 기록하는 수신 프로그램.
 *
 * 실행 단계는 다음과 같음.
 *  1. CLI 인자를 검증하고 UDP socket을 bind함.
 *  2. tmpfs 파일을 전체 예상 sample 크기로 미리 확장함.
 *  3. 파일을 mmap하고 mlockall()과 pre-touch로 page fault를 사전에 처리함.
 *  4. recvmsg() 직후 CLOCK_MONOTONIC/REALTIME과 raw HW timestamp를 기록함.
 *  5. run 종료 후 실제 record만 persistent samples.bin으로 복사하고 fsync함.
 *
 * 수신 hot path에는 disk I/O, 동적 메모리 할당, packet별 로그, mutex가 없음.
 * packet ID와 TX/RX 원시 시각을 저장하며 OWD 계산은 offline에서 수행함.
 * 일반 socket 경로의 user 도달 시점을 XDP/CPUMAP/AF_XDP의 기록과 비교하기 위한
 * 수신기다. NIC 수신 시각과 recvmsg 반환 시각은 서로 다른 측정 지점이다.
 */

#include <arpa/inet.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/errqueue.h>
#include <linux/magic.h>
#include <net/if.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <time.h>
#include <unistd.h>

#include "config.h"
#include "protocol.h"
#include "ace_hwtstamp.h"

/** @brief 한 번의 receiver run에 적용할 CLI와 저장 경로 설정. */
struct receiver_config {
	const char *bind_address;
	const char *tmpfs_path;
	const char *result_path;
	const char *interface_name;
	uint16_t port;
	uint32_t flow_id;
	uint32_t payload_size;
	uint64_t expected_samples;
	bool require_hwts;
	bool flow_id_set;
};

/** @brief SIGINT/SIGTERM 수신 시 수신 loop 종료를 요청하는 플래그. */
static volatile sig_atomic_t stop;

/* =========================================================================
 * #### 종료 신호 처리
 *
 * signal handler는 플래그만 변경함. 이미 수신한 sample의 동기화와 최종 파일
 * 저장은 recvmsg()가 EINTR로 반환된 뒤 main()의 정상 실행 경로에서 수행함.
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
		"  -f, --flow-id ID         expected packet flow ID (required)\n"
		"  -s, --payload-size BYTES expected experiment payload (default: %d)\n"
		"  -n, --count COUNT        valid samples to receive (default: %d)\n"
		"  -t, --tmpfs PATH         staging file (default: %s)\n"
		"  -o, --output PATH        final binary file (default: %s)\n"
		"  -I, --interface IFACE    set RX HW timestamp filter on interface\n"
		"  -H, --require-hwts       fail run if any valid packet lacks HWTS\n",
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

	if (!text || !text[0])
		return -1;
	for (const unsigned char *cursor = (const unsigned char *)text;
	     *cursor; cursor++)
		if (*cursor < '0' || *cursor > '9')
			return -1;
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
		{"flow-id", required_argument, NULL, 'f'},
		{"payload-size", required_argument, NULL, 's'},
		{"count", required_argument, NULL, 'n'},
		{"tmpfs", required_argument, NULL, 't'},
		{"output", required_argument, NULL, 'o'},
		{"interface", required_argument, NULL, 'I'},
		{"require-hwts", no_argument, NULL, 'H'},
		{"help", no_argument, NULL, 'h'},
		{NULL, 0, NULL, 0},
	};
	int option;
	uint64_t value;

	while ((option = getopt_long(argc, argv, "a:p:f:s:n:t:o:I:Hh", options, NULL)) != -1) {
		switch (option) {
		case 'a':
			config->bind_address = optarg;
			break;
		case 'p':
			if (parse_u64(optarg, 1, UINT16_MAX, &value))
				return -1;
			config->port = (uint16_t)value;
			break;
		case 'f':
			if (parse_u64(optarg, 0, UINT32_MAX, &value))
				return -1;
			config->flow_id = (uint32_t)value;
			config->flow_id_set = true;
			break;
		case 's':
			if (parse_u64(optarg, 0,
				ACE_MAX_UDP_PAYLOAD - sizeof(struct ace_experiment_header), &value))
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
		case 'I':
			if (!*optarg || strlen(optarg) >= IFNAMSIZ)
				return -1;
			config->interface_name = optarg;
			break;
		case 'H':
			config->require_hwts = true;
			break;
		case 'h':
			usage(argv[0]);
			exit(0);
		default:
			return -1;
		}
	}
	return config->flow_id_set && optind == argc ? 0 : -1;
}

/* =========================================================================
 * #### 시각 측정과 종료 후 결과 저장
 *
 * realtime_ns()는 recvmsg() 직후에만 호출됨. save_records()의 write/fsync는
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

/** @brief CLOCK_MONOTONIC을 ns 단위로 반환함. */
static int64_t monotonic_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now))
		return -1;
	return (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec;
}

/**
 * @brief recvmsg control message에서 raw hardware RX timestamp를 찾음.
 * @return 유효한 hardware timestamp가 있으면 true, 없으면 false.
 */
static bool extract_hw_timestamp(const struct msghdr *message, int64_t *timestamp_ns)
{
	struct cmsghdr *control;

	for (control = CMSG_FIRSTHDR((struct msghdr *)message); control;
	     control = CMSG_NXTHDR((struct msghdr *)message, control)) {
		bool is_timestamp = control->cmsg_level == SOL_SOCKET &&
			control->cmsg_type == SCM_TIMESTAMPING;
		struct scm_timestamping timestamps;
		const struct timespec *hardware;

#ifdef SCM_TIMESTAMPING_NEW
		is_timestamp = is_timestamp ||
			(control->cmsg_level == SOL_SOCKET &&
			 control->cmsg_type == SCM_TIMESTAMPING_NEW);
#endif
		if (!is_timestamp ||
		    control->cmsg_len < CMSG_LEN(sizeof(timestamps)))
			continue;

		/*
		 * SCM_TIMESTAMPING의 ts[2]가 변환하지 않은 NIC PHC 시각이다.
		 * ts[0]의 software timestamp로 대신 채우면 HW→user라는 측정 구간이
		 * 달라진다. HW 값이 없을 때는 없다고 기록하고 offline에서 구분한다.
		 */
		memcpy(&timestamps, CMSG_DATA(control), sizeof(timestamps));
		hardware = &timestamps.ts[2];
		if (hardware->tv_sec < 0 || hardware->tv_nsec < 0 ||
		    hardware->tv_nsec >= 1000000000L ||
		    (hardware->tv_sec == 0 && hardware->tv_nsec == 0))
			return false;
		if ((uint64_t)hardware->tv_sec >
		    (uint64_t)INT64_MAX / 1000000000ULL)
			return false;

		*timestamp_ns = (int64_t)hardware->tv_sec * 1000000000LL +
			hardware->tv_nsec;
		return true;
	}

	return false;
}

/** @brief SO_RXQ_OVFL ancillary message에서 누적 socket drop 수를 읽음. */
static bool extract_rxq_overflow(const struct msghdr *message, uint32_t *drops)
{
	struct cmsghdr *control;

	for (control = CMSG_FIRSTHDR((struct msghdr *)message); control;
	     control = CMSG_NXTHDR((struct msghdr *)message, control)) {
		if (control->cmsg_level != SOL_SOCKET ||
		    control->cmsg_type != SO_RXQ_OVFL ||
		    control->cmsg_len < CMSG_LEN(sizeof(*drops)))
			continue;
		memcpy(drops, CMSG_DATA(control), sizeof(*drops));
		return true;
	}
	return false;
}

/**
 * @brief mmap buffer의 유효 record를 persistent 결과 파일로 저장함.
 * @param fd 이미 O_EXCL로 생성한 결과 파일 descriptor.
 * @param records 저장할 첫 record 주소.
 * @param bytes 저장할 유효 byte 수.
 * @return write와 fsync가 성공하면 0, 실패하면 -1. close는 호출자가 수행함.
 * @note 이 함수는 수신 hot path가 종료된 뒤에만 호출함.
 */
static int save_records(int fd, const struct sample_record *records, size_t bytes)
{
	const uint8_t *cursor = (const uint8_t *)records;
	size_t remaining = bytes;
	while (remaining > 0) {
		ssize_t written = write(fd, cursor, remaining);
		if (written < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		if (written == 0) {
			errno = EIO;
			return -1;
		}
		cursor += written;
		remaining -= (size_t)written;
	}
	if (fsync(fd))
		return -1;
	return 0;
}

static int ensure_path_absent(const char *path)
{
	struct stat status;

	if (!lstat(path, &status)) {
		errno = EEXIST;
		return -1;
	}
	return errno == ENOENT ? 0 : -1;
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
	struct ace_hwtstamp_state hwtstamp = {0};
	uint8_t packet[ACE_MAX_UDP_PAYLOAD];
	uint64_t received = 0, invalid = 0, truncated = 0;
	uint64_t missing_hwts = 0, receive_errors = 0;
	uint32_t socket_drops = 0;
	size_t expected_packet_size, mapped_bytes, result_bytes;
	int timestamping_flags = SOF_TIMESTAMPING_RX_HARDWARE |
		SOF_TIMESTAMPING_RAW_HARDWARE;
	int enable_rxq_overflow = 1;
	int socket_fd = -1, tmpfs_fd = -1, result_fd = -1, exit_status = 1;

	if (parse_arguments(argc, argv, &config)) {
		usage(argv[0]);
		return 2;
	}
	if (!strcmp(config.tmpfs_path, config.result_path)) {
		fprintf(stderr, "tmpfs and output paths must be different\n");
		return 2;
	}
	if (ensure_path_absent(config.tmpfs_path)) {
		perror("tmpfs staging path already exists or is not usable");
		return 2;
	}
	if (ensure_path_absent(config.result_path)) {
		perror("result path already exists or is not usable");
		return 2;
	}
	local_address.sin_port = htons(config.port);
	expected_packet_size = sizeof(struct ace_experiment_header) + config.payload_size;
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
	/*
	 * socket 옵션은 드라이버가 skb에 넣은 HW 시각을 ancillary data로 전달받는
	 * 요청이다. NIC 자체의 timestamp 수집 설정은 아래 -I 처리와 별도다.
	 * 포팅한 드라이버의 RX descriptor → skb HW timestamp → recvmsg control
	 * message 전달 경로가 여기서 일반 socket 측정과 연결된다.
	 */
	if (setsockopt(socket_fd, SOL_SOCKET, SO_TIMESTAMPING,
		&timestamping_flags, sizeof(timestamping_flags))) {
		perror("setsockopt(SO_TIMESTAMPING)");
		goto out;
	}
	if (setsockopt(socket_fd, SOL_SOCKET, SO_RXQ_OVFL,
		&enable_rxq_overflow, sizeof(enable_rxq_overflow))) {
		perror("setsockopt(SO_RXQ_OVFL)");
		goto out;
	}
	if (config.interface_name) {
		/* 공통 helper가 기존 NIC 설정을 기억하고 RX ALL을 요청하며, 종료 시 복원한다. */
		int timestamp_err = ace_hwtstamp_enable(socket_fd,
			config.interface_name, &hwtstamp);

		if (timestamp_err) {
			fprintf(stderr, "configure interface hardware timestamping: %s\n",
				strerror(-timestamp_err));
			goto out;
		}
		if (setsockopt(socket_fd, SOL_SOCKET, SO_BINDTODEVICE,
			config.interface_name, strlen(config.interface_name) + 1)) {
			perror("setsockopt(SO_BINDTODEVICE)");
			goto out;
		}
	}
	if (bind(socket_fd, (const struct sockaddr *)&local_address,
		sizeof(local_address))) {
		perror("bind");
		goto out;
	}

	/*
	 * tmpfs는 RAM 기반 staging 영역이고 mmap은 이 파일을 배열처럼 쓰게 한다.
	 * 전체 run 크기를 미리 잡고 received 순서로 채우므로 ring처럼 과거 sample을
	 * 덮어쓰지 않는다. sequence를 배열 index로 쓰지 않아 중간 packet이 빠져도
	 * 빈 공간을 만들지 않으며, 실제 packet 식별은 record의 flow_id+seq로 한다.
	 */
	tmpfs_fd = open(config.tmpfs_path,
		O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
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
	result_fd = open(config.result_path,
		O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
	if (result_fd < 0) {
		perror("create result file");
		goto out;
	}

	printf("role: receiver\n"
	       "source/bind: %s\n"
	       "UDP port: %u\n"
	       "flow ID: %u\n"
	       "payload size: %u bytes\n"
	       "expected UDP datagram payload: %zu bytes\n"
	       "expected packet count: %llu\n"
	       "tmpfs location: %s\n"
	       "result location: %s\n"
	       "user clock: CLOCK_MONOTONIC primary, CLOCK_REALTIME secondary\n"
	       "raw hardware timestamp: %s\n",
		config.bind_address, config.port, config.flow_id, config.payload_size,
		expected_packet_size,
		(unsigned long long)config.expected_samples, config.tmpfs_path,
		config.result_path, config.require_hwts ? "required" : "optional");
	if (config.interface_name)
		printf("HWTSTAMP interface: %s, effective RX filter: %d\n",
			hwtstamp.interface_name, hwtstamp.effective.rx_filter);

	/* 측정 구간: recvmsg 직후 시각을 얻고 header 확인과 메모리 기록을 수행함. */
	while (received < config.expected_samples && !stop) {
		union {
			struct cmsghdr alignment;
			uint8_t bytes[CMSG_SPACE(sizeof(struct scm_timestamping)) +
				CMSG_SPACE(sizeof(uint32_t))];
		} control = {0};
		struct iovec io = {
			.iov_base = packet,
			.iov_len = sizeof(packet),
		};
		struct msghdr message = {
			.msg_iov = &io,
			.msg_iovlen = 1,
			.msg_control = control.bytes,
			.msg_controllen = sizeof(control.bytes),
		};
		ssize_t length = recvmsg(socket_fd, &message, 0);
		int64_t user_rx_mono_ns;
		int64_t user_rx_real_ns;
		int64_t hw_rx_ns = 0;
		bool hwts_valid;
		struct ace_experiment_header header;
		struct sample_record record = {0};

		if (length < 0) {
			if (errno == EINTR)
				continue;
			receive_errors++;
			break;
		}
		/*
		 * 먼저 MONOTONIC을 읽어 packet 파싱과 cmsg 해석 시간을 측정 끝점에서
		 * 제외한다. 이 시각까지는 수신 stack, socket 대기열, task wakeup 지연이
		 * 포함된다. 뒤의 REALTIME 읽기는 별도 끝점이므로 두 값이 동시 측정은 아니다.
		 * MONOTONIC은 BPF의 bpf_ktime_get_ns() 기록과 같은 clock domain이다.
		 */
		user_rx_mono_ns = monotonic_ns();
		if (user_rx_mono_ns < 0) {
			receive_errors++;
			break;
		}
		user_rx_real_ns = realtime_ns();
		if (user_rx_real_ns < 0) {
			receive_errors++;
			break;
		}
		(void)extract_rxq_overflow(&message, &socket_drops);
		if (message.msg_flags & MSG_TRUNC) {
			truncated++;
			invalid++;
			continue;
		}
		if ((size_t)length != expected_packet_size) {
			invalid++;
			continue;
		}

		/* wire header를 정렬 안전하게 복사한 뒤 host byte order로 변환함. */
		memcpy(&header, packet, sizeof(header));
		if (be32toh(header.magic) != ACE_PACKET_MAGIC ||
		    be16toh(header.version) != ACE_PACKET_VERSION ||
		    be16toh(header.size) != sizeof(header) ||
		    be32toh(header.flow_id) != config.flow_id ||
		    header.reserved != 0) {
			invalid++;
			continue;
		}

		hwts_valid = !(message.msg_flags & MSG_CTRUNC) &&
			extract_hw_timestamp(&message, &hw_rx_ns);
		if (!hwts_valid)
			missing_hwts++;

		/*
		 * wire 값만 host endian으로 풀고 clock은 원래 domain 그대로 보존한다.
		 * hw_rx_ns를 user_rx_mono_ns에서 직접 빼면 서로 다른 시계의 offset이
		 * 지연처럼 섞인다. PHC calibration으로 기준을 맞추는 일은 offline 몫이다.
		 * user_rx_real_ns - tx_ns 역시 송·수신 장비의 REALTIME 동기화가 전제다.
		 */
		record.seq = be64toh(header.sequence);
		record.tx_ns = (int64_t)be64toh(header.tx_realtime_ns);
		record.user_rx_mono_ns = user_rx_mono_ns;
		record.user_rx_real_ns = user_rx_real_ns;
		record.hw_rx_ns = hw_rx_ns;
		record.flow_id = config.flow_id;
		record.len = (uint32_t)length;
		if (hwts_valid)
			record.flags |= ACE_SAMPLE_F_HWTS_VALID;
		if (message.msg_flags & MSG_CTRUNC)
			record.flags |= ACE_SAMPLE_F_CMSG_TRUNCATED;
		records[received] = record;
		received++;
	}

	/*
	 * 측정이 끝난 뒤 유효 record만 디스크로 옮긴다. tmpfs의 msync만으로
	 * 영구 저장되는 것은 아니며 save_records의 write/fsync가 그 역할을 맡는다.
	 * 따라서 저장 장치의 지연이 packet별 RX timestamp에 끼어들지 않는다.
	 */
	result_bytes = (size_t)received * sizeof(*records);
	if (result_bytes > 0 && msync(records, result_bytes, MS_SYNC)) {
		perror("msync tmpfs records");
		goto out;
	}
	if (save_records(result_fd, records, result_bytes)) {
		perror("save result file");
		goto out;
	}
	{
		int close_result = close(result_fd);

		result_fd = -1;
		if (close_result) {
			perror("close result file");
			goto out;
		}
	}
	if (ftruncate(tmpfs_fd, (off_t)result_bytes)) {
		perror("truncate tmpfs file to recorded samples");
		goto out;
	}

	printf("completed: samples=%llu invalid=%llu truncated=%llu "
	       "missing_hwts=%llu socket_drops=%u receive_errors=%llu bytes=%zu\n",
		(unsigned long long)received, (unsigned long long)invalid,
		(unsigned long long)truncated,
		(unsigned long long)missing_hwts,
		socket_drops, (unsigned long long)receive_errors, result_bytes);
	exit_status = receive_errors || (config.require_hwts && missing_hwts) ? 1 : 0;

out:
	if (records != MAP_FAILED)
		munmap(records, mapped_bytes);
	if (tmpfs_fd >= 0 && close(tmpfs_fd)) {
		perror("close tmpfs staging file");
		exit_status = 1;
	}
	if (result_fd >= 0 && close(result_fd)) {
		perror("close result file");
		exit_status = 1;
	}
	if (socket_fd >= 0) {
		int restore_err = ace_hwtstamp_restore(socket_fd, &hwtstamp);

		if (restore_err < 0) {
			fprintf(stderr, "restore interface hardware timestamping: %s\n",
				strerror(-restore_err));
			exit_status = 1;
		} else if (restore_err > 0) {
			fprintf(stderr,
				"warning: HWTSTAMP configuration changed externally; not restoring stale settings\n");
			exit_status = 1;
		}
	}
	if (socket_fd >= 0 && close(socket_fd)) {
		perror("close UDP socket");
		exit_status = 1;
	}
	munlockall();
	return exit_status;
}
