#ifndef ACE_UDP_CONFIG_H
#define ACE_UDP_CONFIG_H

/**
 * @file config.h
 * @brief UDP latency 실험의 공통 기본값.
 *
 * 여러 source에 흩어지면 안 되는 기본값만 이 파일에서 관리함. port, payload
 * 크기, 주기, sample 수, 경로는 실행 시 CLI 인자로 덮어쓸 수 있음.
 */

/* 숫자 기본값은 preprocessor macro 대신 compile-time 정수 상수로 정의함. */
enum {
	ACE_DEFAULT_UDP_PORT = 9000,
	ACE_DEFAULT_PAYLOAD_SIZE = 64,
	ACE_DEFAULT_PERIOD_NS = 1000000,
	ACE_DEFAULT_SAMPLE_COUNT = 200000,
	ACE_MAX_UDP_PAYLOAD = 65507,
};

/* 문자열 기본값은 sender/receiver가 직접 참조하는 단일 정의로 유지함. */
#define ACE_DEFAULT_BIND_ADDRESS "0.0.0.0"
#define ACE_DEFAULT_TMPFS_PATH   "/dev/shm/ace_udp_samples.tmp"
#define ACE_DEFAULT_RESULT_PATH  "./samples.bin"

#endif
