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
 * UDP datagram 앞부분의 32-byte application header는 ace_packet_abi.h의
 * ace_experiment_header 정의를 공유한다. 모든 multi-byte field는 network byte
 * order이며 sender에서 encode하고 BPF/socket/AF_XDP 경로에서 decode한다. flow_id와
 * sequence의 조합은 한 run 안에서 packet을 고유하게 식별한다. run_id는 wire
 * packet에 넣지 않고 결과 디렉터리와 run manifest에서 관리한다.
 * 서로 다른 flow는 sequence가 같아도 구별되지만, 새 run의 결과와 이전 run의
 * record를 한 묶음으로 섞으면 같은 key가 재등장할 수 있으므로 run별로 저장한다.
 */
/**
 * @brief receiver가 samples.bin에 연속으로 저장하는 56-byte record.
 *
 * HW timestamp는 NIC PHC domain이고 user_rx_mono_ns는 CLOCK_MONOTONIC,
 * tx_ns/user_rx_real_ns는 CLOCK_REALTIME이다. 서로 다른 clock domain의 값을
 * 직접 빼지 않으며 PHC calibration 결과를 이용해 offline에서 변환한다.
 *
 * 이 record는 wire header가 아니라 수신기가 만드는 분석 입력이다. 실제 UDP
 * payload를 통째로 보관하지 않고 packet을 연결할 ID와 측정 지점별 시각을 남긴다.
 * len은 Ethernet/IP/UDP header를 제외한 UDP datagram 내용의 길이이며, 여기에는
 * 32-byte 실험 header가 포함된다. HW timestamp 존재 여부는 flags로 판단한다.
 * reserved field는 0으로 저장하며 native-endian 56-byte layout을 분석기도 공유한다.
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
