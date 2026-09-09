/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_PACKET_ABI_H
#define ACE_PACKET_ABI_H

/*
 * Wire ABI shared by the BPF parser and UDP sender/receivers.  vmlinux.h
 * already supplies these types to the BPF translation unit; ordinary
 * userspace gets the UAPI definitions from linux/types.h.
 */
#ifndef __VMLINUX_H__
#include <linux/types.h>
#endif

#define ACE_PACKET_MAGIC          0x41434531U /* "ACE1" after BE conversion */
#define ACE_PACKET_VERSION        1U
#define ACE_PACKET_HEADER_SIZE    32U

/* Every member is encoded in network (big-endian) byte order.
 *
 * UDP payload의 맨 앞에 붙는 공통 32바이트 header다. 송신기, BPF parser,
 * UDP/AF_XDP 수신기가 이 정의를 함께 써야 같은 패킷을 같은 ID로 해석한다.
 * flow_id는 사용자가 정한 흐름 번호이고 sequence는 흐름 안에서 증가한다.
 * 한 실행에서 흐름별 ID를 다르게 주면 각 sequence를 0부터 시작해도 된다.
 * 실행 간 구분은 결과 디렉터리에서 하므로 여기에는 run_id를 넣지 않는다.
 * tx_realtime_ns는 송신 장치의 realtime 기록이다. 수신 장치와 시계가 맞았다는
 * 뜻은 아니며, 수신측 monotonic 값과 그대로 빼서 편도 지연으로 쓰지 않는다.
 */
struct ace_experiment_header {
	__be32 magic;
	__be16 version;
	__be16 size;
	__be32 flow_id;
	__be32 reserved;
	__be64 sequence;
	__be64 tx_realtime_ns;
};

_Static_assert(sizeof(struct ace_experiment_header) == ACE_PACKET_HEADER_SIZE,
	"experiment packet header ABI changed");

#endif /* ACE_PACKET_ABI_H */
