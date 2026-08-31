#define _GNU_SOURCE

/**
 * @file sender.c
 * @brief 일반 Linux UDP socket을 사용하는 주기 송신 프로그램.
 *
 * 송신 과정은 다음과 같음.
 *  1. CLI 인자를 검증하고 UDP 목적지를 준비함.
 *  2. 송신 buffer를 미리 초기화하고 전체 주소 공간을 잠금.
 *  3. CLOCK_MONOTONIC 절대 시각을 기준으로 송신 주기를 유지함.
 *  4. packet마다 sequence number와 CLOCK_REALTIME TX 시각을 기록함.
 *  5. sendto()로 고정 크기 UDP datagram을 전송함.
 *
 * CLOCK_REALTIME은 PTP로 동기화된 두 장비 사이의 OWD 계산에 사용하고,
 * CLOCK_MONOTONIC은 시스템 시각 보정에 영향을 받지 않는 주기 제어에 사용함.
 * 측정 loop 안에서는 메모리 할당과 packet별 로그 출력을 수행하지 않음.
 */

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

/** @brief 한 번의 sender run에 적용할 CLI 설정. */
struct sender_config {
	const char *destination;
	uint16_t port;
	uint32_t payload_size;
	uint64_t period_ns;
	uint64_t packet_count;
};

/** @brief SIGINT/SIGTERM 수신 시 송신 loop 종료를 요청하는 플래그. */
static volatile sig_atomic_t stop;

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
	return sigaction(SIGINT, &action, NULL) ||
		sigaction(SIGTERM, &action, NULL);
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
		"  -s, --payload-size BYTES experiment payload bytes (default: %d)\n"
		"  -n, --count COUNT        packets to send (default: %d)\n"
		"  -i, --period-ns NS       send period (default: %d)\n",
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
	int result;

	do {
		result = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, deadline, NULL);
	} while (result == EINTR && !stop);
	return result;
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

	/* 일반 Linux IPv4 UDP socket을 한 번 생성하여 전체 run 동안 재사용함. */
	socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (socket_fd < 0) {
		perror("socket");
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

	/* 측정 구간: 동적 할당과 packet별 출력 없이 timestamp와 송신만 수행함. */
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
		/* 서로 다른 byte order의 장비에서도 동일하게 해석하도록 network order 사용. */
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
