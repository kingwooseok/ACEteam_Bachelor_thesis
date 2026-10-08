#define _GNU_SOURCE

/**
 * @file sender.c
 * @brief 일반 Linux UDP socket을 사용하는 주기 송신 프로그램.
 *
 * 송신 과정은 다음과 같음.
 *  1. CLI 인자를 검증하고 UDP 목적지를 준비함.
 *  2. 송신 buffer를 미리 초기화하고 전체 주소 공간을 잠금.
 *  3. CLOCK_MONOTONIC 절대 시각을 기준으로 송신 주기를 유지함.
 *  4. packet마다 flow ID, sequence와 CLOCK_REALTIME TX 시각을 기록함.
 *  5. sendto()로 고정 크기 UDP datagram을 전송함.
 *
 * CLOCK_REALTIME은 별도로 PTP 동기화를 마친 두 장비 사이의 OWD 계산에 사용하고,
	 * CLOCK_MONOTONIC은 달력 시각의 불연속 변경과 분리된 주기 제어에 사용함.
 * 측정 loop 안에서는 메모리 할당과 packet별 로그 출력을 수행하지 않음.
 */

#include <arpa/inet.h>
#include <endian.h>
#include <errno.h>
#include <getopt.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <netinet/ip.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "config.h"
#include "protocol.h"
#include "ace_run_control.h"

/** @brief 한 번의 sender run에 적용할 CLI 설정. */
struct sender_config {
	const char *destination;
	const char *ready_file;
	const char *stats_json;
	const char *start_file;
	uint16_t port;
	uint32_t flow_id;
	uint32_t payload_size;
	uint64_t period_ns;
	uint64_t packet_count;
	uint64_t start_at_ns;
	int completion_fd;
	bool defer_flush;
	bool flow_id_set;
};

/** @brief SIGINT/SIGTERM 수신 시 송신 loop 종료를 요청하는 플래그. */
static volatile sig_atomic_t stop;
static volatile sig_atomic_t flush_requested;
static void on_flush(int signo) { (void)signo; flush_requested = 1; }

/* =========================================================================
 * #### 종료 신호 처리
 *
 * signal handler에서는 비동기 신호 안전성을 위해 플래그만 변경함. socket
 * close와 메모리 잠금 해제는 main()의 정상 종료 경로에서 수행함.
 * ========================================================================= */

/**
 * @brief SIGINT 또는 SIGTERM 수신 사실을 송신 loop에 전달함.
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
	if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL))
		return -1;
	action.sa_handler = on_flush;
	if (sigaction(SIGUSR1, &action, NULL)) return -1;
	sigset_t handled;
	sigemptyset(&handled); sigaddset(&handled,SIGINT); sigaddset(&handled,SIGTERM); sigaddset(&handled,SIGUSR1);
	return sigprocmask(SIG_UNBLOCK,&handled,NULL);
}

/* =========================================================================
 * #### CLI 설정과 입력값 검증
 *
 * 실험마다 바뀌는 port, payload 크기, packet 수, 주기를 CLI로 받음. 잘못된
 * 값은 측정을 시작하기 전에 거부하여 run 설정이 모호해지지 않도록 함.
 * ========================================================================= */

/**
 * @brief sender 실행 옵션과 기본값을 표준 오류에 출력함.
 * @param program 실행 파일 이름.
 * @return 반환값 없음.
 */
