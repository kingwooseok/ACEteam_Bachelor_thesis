#ifndef ACE_UDP_PROTOCOL_H
#define ACE_UDP_PROTOCOL_H

/**
 * @file protocol.h
 * @brief UDP wire packet과 수신 결과 binary record의 공통 형식.
 *
 * sender와 receiver가 구조체를 중복 정의하지 않도록 공유함. wire packet은
 * network byte order를 사용하고, samples.bin은 receiver native byte order로
 * 저장함. Raspberry Pi 5 ARM64 환경에서는 일반적으로 little-endian임.
 */

#include <stdint.h>

/**
 * @brief UDP datagram 앞부분에 기록하는 16-byte application header.
 * @var packet_header::seq 0부터 증가하는 packet sequence number.
 * @var packet_header::tx_ns CLOCK_REALTIME 기준 송신 시각(ns).
 */
struct packet_header {
	uint64_t seq;
	int64_t tx_ns;
};

/**
 * @brief receiver가 samples.bin에 연속으로 저장하는 32-byte record.
 * @var sample_record::seq packet에서 복원한 sequence number.
 * @var sample_record::tx_ns packet에서 복원한 송신 시각(ns).
 * @var sample_record::rx_ns recv() 직후 측정한 수신 시각(ns).
 * @var sample_record::len 수신한 전체 UDP application payload 길이.
 * @var sample_record::flags 현재는 0으로 기록하며 형식 호환을 위해 유지함.
 * @var sample_record::reserved 정렬과 향후 형식 호환을 위한 예약 영역.
 */
struct sample_record {
	uint64_t seq;
	int64_t tx_ns;
	int64_t rx_ns;
	uint32_t len;
	uint16_t flags;
	uint16_t reserved;
};

/* binary layout이 의도치 않게 바뀌면 build 단계에서 즉시 실패시킴. */
_Static_assert(sizeof(struct packet_header) == 16,
	"packet_header layout changed");
_Static_assert(sizeof(struct sample_record) == 32,
	"sample_record layout changed");

#endif
