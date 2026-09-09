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
#include "ace_packet_abi.h"

/**
 * @brief UDP datagram 앞부분에 기록하는 32-byte application header.
 * packet_header의 모든 multi-byte field는 network byte order다. flow_id와
 * sequence의 조합은 한 run 안에서 packet을 고유하게 식별한다. run_id는 wire
 * packet에 넣지 않고 결과 디렉터리와 run manifest에서 관리한다.
 */
/**
 * @brief receiver가 samples.bin에 연속으로 저장하는 56-byte record.
 *
 * HW timestamp는 NIC PHC domain이고 user_rx_mono_ns는 CLOCK_MONOTONIC,
 * tx_ns/user_rx_real_ns는 CLOCK_REALTIME이다. 서로 다른 clock domain의 값을
 * 직접 빼지 않으며 PHC calibration 결과를 이용해 offline에서 변환한다.
 */
struct sample_record {
	uint64_t seq;
	int64_t tx_ns;
	int64_t user_rx_mono_ns;
	int64_t user_rx_real_ns;
	int64_t hw_rx_ns;
	uint32_t flow_id;
	uint32_t len;
	uint16_t flags;
	uint16_t reserved16;
	uint32_t reserved32;
};

enum ace_sample_flags {
	ACE_SAMPLE_F_HWTS_VALID = 1U << 0,
	ACE_SAMPLE_F_CMSG_TRUNCATED = 1U << 1,
};

/* binary layout이 의도치 않게 바뀌면 build 단계에서 즉시 실패시킴. */
_Static_assert(sizeof(struct sample_record) == 56,
	"sample_record layout changed");

#endif