static void usage(const char *program)
{
	fprintf(stderr,
		"Usage: %s [options] DESTINATION_IPV4\n"
		"  -p, --port PORT          UDP port (default: %d)\n"
		"  -f, --flow-id ID         packet flow identifier (required)\n"
		"  -s, --payload-size BYTES experiment payload bytes (default: %d)\n"
		"  -n, --count COUNT        packets to send (default: %d)\n"
		"  -i, --period-ns NS       send period (default: %d)\n"
		"      --ready-file PATH   atomically publish setup completion\n"
		"      --stats-json PATH   exclusive JSON summary after measurement\n"
		"      --start-at-ns NS    sender-host CLOCK_MONOTONIC start deadline\n"
		"      --start-file PATH  wait for an atomic file containing that deadline\n"
		"      --completion-fd N  inherited pipe: byte 0=sent, 1=error, before stats\n"
		"      --defer-flush       wait for SIGUSR1/INT/TERM before statistics I/O\n",
		program, ACE_DEFAULT_UDP_PORT, ACE_DEFAULT_PAYLOAD_SIZE,
		ACE_DEFAULT_SAMPLE_COUNT, ACE_DEFAULT_PERIOD_NS);
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
 * @brief sender CLI 인자를 해석하여 실행 설정을 완성함.
 * @param argc main()에서 전달받은 인자 개수.
 * @param argv main()에서 전달받은 인자 배열.
 * @param config 기본값이 채워진 설정 구조체. 지정된 옵션만 덮어씀.
 * @return 유효한 설정이면 0, 필수 목적지 누락 또는 잘못된 옵션이면 -1.
 */
static int parse_arguments(int argc, char **argv, struct sender_config *config)
{
	static const struct option options[] = {
		{"port", required_argument, NULL, 'p'},
		{"flow-id", required_argument, NULL, 'f'},
		{"payload-size", required_argument, NULL, 's'},
		{"count", required_argument, NULL, 'n'},
		{"period-ns", required_argument, NULL, 'i'},
		{"ready-file", required_argument, NULL, 1000},
		{"stats-json", required_argument, NULL, 1001},
		{"start-at-ns", required_argument, NULL, 1002},
		{"start-file", required_argument, NULL, 1003},
		{"completion-fd", required_argument, NULL, 1004},
		{"defer-flush", no_argument, NULL, 1005},
		{"help", no_argument, NULL, 'h'},
		{NULL, 0, NULL, 0},
	};
	int option;
	uint64_t value;

	while ((option = getopt_long(argc, argv, "p:f:s:n:i:h", options, NULL)) != -1) {
		switch (option) {
		case 1004:
			if (ace_parse_u64(optarg, 3, INT_MAX, &value)) return -1;
			config->completion_fd = (int)value;
			break;
		case 1005:
			config->defer_flush = true;
			break;
		case 1003:
			if (!*optarg) return -1;
			config->start_file = optarg;
			break;
		case 1000:
			if (!*optarg) return -1;
			config->ready_file = optarg;
			break;
		case 1001:
			if (!*optarg) return -1;
			config->stats_json = optarg;
			break;
		case 1002:
			if (ace_parse_u64(optarg, 1, INT64_MAX, &config->start_at_ns))
				return -1;
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
	if (!config->flow_id_set || optind + 1 != argc)
		return -1;
	if (config->start_file && config->start_at_ns)
		return -1;
	if (config->defer_flush && config->completion_fd < 0)
		return -1;
	config->destination = argv[optind];
	return 0;
}

/* =========================================================================
 * #### 시간 측정과 주기 제어
 *
 * packet에 기록할 송신 시각과 송신 주기 기준 clock을 분리함. 절대 시각
 * sleep을 사용하여 loop 실행 시간만큼 주기가 계속 밀리는 현상을 방지함.
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
 * @brief timespec 절대 시각에 ns 단위 주기를 더함.
 * @param time 갱신할 절대 시각.
 * @param nanoseconds 더할 시간(ns).
 * @return 반환값 없음.
 */
static void add_ns(struct timespec *time, uint64_t nanoseconds)
{
	time->tv_sec += (time_t)(nanoseconds / 1000000000ULL);
	time->tv_nsec += (long)(nanoseconds % 1000000000ULL);
	if (time->tv_nsec >= 1000000000L) {
		time->tv_sec++;
		time->tv_nsec -= 1000000000L;
	}
}

/**
 * @brief CLOCK_MONOTONIC 절대 시각까지 대기함.
 * @param deadline 다음 packet을 송신할 절대 시각.
 * @return 성공 시 0, 실패 시 clock_nanosleep()의 오류 번호.
 * @note 신호로 중단되면 종료 요청이 없는 동안 같은 deadline까지 다시 대기함.
 */
static int sleep_until(const struct timespec *deadline)
{
	int64_t target = (int64_t)deadline->tv_sec * 1000000000LL + deadline->tv_nsec;
	/* A long initial barrier must also stop promptly if a signal arrives just
	 * before entering nanosleep. Normal packet periods retain absolute pacing. */
	while (!stop) {
		int64_t now = ace_mono_ns();
		if (now < 0) return errno;
		if (now >= target) return 0;
		int64_t wake = target - now > 10000000LL ? now + 10000000LL : target;
		struct timespec chunk = {.tv_sec = (time_t)(wake / 1000000000LL),
			.tv_nsec = (long)(wake % 1000000000LL)};
		int result = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &chunk, NULL);
		if (result && result != EINTR) return result;
	}
	return EINTR;
}

/* Only used before measurement. The coordinator publishes a complete file by
 * rename after every sender's ready marker is visible. No polling in hot path. */
static int wait_start_file(const char *path, uint64_t *deadline)
{
	while (!stop) {
		int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
		if (fd >= 0) {
			char value[80];
			struct stat status;
			ssize_t count;
			if (fstat(fd, &status) || !S_ISREG(status.st_mode)) {
				close(fd); errno = EINVAL; return -1;
			}
			count = read(fd, value, sizeof(value) - 1);
			close(fd);
			if (count <= 0 || count == (ssize_t)sizeof(value) - 1 ||
			    memchr(value, '\0', (size_t)count)) {
				errno = EINVAL; return -1;
			}
			while (count && (value[count - 1] == '\n' || value[count - 1] == '\r')) count--;
			value[count] = '\0';
			if (ace_parse_u64(value, 1, INT64_MAX, deadline)) { errno = EINVAL; return -1; }
			int64_t now = ace_mono_ns();
			if (now < 0) return -1;
			if (*deadline <= (uint64_t)now) { errno = ETIME; return -1; }
			return 0;
		}
		if (errno != ENOENT && errno != EINTR) return -1;
		struct timespec pause = {.tv_nsec = 10000000L};
		nanosleep(&pause, NULL);
	}
	errno = EINTR;
	return -1;
}

/* =========================================================================
 * #### 송신 프로그램 실행 흐름
 *
 * setup 단계에서 socket, packet buffer, 메모리 잠금, signal handler를 모두
 * 준비한 뒤 측정 loop에 진입함. loop에서는 timestamp 기록과 sendto()만 수행함.
 * ========================================================================= */

/**
 * @brief UDP packet을 설정된 주기와 개수만큼 전송함.
 * @param argc CLI 인자 개수.
 * @param argv CLI 인자 배열. 마지막 위치 인자는 receiver IPv4 주소임.
 * @return 정상 완료 시 0, CLI 오류 시 2, setup 또는 송신 오류 시 1.
 */
int main(int argc, char **argv)
{
	struct sender_config config = {
		.port = ACE_DEFAULT_UDP_PORT,
		.payload_size = ACE_DEFAULT_PAYLOAD_SIZE,
		.period_ns = ACE_DEFAULT_PERIOD_NS,
		.packet_count = ACE_DEFAULT_SAMPLE_COUNT,
		.completion_fd = -1,
	};
	struct sockaddr_in destination = {
		.sin_family = AF_INET,
	};
	uint8_t packet[ACE_MAX_UDP_PAYLOAD];
	size_t packet_size;
	struct timespec next_send;
	uint64_t sent = 0, errors = 0, attempted = 0;
	uint64_t interval_count = 0, interval_min = UINT64_MAX, interval_max = 0;
	uint64_t late_count = 0, late_max = 0, late_periods = 0;
	long double interval_sum = 0, late_sum = 0;
	int64_t start_ns = 0, end_ns = 0, previous_tx_ns = 0;
	int64_t first_tx_ns = 0, last_tx_ns = 0;
	const char *reason = "count";
	FILE *stats = NULL;
	int last_send_errno = 0;
	int pmtudisc = IP_PMTUDISC_DO;
	int socket_fd;

	if (parse_arguments(argc, argv, &config)) {
		usage(argv[0]);
		return 2;
	}
	if (config.completion_fd >= 0) {
		struct stat status;
		int access_mode = fcntl(config.completion_fd, F_GETFL);
		if (access_mode < 0 || (access_mode & O_ACCMODE) == O_RDONLY ||
		    fstat(config.completion_fd, &status) || !S_ISFIFO(status.st_mode)) {
			fprintf(stderr, "completion-fd must be an inherited pipe\n");
			return 2;
		}
		signal(SIGPIPE, SIG_IGN);
	}
	destination.sin_port = htons(config.port);
	packet_size = sizeof(struct ace_experiment_header) + config.payload_size;
	if (inet_pton(AF_INET, config.destination, &destination.sin_addr) != 1) {
		fprintf(stderr, "invalid destination IPv4 address: %s\n",
			config.destination);
		return 2;
	}

	/* 일반 Linux IPv4 UDP socket을 한 번 생성하여 전체 run 동안 재사용함. */
	socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (socket_fd < 0) {
		perror("socket");
		return 1;
	}
	/*
	 * 실험 traffic은 MTU 1500의 단일 Ethernet frame으로 제한한다.
	 * 크기 제한과 함께 IP fragmentation을 금지하여 한 UDP datagram을 여러
	 * IP fragment로 나누지 않는다. 드라이버의 multi-descriptor DROP과는
	 * 다른 계층의 조건이다. descriptor 개수는 NIC의 RX buffer 구성에 달렸다.
	 */
	if (setsockopt(socket_fd, IPPROTO_IP, IP_MTU_DISCOVER,
		&pmtudisc, sizeof(pmtudisc))) {
		perror("setsockopt(IP_MTU_DISCOVER)");
		close(socket_fd);
		return 1;
	}
	/* 측정 전에 최대 packet buffer 전체를 접근하고 현재/미래 page를 잠금. */
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
	if (config.stats_json) {
		stats = ace_open_exclusive_stream(config.stats_json);
		if (!stats) {
			perror("create stats JSON");
			close(socket_fd);
			munlockall();
			return 1;
		}
	}

	printf("role: sender\n"
	       "destination: %s\n"
	       "UDP port: %u\n"
	       "flow ID: %u\n"
	       "period: %llu ns\n"
	       "payload size: %u bytes\n"
	       "UDP datagram payload: %zu bytes (header + payload)\n"
	       "packet count: %llu\n"
	       "clock: CLOCK_REALTIME (TX), CLOCK_MONOTONIC (pacing)\n",
		config.destination, config.port, config.flow_id,
		(unsigned long long)config.period_ns, config.payload_size, packet_size,
		(unsigned long long)config.packet_count);

	if (ace_publish_ready(config.ready_file, "sender")) {
		perror("publish ready file");
		reason = "setup_error";
		errors++;
		goto finished;
	}
	if (config.start_file && wait_start_file(config.start_file, &config.start_at_ns)) {
		if (stop) reason = "signal";
		else { perror("read start file"); reason = "start_file_error"; errors++; }
		goto finished;
	}
	start_ns = ace_mono_ns();
	if (start_ns < 0) {
		perror("clock_gettime(CLOCK_MONOTONIC)");
		reason = "clock_error";
		errors++;
		goto finished;
	}
	/* Validate the whole deadline sequence before sending the first packet. */
	uint64_t base_ns = config.start_at_ns ? config.start_at_ns : (uint64_t)start_ns;
	if (config.start_at_ns && base_ns <= (uint64_t)start_ns) {
		fprintf(stderr, "start deadline must be in the future\n");
		reason = "start_deadline_expired";
		errors++;
		goto finished;
	}
	if (config.packet_count - 1 > ((uint64_t)INT64_MAX - base_ns) / config.period_ns) {
		fprintf(stderr, "send deadline sequence exceeds int64 nanoseconds\n");
		reason = "configuration_error";
		errors++;
		goto finished;
	}
	next_send.tv_sec = (time_t)(base_ns / 1000000000ULL);
	next_send.tv_nsec = (long)(base_ns % 1000000000ULL);

	/* 측정 구간: 동적 할당과 packet별 출력 없이 timestamp와 송신만 수행함. */
	for (uint64_t seq = 0; seq < config.packet_count && !stop; seq++) {
		struct ace_experiment_header header;
		int64_t tx_ns;
		ssize_t result;

		if (seq != 0 || config.start_at_ns) {
			/*
			 * 직전 sendto() 완료 시각이 아니라 최초 기준 시각에 주기를 누적한다.
			 * 따라서 header 작성·syscall 시간이 매 주기에 추가되어 밀리지 않는다.
			 * 이미 deadline을 지난 경우 sleep은 즉시 끝난다. 이 코드는 주기
			 * 기준을 유지할 뿐, 실제 NIC 송신 시각을 정밀하게 예약하는 것은 아니다.
			 */
			if (seq != 0)
				add_ns(&next_send, config.period_ns);
			int sleep_result = sleep_until(&next_send);
			if (sleep_result && sleep_result != EINTR) {
				errno = sleep_result;
				perror("clock_nanosleep");
				errors++;
				reason = "sleep_error";
				break;
			}
			if (stop)
				break;
		}
		int64_t actual_ns = ace_mono_ns();
		if (actual_ns < 0) {
			errors++;
			reason = "clock_error";
			break;
		}
		int64_t deadline_ns = (int64_t)next_send.tv_sec * 1000000000LL + next_send.tv_nsec;
		uint64_t lateness = actual_ns > deadline_ns ? (uint64_t)(actual_ns - deadline_ns) : 0;
		if (lateness) late_count++;
		if (lateness >= config.period_ns) late_periods++;
		if (lateness > late_max) late_max = lateness;
		late_sum += lateness;
		if (previous_tx_ns) {
			uint64_t interval = (uint64_t)(actual_ns - previous_tx_ns);
			if (interval < interval_min) interval_min = interval;
			if (interval > interval_max) interval_max = interval;
			interval_sum += interval;
			interval_count++;
		}
		previous_tx_ns = actual_ns;

		/*
		 * TX 기준점은 user space에서 header를 만들기 직전이다. 이후 header
		 * 복사, sendto(), 송신 stack/queue 지연도 user-to-user OWD에 포함된다.
		 * NIC가 선로로 내보낸 HW TX timestamp와 혼동하지 않는다.
		 */
		tx_ns = realtime_ns();
		if (tx_ns < 0) {
			errors++;
			reason = "clock_error";
			break;
		}
		/*
		 * 모든 경로가 같은 32-byte wire ABI를 읽는다. flow_id는 실험 흐름을,
		 * sequence는 그 흐름의 packet을 구별하며 둘을 함께 offline join에 쓴다.
		 * sequence는 송신 시도마다 증가하므로 sendto 실패를 다음 packet ID로
		 * 재사용하지 않는다. network order는 송·수신 CPU의 endian 차이를 없앤다.
		 */
		header.magic = htobe32(ACE_PACKET_MAGIC);
		header.version = htobe16(ACE_PACKET_VERSION);
		header.size = htobe16(sizeof(header));
		header.flow_id = htobe32(config.flow_id);
		header.reserved = 0;
		header.sequence = htobe64(seq);
		header.tx_realtime_ns = htobe64((uint64_t)tx_ns);
		memcpy(packet, &header, sizeof(header));
		if (!attempted) first_tx_ns = actual_ns;
		last_tx_ns = actual_ns;
		attempted++;
		result = sendto(socket_fd, packet, packet_size, 0,
			(const struct sockaddr *)&destination, sizeof(destination));
		if (result == (ssize_t)packet_size)
			sent++;
		else {
			last_send_errno = result < 0 ? errno : EIO;
			errors++;
		}
	}
	if (stop)
		reason = "signal";
	else if (errors && !strcmp(reason, "count"))
		reason = "send_error";
finished:
	end_ns = ace_mono_ns();
	if (end_ns < 0) { end_ns = start_ns; errors++; }
	/* Offered-rate window excludes setup and the shared start barrier. */
	start_ns = first_tx_ns;
	if (!attempted) end_ns = 0;
	if (config.completion_fd >= 0) {
		unsigned char outcome = errors || stop || attempted != config.packet_count ? 1 : 0;
		ssize_t n;
		do { n = write(config.completion_fd, &outcome, 1); } while (n < 0 && errno == EINTR);
		if (n != 1) { errors++; reason = "completion_error"; }
		close(config.completion_fd);
	}
	if (config.defer_flush && !errors) {
		sigset_t blocked, previous;
		sigemptyset(&blocked); sigaddset(&blocked,SIGUSR1); sigaddset(&blocked,SIGINT); sigaddset(&blocked,SIGTERM);
		if (sigprocmask(SIG_BLOCK,&blocked,&previous)) { errors++; reason="signal_error"; }
		else {
			sigset_t wait_mask=previous; sigdelset(&wait_mask,SIGUSR1); sigdelset(&wait_mask,SIGINT); sigdelset(&wait_mask,SIGTERM);
			while (!flush_requested && !stop) sigsuspend(&wait_mask);
			sigprocmask(SIG_SETMASK,&previous,NULL);
		}
	}
	printf("completed: sent=%llu errors=%llu\n",
		(unsigned long long)sent, (unsigned long long)errors);
	if (stats) {
		fprintf(stats, "{\"schema_version\":1,\"role\":\"sender\",\"flow_id\":%u,"
			"\"expected_count\":%llu,\"attempted\":%llu,\"sent\":%llu,\"errors\":%llu,"
			"\"start_mono_ns\":%lld,\"end_mono_ns\":%lld,\"elapsed_ns\":%lld,"
			"\"scheduled_start_mono_ns\":%llu,\"first_send_mono_ns\":%lld,\"last_send_mono_ns\":%lld,"
			"\"period_ns\":%llu,\"interval_count\":%llu,\"interval_min_ns\":%llu,"
			"\"interval_max_ns\":%llu,\"interval_mean_ns\":%.3Lf,"
			"\"late_count\":%llu,\"late_period_count\":%llu,\"lateness_max_ns\":%llu,"
			"\"lateness_mean_ns\":%.3Lf,\"last_send_errno\":%d,\"termination_reason\":\"%s\"}\n",
			config.flow_id, (unsigned long long)config.packet_count,
			(unsigned long long)attempted, (unsigned long long)sent, (unsigned long long)errors,
			(long long)start_ns, (long long)end_ns, (long long)(start_ns ? end_ns - start_ns : 0),
			(unsigned long long)config.start_at_ns, (long long)first_tx_ns, (long long)last_tx_ns,
			(unsigned long long)config.period_ns, (unsigned long long)interval_count,
			(unsigned long long)(interval_count ? interval_min : 0),
			(unsigned long long)interval_max, interval_count ? interval_sum / interval_count : 0.0L,
			(unsigned long long)late_count, (unsigned long long)late_periods,
			(unsigned long long)late_max, attempted ? late_sum / attempted : 0.0L,
			last_send_errno, reason);
		if (ace_finish_stream(stats)) { perror("save stats JSON"); errors++; }
	}
	close(socket_fd);
	munlockall();
	return errors ? 1 : 0;
}
