// SPDX-License-Identifier: GPL-2.0
//
// afxdp_recv.c — UDP/9002 AF_XDP 수신 예제
//
// xdp_loader가 로드·attach하고 pin한 xsk_map을 열어, RX queue 하나에
// AF_XDP socket을 연결한다. socket FD를 xsk_map[queue_id]에 등록하면
// XDP의 UDP/9002 분류 결과가 UMEM의 RX ring으로 전달된다.
//
// 실행 흐름:
//   pin된 map 열기 → UMEM/FILL·RX ring 생성 → AF_XDP socket 생성
//   → XSKMAP[queue_id] 등록 → RX polling
//
// 이 경로는 zero-copy가 아니라 XDP_COPY다. macb의 page_pool RX 버퍼에서
// XDP를 실행한 뒤 커널이 metadata와 패킷을 사용자 UMEM으로 복사한다.
// 따라서 측정하는 지연에는 이 복사와 사용자 프로세스가 깨어나는 시간이
// 포함된다. 사용자 수신 기록은 메모리에 모았다가 종료 시 CSV로 저장한다.

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <linux/if_ether.h>
#include <linux/if_link.h>
#include <linux/if_xdp.h>
#include <linux/net_tstamp.h>
#include <xdp/xsk.h>            /* AF_XDP socket/ring API (libxdp) */

#include <arpa/inet.h>
#include <ctype.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <net/if.h>
#include <netinet/ip.h>
#include <netinet/udp.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../include/ace_process.h"
#include "config.h"
#include "metadata.h"

/* ===== 기본 설정 ===== */

enum {
	NUM_FRAMES = 4096,
	FRAME_SIZE = XSK_UMEM__DEFAULT_FRAME_SIZE,
	UMEM_SIZE = NUM_FRAMES * FRAME_SIZE,
	RING_SIZE = 2048,
	RX_BATCH_SIZE = 64,
};

static volatile sig_atomic_t stop;
static unsigned long long received_packets;
static unsigned long long received_udp_packets;
static unsigned long long received_experiment_packets;
static unsigned long long malformed_packets;
static unsigned long long unexpected_port_packets;
static unsigned long long metadata_invalid_packets;
static unsigned long long record_overflow_packets;
static unsigned long long recycled_frames;
static unsigned long long umem_invariant_errors;

/* ===== 종료 처리 ===== */

static void on_signal(int signo)
{
	(void)signo;
	stop = 1;
}

/* ===== AF_XDP 자료구조 ===== */

/* UMEM과 그에 연결된 FILL/COMPLETION ring. 수신만 하므로 completion은
 * 생성만 하고 사용하지 않는다(TX packet을 보낼 때 필요). */
/* 수신용 UMEM frame의 순환:
 *   사용자: 빈 frame 주소를 FILL에 제출
 *   커널: 그 frame에 패킷을 복사하고 RX에 descriptor 게시
 *   사용자: RX에서 주소를 받아 읽고 같은 frame 시작 주소를 FILL에 반환
 * ring에는 패킷 본문이 아니라 UMEM 안의 위치가 들어 있다. RX slot을
 * release하는 것과 frame을 FILL로 돌려주는 것은 서로 별개의 작업이다. */
struct umem_info {
	void *buffer;
	struct xsk_umem *umem;
	struct xsk_ring_prod fill;
	struct xsk_ring_cons completion;
	unsigned char frame_in_kernel[NUM_FRAMES];
	unsigned int userspace_owned_frames;
};

/* AF_XDP socket과 RX/TX ring. 이 예제에서는 RX만 사용한다. */
struct xsk_info {
	struct xsk_socket *socket;
	struct xsk_ring_cons rx;
	struct xsk_ring_prod tx;
};

struct receiver_options {
	const char *ifname;
	const char *output_path;
	__u32 queue_id;
	__u32 record_capacity;
	bool records_explicit;
};

/* ===== UMEM 및 socket 초기화 ===== */

static int create_umem(struct umem_info *umem)
{
	struct xsk_umem_config config = {
		.fill_size = NUM_FRAMES,
		.comp_size = NUM_FRAMES,
		.frame_size = FRAME_SIZE,
		.frame_headroom = 0,
		.flags = 0,
	};
	int err;

	/* AF_XDP UMEM은 page-aligned buffer를 요구한다. */
	err = posix_memalign(&umem->buffer, (size_t)getpagesize(), UMEM_SIZE);
	if (err)
		return -err;
	memset(umem->buffer, 0, UMEM_SIZE);

	err = xsk_umem__create(&umem->umem, umem->buffer, UMEM_SIZE,
		&umem->fill, &umem->completion, &config);
	if (err) {
		free(umem->buffer);
		umem->buffer = NULL;
		return err;
	}

	return 0;
}

static int fill_initial_frames(struct umem_info *umem)
{
	__u32 idx;
	__u32 i;

	if (xsk_ring_prod__reserve(&umem->fill, NUM_FRAMES, &idx) != NUM_FRAMES)
		return -ENOSPC;
	for (i = 0; i < NUM_FRAMES; i++)
		*xsk_ring_prod__fill_addr(&umem->fill, idx + i) =
			(__u64)i * FRAME_SIZE;
	xsk_ring_prod__submit(&umem->fill, NUM_FRAMES);
	memset(umem->frame_in_kernel, 1, sizeof(umem->frame_in_kernel));
	return 0;
}

static int create_socket(struct xsk_info *xsk, struct umem_info *umem,
		const char *ifname, __u32 queue_id)
{
	struct xsk_socket_config config = {
		.rx_size = RING_SIZE,
		.tx_size = RING_SIZE,
		.libbpf_flags = XSK_LIBBPF_FLAGS__INHIBIT_PROG_LOAD,
		.xdp_flags = XDP_FLAGS_DRV_MODE,
		.bind_flags = XDP_COPY,
	};

	/* 기존 native XDP program은 loader가 소유한다. 여기서는 새 program을
	 * attach하지 않고 copy-mode AF_XDP socket만 해당 queue에 bind한다. */
	return xsk_socket__create(&xsk->socket, ifname, queue_id, umem->umem,
		&xsk->rx, &xsk->tx, &config);
}

static int register_socket(struct xsk_info *xsk, int xsk_map_fd,
		__u32 queue_id)
{
	int socket_fd = xsk_socket__fd(xsk->socket);

	/* XSKMAP의 key는 UDP 포트/flow_id가 아니라 실제 NIC RX queue 번호다.
	 * xdp_dispatch의 ctx->rx_queue_index와 이 queue_id가 같아야 이 socket으로
	 * 전달된다. UDP/9002 분류는 그보다 앞선 BPF 단계에서 이미 끝난다. */
	/* BPF_NOEXIST prevents a second receiver from silently replacing the
	 * live owner of this queue. Closing the socket later removes only entries
	 * that still point at this socket, so cleanup cannot delete another
	 * receiver's entry after an external replacement. */
	if (bpf_map_update_elem(xsk_map_fd, &queue_id, &socket_fd,
			BPF_NOEXIST) < 0)
		return -errno;

	return 0;
}

static bool xsk_owner_equal(const struct ace_xsk_owner *left,
		const struct ace_xsk_owner *right)
{
	return left->process_start_ticks == right->process_start_ticks &&
		left->pid == right->pid && left->reserved == right->reserved;
}

static int claim_xsk_queue(int owner_map_fd, __u32 queue_id,
		const struct ace_xsk_owner *owner)
{
	if (bpf_map_update_elem(owner_map_fd, &queue_id, owner,
			BPF_NOEXIST))
		return -errno;
	return 0;
}

static int release_xsk_queue(int owner_map_fd, __u32 queue_id,
		const struct ace_xsk_owner *owner)
{
	struct ace_xsk_owner current;

	if (bpf_map_lookup_elem(owner_map_fd, &queue_id, &current))
		return errno == ENOENT ? -ESTALE : -errno;
	if (!xsk_owner_equal(&current, owner))
		return -ESTALE;
	if (bpf_map_delete_elem(owner_map_fd, &queue_id))
		return -errno;
	return 0;
}

/* ===== 수신 packet 처리 ===== */

struct udp_packet_info {
	const unsigned char *payload;
	__u32 payload_length;
	__u16 destination_port;
};

static bool packet_udp_info(const void *packet, __u32 length,
		struct udp_packet_info *info)
{
	const unsigned char *bytes = packet;
	struct ethhdr eth;
	struct iphdr ip;
	struct udphdr udp;
	__u32 ip_header_len, ip_total_len, udp_len;

	if (length < sizeof(eth))
		return false;
	memcpy(&eth, bytes, sizeof(eth));
	if (ntohs(eth.h_proto) != ETH_P_IP)
		return false;
	if (length < sizeof(eth) + sizeof(ip))
		return false;

	memcpy(&ip, bytes + sizeof(eth), sizeof(ip));
	if (ip.version != 4 || ip.ihl < 5 || ip.protocol != IPPROTO_UDP)
		return false;
	if (ntohs(ip.frag_off) & (IP_MF | IP_OFFMASK))
		return false;
	ip_header_len = (__u32)ip.ihl * 4;
	if (length < sizeof(eth) + ip_header_len + sizeof(udp))
		return false;
	ip_total_len = ntohs(ip.tot_len);
	if (ip_total_len < ip_header_len + sizeof(udp) ||
		ip_total_len > length - sizeof(eth))
		return false;

	memcpy(&udp, bytes + sizeof(eth) + ip_header_len, sizeof(udp));
	udp_len = ntohs(udp.len);
	if (udp_len < sizeof(udp) || udp_len > ip_total_len - ip_header_len)
		return false;

	info->destination_port = ntohs(udp.dest);
	info->payload = bytes + sizeof(eth) + ip_header_len + sizeof(udp);
	info->payload_length = udp_len - (__u32)sizeof(udp);
	return true;
}

static bool experiment_packet_id(const struct udp_packet_info *packet,
		__u32 *flow_id, __u64 *sequence, __s64 *tx_realtime_ns)
{
	struct ace_experiment_header header;

	/* The Ethernet/IP/UDP offsets do not guarantee 64-bit alignment. */
	if (packet->payload_length < sizeof(header))
		return false;
	memcpy(&header, packet->payload, sizeof(header));
	if (ntohl(header.magic) != ACE_PACKET_MAGIC ||
		ntohs(header.version) != ACE_PACKET_VERSION ||
		ntohs(header.size) != sizeof(header) || ntohl(header.reserved) != 0)
		return false;

	*flow_id = ntohl(header.flow_id);
	*sequence = be64toh(header.sequence);
	*tx_realtime_ns = (__s64)be64toh(header.tx_realtime_ns);
	return true;
}

/* MONOTONIC은 같은 Pi의 bpf_ktime_get_ns()와 비교하는 사용자 도착 시각,
 * REALTIME은 송신 기록 및 외부 clock 보정과 연결할 때 쓰는 시각이다.
 * NIC의 PHC timestamp와 MONOTONIC은 기준 시계가 다르므로 단순히 빼지
 * 않는다. PHC 보정 결과를 이용한 변환은 오프라인 분석기가 담당한다. */
static int capture_user_timestamps(__u64 *monotonic, __s64 *realtime)
{
	struct timespec mono;
	struct timespec real;

	if (clock_gettime(CLOCK_MONOTONIC, &mono) ||
	    clock_gettime(CLOCK_REALTIME, &real))
		return -errno;
	*monotonic = (__u64)mono.tv_sec * 1000000000ULL + (__u64)mono.tv_nsec;
	*realtime = (__s64)real.tv_sec * 1000000000LL + real.tv_nsec;
	return 0;
}

/* xdp_dispatch가 data_meta에 쓴 ace_rx_meta는 Ethernet header 바로 앞에
 * 붙어 온다. UDP payload의 실험 header와 달리 네트워크로 전송된 내용이
 * 아니라 이 Pi의 XDP가 추가한 로컬 측정값이다. 따라서 packet_addr에서
 * metadata 크기만큼 뒤로 가서 읽고 payload의 flow_id + sequence와 맞춘다.
 * timestamp 유효성은 값이 0인지가 아니라 meta_flags로 판단한다. */
static void read_xdp_metadata(const struct umem_info *umem, __u64 packet_addr,
		__u32 flow_id, __u64 sequence,
		struct ace_xsk_user_record *record)
{
	const __u64 chunk_start = packet_addr & ~((__u64)FRAME_SIZE - 1);
	struct ace_rx_meta metadata;
	__u64 metadata_addr;

	if (packet_addr < sizeof(metadata)) {
		metadata_invalid_packets++;
		return;
	}
	metadata_addr = packet_addr - sizeof(metadata);
	if (metadata_addr < chunk_start ||
	    metadata_addr > UMEM_SIZE - sizeof(metadata)) {
		metadata_invalid_packets++;
		return;
	}

	memcpy(&metadata, (const unsigned char *)umem->buffer + metadata_addr,
		sizeof(metadata));
	if (metadata.magic != ACE_RX_META_MAGIC ||
	    metadata.version != ACE_RX_META_VERSION ||
	    metadata.size != sizeof(metadata) ||
	    !(metadata.flags & ACE_META_F_PACKET_ID_VALID)) {
		metadata_invalid_packets++;
		return;
	}

	record->validation_flags |= ACE_XSK_RECORD_F_META_VALID;
	if (metadata.flow_id == flow_id && metadata.sequence == sequence)
		record->validation_flags |= ACE_XSK_RECORD_F_KEY_MATCH;
	if (metadata.requested_path == ACE_PATH_XSK)
		record->validation_flags |= ACE_XSK_RECORD_F_PATH_MATCH;
	if ((record->validation_flags &
	     (ACE_XSK_RECORD_F_KEY_MATCH | ACE_XSK_RECORD_F_PATH_MATCH)) !=
	    (ACE_XSK_RECORD_F_KEY_MATCH | ACE_XSK_RECORD_F_PATH_MATCH)) {
		/* A valid-looking value from a previous use of this UMEM frame must
		 * never be reported as metadata for the current packet. */
		metadata_invalid_packets++;
		return;
	}

	record->hw_rx_ns = metadata.hw_rx_ns;
	record->initial_xdp_ns = metadata.initial_xdp_ns;
	record->meta_flags = metadata.flags;
	record->requested_path = metadata.requested_path;
	record->timestamp_error = metadata.timestamp_error;
}

static void clear_xdp_metadata(struct umem_info *umem, __u64 packet_addr)
{
	const __u64 chunk_start = packet_addr & ~((__u64)FRAME_SIZE - 1);
	__u64 metadata_addr;

	if (packet_addr < sizeof(struct ace_rx_meta))
		return;
	metadata_addr = packet_addr - sizeof(struct ace_rx_meta);
	if (metadata_addr < chunk_start ||
	    metadata_addr > UMEM_SIZE - sizeof(struct ace_rx_meta))
		return;
	memset((unsigned char *)umem->buffer + metadata_addr, 0,
	       sizeof(struct ace_rx_meta));
}

static int validate_rx_descriptor(const struct xdp_desc *desc,
		unsigned int batch_index, __u64 *fill_addr, __u64 *data_addr)
{
	__u64 chunk_start, chunk_end;
	__u64 base_addr = xsk_umem__extract_addr(desc->addr);
	__u64 packet_addr = xsk_umem__add_offset_to_addr(desc->addr);

	/* This receiver deliberately does not request XDP_USE_SG. If a continued
	 * descriptor is nevertheless observed, retaining the RX entries and
	 * terminating is safer than parsing part of a packet as a complete one. */
	if (desc->options & XDP_PKT_CONTD) {
		fprintf(stderr,
			"RX descriptor %u is a multi-buffer continuation; XDP_USE_SG is disabled\n",
			batch_index);
		return -EOPNOTSUPP;
	}
	if (desc->options != 0) {
		fprintf(stderr, "RX descriptor %u has unsupported options %#x\n",
			batch_index, desc->options);
		return -EOPNOTSUPP;
	}

	if (base_addr >= UMEM_SIZE || packet_addr >= UMEM_SIZE || !desc->len ||
		desc->len > UMEM_SIZE - packet_addr) {
		fprintf(stderr,
			"RX descriptor %u is outside UMEM: raw=%#llx base=%#llx data=%#llx len=%u\n",
			batch_index, (unsigned long long)desc->addr,
			(unsigned long long)base_addr,
			(unsigned long long)packet_addr, desc->len);
		return -ERANGE;
	}

	/* This UMEM uses aligned FRAME_SIZE chunks. Check that the packet does not
	 * cross its owning chunk even when desc->addr carries an encoded offset. */
	chunk_start = base_addr & ~((__u64)FRAME_SIZE - 1);
	chunk_end = chunk_start + FRAME_SIZE;
	if (packet_addr < chunk_start || packet_addr >= chunk_end ||
		desc->len > chunk_end - packet_addr) {
		fprintf(stderr,
			"RX descriptor %u crosses a UMEM frame: base=%#llx data=%#llx len=%u\n",
			batch_index, (unsigned long long)base_addr,
			(unsigned long long)packet_addr, desc->len);
		return -ERANGE;
	}

	/* This UMEM is in aligned-chunk mode. Refill with the actual chunk base;
	 * an RX descriptor address itself points at packet data (normally after
	 * XDP_PACKET_HEADROOM), not at the beginning of the UMEM frame. */
	*fill_addr = chunk_start;
	*data_addr = packet_addr;
	return 0;
}

/* 한 batch에서 주소 확인 → FILL 반환 자리 확보 → 패킷별 시각/기록 복사
 * → RX slot 해제·FILL 제출까지 끝낸다. 기록은 별도 배열로 복사하므로
 * frame을 커널에 돌려준 뒤에도 저장할 측정값은 계속 보존된다. */
static int receive_batch(struct xsk_info *xsk, struct umem_info *umem,
		__u32 queue_id, struct ace_xsk_user_record *records,
		__u32 record_capacity, __u32 *record_count)
{
	__u32 rx_index, fill_index;
	__u64 addresses[RX_BATCH_SIZE];
	__u64 data_addresses[RX_BATCH_SIZE];
	unsigned int received, i;
	int batch_error = 0;
	int err;

	received = xsk_ring_cons__peek(&xsk->rx, RX_BATCH_SIZE, &rx_index);
	if (!received)
		return 0;

	/* Validate the entire batch before consuming any RX descriptor. */
	for (i = 0; i < received; i++) {
		const struct xdp_desc *desc =
			xsk_ring_cons__rx_desc(&xsk->rx, rx_index + i);

		err = validate_rx_descriptor(desc, i, &addresses[i],
			&data_addresses[i]);
		if (err) {
			for (unsigned int j = 0; j < i; j++) {
				__u32 frame = (__u32)(addresses[j] / FRAME_SIZE);

				umem->frame_in_kernel[frame] = 1;
				umem->userspace_owned_frames--;
			}
			xsk_ring_cons__cancel(&xsk->rx, received);
			return err;
		}
		{
			__u32 frame = (__u32)(addresses[i] / FRAME_SIZE);

			if (frame >= NUM_FRAMES || !umem->frame_in_kernel[frame]) {
				for (unsigned int j = 0; j < i; j++) {
					__u32 previous =
						(__u32)(addresses[j] / FRAME_SIZE);

					umem->frame_in_kernel[previous] = 1;
					umem->userspace_owned_frames--;
				}
				umem_invariant_errors++;
				xsk_ring_cons__cancel(&xsk->rx, received);
				return -EUCLEAN;
			}
			umem->frame_in_kernel[frame] = 0;
			umem->userspace_owned_frames++;
		}
	}

	/* Reserve all replacement slots before releasing RX ownership. On failure,
	 * cancel the peek so no UMEM addresses are lost. */
	if (xsk_ring_prod__reserve(&umem->fill, received, &fill_index) != received) {
		for (i = 0; i < received; i++) {
			__u32 frame = (__u32)(addresses[i] / FRAME_SIZE);

			umem->frame_in_kernel[frame] = 1;
			umem->userspace_owned_frames--;
		}
		xsk_ring_cons__cancel(&xsk->rx, received);
		return -ENOSPC;
	}

	for (i = 0; i < received; i++) {
		const struct xdp_desc *desc =
			xsk_ring_cons__rx_desc(&xsk->rx, rx_index + i);
		const void *packet = (const unsigned char *)umem->buffer +
			data_addresses[i];
		struct udp_packet_info udp_packet;
		__u32 flow_id;
		__u64 sequence;
		__s64 tx_realtime_ns;
		__u64 user_rx_mono_ns = 0;
		__s64 user_rx_real_ns = 0;

		received_packets++;
		/* 논문에서의 사용자 수신 경계는 바로 여기다: batch 주소 확인과
		 * FILL 예약 뒤, 개별 패킷의 header parsing/기록 복사 직전이다.
		 * 배치의 뒤쪽 패킷에는 앞선 패킷 처리 시간도 포함되며, poll 반환
		 * 시각이나 NIC 도착 시각 자체를 측정하는 것은 아니다. */
		err = capture_user_timestamps(&user_rx_mono_ns,
			&user_rx_real_ns);
		if (err) {
			if (!batch_error)
				batch_error = err;
			goto recycle;
		}
		if (!packet_udp_info(packet, desc->len, &udp_packet)) {
			malformed_packets++;
		} else {
			received_udp_packets++;
			if (udp_packet.destination_port != ACE_XDP_XSK_PORT) {
				unexpected_port_packets++;
			} else if (experiment_packet_id(&udp_packet, &flow_id, &sequence,
						 &tx_realtime_ns)) {
				received_experiment_packets++;
				if (*record_count < record_capacity) {
					struct ace_xsk_user_record *record =
						&records[(*record_count)++];

					memset(record, 0, sizeof(*record));
					record->flow_id = flow_id;
					record->rx_queue = queue_id;
					record->sequence = sequence;
					record->tx_realtime_ns = tx_realtime_ns;
					record->user_rx_mono_ns = user_rx_mono_ns;
					record->user_rx_real_ns = user_rx_real_ns;
					record->packet_length = desc->len;
					read_xdp_metadata(umem, data_addresses[i],
						flow_id, sequence, record);
				} else {
					record_overflow_packets++;
					if (!batch_error)
						batch_error = -ENOSPC;
				}
			}
		}

recycle:
		/* Do not let a metadata-free packet inherit bytes left by the
		 * previous use of this UMEM frame. */
		clear_xdp_metadata(umem, data_addresses[i]);
		*xsk_ring_prod__fill_addr(&umem->fill, fill_index + i) =
			addresses[i];
		{
			__u32 frame = (__u32)(addresses[i] / FRAME_SIZE);

			if (umem->frame_in_kernel[frame] ||
			    !umem->userspace_owned_frames) {
				umem_invariant_errors++;
				if (!batch_error)
					batch_error = -EUCLEAN;
			} else {
				umem->frame_in_kernel[frame] = 1;
				umem->userspace_owned_frames--;
				recycled_frames++;
			}
		}
	}

	/* Packet access is complete and every frame already has a reserved refill
	 * entry, so publishing both rings cannot drain the UMEM address pool. */
	xsk_ring_cons__release(&xsk->rx, received);
	xsk_ring_prod__submit(&umem->fill, received);
	if (umem->userspace_owned_frames) {
		umem_invariant_errors++;
		if (!batch_error)
			batch_error = -EUCLEAN;
	}

	if (batch_error)
		return batch_error;
	return (int)received;
}

/* ===== 실행 대상 및 pinned map 검증 ===== */

static int parse_queue_id(const char *text, __u32 *queue_id)
{
	unsigned long long parsed;
	char *end;

	if (!text || !text[0])
		return -EINVAL;
	for (const unsigned char *cursor = (const unsigned char *)text;
		*cursor; cursor++)
		if (!isdigit(*cursor))
			return -EINVAL;

	errno = 0;
	parsed = strtoull(text, &end, 10);
	if (errno == ERANGE || *end || parsed > UINT32_MAX)
		return -ERANGE;

	*queue_id = (__u32)parsed;
	return 0;
}

static int parse_record_capacity(const char *text, __u32 *capacity)
{
	unsigned long long parsed;
	char *end;

	if (!text || !text[0])
		return -EINVAL;
	for (const unsigned char *cursor = (const unsigned char *)text;
	     *cursor; cursor++)
		if (!isdigit(*cursor))
			return -EINVAL;
	errno = 0;
	parsed = strtoull(text, &end, 10);
	if (errno || text == end || *end || parsed < 1 ||
	    parsed > ACE_XDP_RECORD_MAX_ENTRIES)
		return -EINVAL;
	*capacity = (__u32)parsed;
	return 0;
}

static void usage(const char *program)
{
	fprintf(stderr,
		"usage: %s --output FILE [--records COUNT] [interface [rx_queue]]\n"
		"  --output, -o FILE   create end-of-run AF_XDP record CSV\n"
		"  --records, -n COUNT max records (default %u, max %u)\n",
		program, ACE_XDP_RECORD_DEFAULT_ENTRIES,
		ACE_XDP_RECORD_MAX_ENTRIES);
}

static int parse_options(int argc, char **argv, struct receiver_options *options)
{
	static const struct option long_options[] = {
		{ "output", required_argument, NULL, 'o' },
		{ "records", required_argument, NULL, 'n' },
		{ "help", no_argument, NULL, 'h' },
		{},
	};
	int option;

	options->ifname = ACE_XDP_DEFAULT_IFNAME;
	options->queue_id = ACE_XSK_DEFAULT_QUEUE;
	options->record_capacity = ACE_XDP_RECORD_DEFAULT_ENTRIES;

	while ((option = getopt_long(argc, argv, "o:n:h",
				 long_options, NULL)) != -1) {
		switch (option) {
		case 'o':
			options->output_path = optarg;
			break;
		case 'n':
			if (parse_record_capacity(optarg, &options->record_capacity))
				return -EINVAL;
			options->records_explicit = true;
			break;
		case 'h':
			usage(argv[0]);
			return 1;
		default:
			return -EINVAL;
		}
	}

	if (optind < argc)
		options->ifname = argv[optind++];
	if (optind < argc) {
		if (parse_queue_id(argv[optind++], &options->queue_id))
			return -EINVAL;
	}
	if (optind != argc || !options->output_path || !*options->output_path)
		return -EINVAL;
	return 0;
}

static int save_user_records(int output_fd,
		const struct ace_xsk_user_record *records, __u32 count)
{
	FILE *file = fdopen(output_fd, "w");
	int saved_errno;

	if (!file) {
		int open_errno = errno;

		close(output_fd);
		errno = open_errno;
		return -open_errno;
	}
	fprintf(file,
		"flow_id,sequence,rx_queue,tx_realtime_ns,user_rx_mono_ns,"
		"user_rx_real_ns,hw_rx_ns,"
		"initial_xdp_ns,meta_flags,requested_path,timestamp_error,"
		"packet_length,validation_flags\n");
	for (__u32 i = 0; i < count; i++) {
		const struct ace_xsk_user_record *record = &records[i];

		fprintf(file, "%u,%" PRIu64 ",%u,%" PRId64 ",%" PRIu64
			",%" PRId64 ",%" PRIu64 ",%" PRIu64
			",%u,%u,%d,%u,%u\n",
			record->flow_id, (uint64_t)record->sequence,
			record->rx_queue, (int64_t)record->tx_realtime_ns,
			(uint64_t)record->user_rx_mono_ns,
			(int64_t)record->user_rx_real_ns,
			(uint64_t)record->hw_rx_ns,
			(uint64_t)record->initial_xdp_ns, record->meta_flags,
			record->requested_path, record->timestamp_error,
			record->packet_length, record->validation_flags);
	}
	if (ferror(file) || fflush(file)) {
		saved_errno = errno ? errno : EIO;
		fclose(file);
		errno = saved_errno;
		return -1;
	}
	if (fsync(fileno(file))) {
		saved_errno = errno;
		fclose(file);
		errno = saved_errno;
		return -1;
	}
	return fclose(file);
}

static int validate_rx_queue(const char *ifname, __u32 queue_id)
{
	char path[PATH_MAX];
	int length;

	length = snprintf(path, sizeof(path), "/sys/class/net/%s/queues/rx-%u",
		ifname, queue_id);
	if (length < 0 || (size_t)length >= sizeof(path))
		return -ENAMETOOLONG;
	if (access(path, F_OK) < 0)
		return -errno;

	return 0;
}

static int validate_pinned_map(int fd, const char *pin_path,
		enum bpf_map_type expected_type, __u32 expected_key_size,
		__u32 expected_value_size, __u32 expected_max_entries,
		const char *expected_name, __u32 *map_id)
{
	struct bpf_map_info info = {};
	__u32 info_length = sizeof(info);

	if (bpf_obj_get_info_by_fd(fd, &info, &info_length) < 0) {
		int err = errno;

		fprintf(stderr, "cannot inspect pinned map %s: %s\n", pin_path,
			strerror(err));
		return -err;
	}

	if (info.type != (__u32)expected_type ||
		info.key_size != expected_key_size ||
		info.value_size != expected_value_size ||
		info.max_entries != expected_max_entries ||
		strcmp(info.name, expected_name) != 0) {
		fprintf(stderr,
			"pinned map %s has unexpected schema: name=%s type=%u key=%u value=%u max=%u\n",
			pin_path, info.name, info.type, info.key_size, info.value_size,
			info.max_entries);
		return -EINVAL;
	}

	if (map_id)
		*map_id = info.id;
	return 0;
}

static bool program_uses_map(const __u32 *map_ids, __u32 count, __u32 map_id)
{
	for (__u32 i = 0; i < count; i++)
		if (map_ids[i] == map_id)
			return true;
	return false;
}

static int require_live_loader_owner(
		const struct ace_runtime_config *runtime)
{
	bool alive;
	int err;

	if (runtime->owner_pid > (unsigned int)INT_MAX)
		return -EINVAL;
	err = ace_process_identity_is_alive((pid_t)runtime->owner_pid,
		runtime->owner_start_ticks, &alive);
	if (err)
		return err;
	return alive ? 0 : -ESRCH;
}

static int validate_attached_program(int ifindex, __u32 xsk_map_id,
		__u32 stats_map_id, __u32 expected_program_id)
{
	struct bpf_xdp_query_opts query = {
		.sz = sizeof(query),
	};
	struct bpf_prog_info info = {};
	__u32 *map_ids = NULL;
	__u32 info_length = sizeof(info);
	__u32 program_id;
	int program_fd = -1;
	int err;

	err = bpf_xdp_query(ifindex, XDP_FLAGS_DRV_MODE, &query);
	if (err) {
		fprintf(stderr, "cannot query native XDP program: %s\n",
			strerror(-err));
		return err;
	}
	program_id = query.prog_id ? query.prog_id : query.drv_prog_id;
	if (!program_id) {
		fprintf(stderr, "no native XDP program is attached to interface index %d\n",
			ifindex);
		return -ENOENT;
	}
	if (program_id != expected_program_id) {
		fprintf(stderr,
			"attached native XDP program id %u differs from runtime owner id %u\n",
			program_id, expected_program_id);
		return -EXDEV;
	}

	program_fd = bpf_prog_get_fd_by_id(program_id);
	if (program_fd < 0) {
		err = -errno;
		fprintf(stderr, "cannot open attached XDP program %u: %s\n",
			program_id, strerror(-err));
		return err;
	}
	if (bpf_obj_get_info_by_fd(program_fd, &info, &info_length) < 0) {
		err = -errno;
		fprintf(stderr, "cannot inspect attached XDP program %u: %s\n",
			program_id, strerror(-err));
		goto out;
	}
	if (info.type != BPF_PROG_TYPE_XDP || !info.nr_map_ids) {
		fprintf(stderr, "attached program %u is not the expected mapped XDP program\n",
			program_id);
		err = -EINVAL;
		goto out;
	}

	map_ids = calloc(info.nr_map_ids, sizeof(*map_ids));
	if (!map_ids) {
		err = -ENOMEM;
		goto out;
	}
	{
		__u32 map_count = info.nr_map_ids;

		memset(&info, 0, sizeof(info));
		info.nr_map_ids = map_count;
		info.map_ids = (__u64)(uintptr_t)map_ids;
		info_length = sizeof(info);
		if (bpf_obj_get_info_by_fd(program_fd, &info, &info_length) < 0) {
			err = -errno;
			fprintf(stderr, "cannot read map IDs from XDP program %u: %s\n",
				program_id, strerror(-err));
			goto out;
		}
	}

	if (!program_uses_map(map_ids, info.nr_map_ids, xsk_map_id) ||
	    !program_uses_map(map_ids, info.nr_map_ids, stats_map_id)) {
		fprintf(stderr,
			"pinned maps do not belong to native XDP program %u on this interface\n",
			program_id);
		err = -EXDEV;
		goto out;
	}
	err = 0;

out:
	free(map_ids);
	close(program_fd);
	return err;
}

/* ===== BPF 통계 출력 ===== */

static void print_stats(int stats_fd)
{
	int ncpu = libbpf_num_possible_cpus();
	__u64 *values;

	if (ncpu < 1)
		return;
	values = calloc((size_t)ncpu, sizeof(*values));
	if (!values)
		return;

	for (__u32 id = 0; id < (unsigned int)ACE_XDP_STAT_COUNT; id++) {
		__u64 total = 0;
		if (bpf_map_lookup_elem(stats_fd, &id, values) == 0)
			for (int cpu = 0; cpu < ncpu; cpu++)
				total += values[cpu];
		printf("stat[%u]=%llu%s", id, (unsigned long long)total,
			id == (unsigned int)(ACE_XDP_STAT_COUNT - 1) ? "\n" : " ");
	}
	free(values);
}

static void print_xsk_stats(const struct xsk_info *xsk)
{
	struct xdp_statistics stats = {};
	socklen_t stats_length = sizeof(stats);

	if (getsockopt(xsk_socket__fd(xsk->socket), SOL_XDP, XDP_STATISTICS,
			&stats, &stats_length) < 0) {
		fprintf(stderr, "cannot read AF_XDP statistics: %s\n",
			strerror(errno));
		return;
	}
	if (stats_length < sizeof(stats)) {
		fprintf(stderr,
			"AF_XDP statistics response is too short: %u bytes (expected %zu)\n",
			(unsigned int)stats_length, sizeof(stats));
		return;
	}

	printf("AF_XDP stats: rx_dropped=%llu rx_invalid_descs=%llu "
	       "rx_ring_full=%llu rx_fill_ring_empty_descs=%llu "
	       "tx_invalid_descs=%llu tx_ring_empty_descs=%llu\n",
		(unsigned long long)stats.rx_dropped,
		(unsigned long long)stats.rx_invalid_descs,
		(unsigned long long)stats.rx_ring_full,
		(unsigned long long)stats.rx_fill_ring_empty_descs,
		(unsigned long long)stats.tx_invalid_descs,
		(unsigned long long)stats.tx_ring_empty_descs);
}

static void print_receiver_stats(const struct umem_info *umem)
{
	printf("receiver stats: packets=%llu udp=%llu experiment=%llu "
	       "malformed=%llu unexpected_port=%llu metadata_invalid=%llu "
	       "record_overflow=%llu recycled_frames=%llu "
	       "umem_owned=%u umem_invariant_errors=%llu\n",
		received_packets, received_udp_packets,
		received_experiment_packets, malformed_packets,
		unexpected_port_packets, metadata_invalid_packets,
		record_overflow_packets, recycled_frames,
		umem->userspace_owned_frames, umem_invariant_errors);
}

/* ===== 프로그램 진입점 ===== */

int main(int argc, char **argv)
{
	struct receiver_options options = {};
	struct ace_xsk_user_record *records = NULL;
	struct ace_runtime_config runtime;
	struct ace_runtime_config latest_runtime;
	struct ace_xsk_owner self_owner = {};
	__u32 record_count = 0;
	__u32 xsk_map_id, stats_map_id;
	struct umem_info umem = {0};
	struct xsk_info xsk = {0};
	struct pollfd pollfd = {};
	size_t records_size;
	unsigned int ifindex;
	int xsk_map_fd = -1, xsk_owner_fd = -1, stats_fd = -1;
	int runtime_fd = -1;
	int output_fd = -1;
	int parse_result, err = 1;
	bool owner_claimed = false;

	parse_result = parse_options(argc, argv, &options);
	if (parse_result > 0)
		return 0;
	if (parse_result < 0) {
		usage(argv[0]);
		return 1;
	}
	if (!options.ifname[0] || strlen(options.ifname) >= IFNAMSIZ) {
		fprintf(stderr, "invalid interface name: %s\n", options.ifname);
		return 1;
	}
	if (options.queue_id >= (unsigned int)ACE_XDP_XSK_MAP_MAX_ENTRIES) {
		fprintf(stderr, "RX queue %u is outside xsk_map range\n",
			options.queue_id);
		return 1;
	}
	ifindex = if_nametoindex(options.ifname);
	if (!ifindex) {
		fprintf(stderr, "interface %s does not exist\n", options.ifname);
		return 1;
	}
	err = validate_rx_queue(options.ifname, options.queue_id);
	if (err) {
		fprintf(stderr, "interface %s has no RX queue %u\n",
			options.ifname, options.queue_id);
		return 1;
	}

	libbpf_set_strict_mode(LIBBPF_STRICT_ALL);
	libbpf_set_print(NULL);
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	self_owner.pid = (__u32)getpid();
	err = ace_process_start_ticks(getpid(), &self_owner.process_start_ticks);
	if (err) {
		fprintf(stderr, "cannot identify receiver process lifetime: %s\n",
			strerror(-err));
		goto out;
	}

	/* BPF loader가 pin한 map만 열고, 이 프로세스에서는 BPF object를 다시
	 * load하거나 XDP를 attach하지 않는다. */
	xsk_map_fd = bpf_obj_get(ACE_XDP_XSK_MAP_PIN);
	if (xsk_map_fd < 0) {
		err = -errno;
		fprintf(stderr, "cannot open pinned xsk_map (%s): %s\n",
			ACE_XDP_XSK_MAP_PIN, strerror(-err));
		goto out;
	}
	xsk_owner_fd = bpf_obj_get(ACE_XDP_XSK_OWNER_MAP_PIN);
	if (xsk_owner_fd < 0) {
		err = -errno;
		fprintf(stderr, "cannot open pinned XSK owner map (%s): %s\n",
			ACE_XDP_XSK_OWNER_MAP_PIN, strerror(-err));
		goto out;
	}
	stats_fd = bpf_obj_get(ACE_XDP_STATS_MAP_PIN);
	if (stats_fd < 0) {
		err = -errno;
		fprintf(stderr, "cannot open pinned stats map (%s): %s\n",
			ACE_XDP_STATS_MAP_PIN, strerror(-err));
		goto out;
	}
	runtime_fd = bpf_obj_get(ACE_XDP_RUNTIME_MAP_PIN);
	if (runtime_fd < 0) {
		err = -errno;
		fprintf(stderr, "cannot open pinned runtime map (%s): %s\n",
			ACE_XDP_RUNTIME_MAP_PIN, strerror(-err));
		goto out;
	}
	err = validate_pinned_map(xsk_map_fd, ACE_XDP_XSK_MAP_PIN,
		BPF_MAP_TYPE_XSKMAP, sizeof(__u32), sizeof(__u32),
		ACE_XDP_XSK_MAP_MAX_ENTRIES, "xsk_map", &xsk_map_id);
	if (err)
		goto out;
	err = validate_pinned_map(xsk_owner_fd, ACE_XDP_XSK_OWNER_MAP_PIN,
		BPF_MAP_TYPE_HASH, sizeof(__u32), sizeof(struct ace_xsk_owner),
		ACE_XDP_XSK_MAP_MAX_ENTRIES, "xsk_owners", NULL);
	if (err)
		goto out;
	err = validate_pinned_map(stats_fd, ACE_XDP_STATS_MAP_PIN,
		BPF_MAP_TYPE_PERCPU_ARRAY, sizeof(__u32), sizeof(__u64),
		ACE_XDP_STAT_COUNT, "stats", &stats_map_id);
	if (err)
		goto out;
	err = validate_pinned_map(runtime_fd, ACE_XDP_RUNTIME_MAP_PIN,
		BPF_MAP_TYPE_ARRAY, sizeof(__u32),
		sizeof(struct ace_runtime_config), 1, "runtime_config",
		NULL);
	if (err)
		goto out;
	{
		__u32 zero = 0;

		if (bpf_map_lookup_elem(runtime_fd, &zero, &runtime)) {
			err = -errno;
			fprintf(stderr, "cannot read runtime configuration: %s\n",
				strerror(-err));
			goto out;
		}
	}
	if (runtime.abi_version != ACE_XDP_RUNTIME_ABI_VERSION ||
	    runtime.ifindex != ifindex || !runtime.record_capacity ||
	    runtime.record_capacity > ACE_XDP_RECORD_MAX_ENTRIES ||
	    !runtime.xdp_program_id || !runtime.owner_pid ||
	    !runtime.owner_start_ticks ||
	    (runtime.flags & (ACE_RUNTIME_F_HWTSTAMP_CAPTURED |
			      ACE_RUNTIME_F_HWTSTAMP_APPLIED)) !=
		(ACE_RUNTIME_F_HWTSTAMP_CAPTURED |
		 ACE_RUNTIME_F_HWTSTAMP_APPLIED) ||
	    (runtime.flags & (ACE_RUNTIME_F_HWTSTAMP_APPLYING |
			      ACE_RUNTIME_F_STOPPING)) ||
	    (runtime.flags & ~(ACE_RUNTIME_F_HWTSTAMP_CAPTURED |
			       ACE_RUNTIME_F_HWTSTAMP_APPLYING |
			       ACE_RUNTIME_F_HWTSTAMP_APPLIED |
			       ACE_RUNTIME_F_STOPPING)) ||
	    runtime.effective_hwtstamp_rx_filter != HWTSTAMP_FILTER_ALL) {
		fprintf(stderr,
			"runtime configuration mismatch: abi=%u ifindex=%u records=%u "
			"program=%u owner=%u flags=%#x\n",
			runtime.abi_version, runtime.ifindex,
			runtime.record_capacity, runtime.xdp_program_id,
			runtime.owner_pid, runtime.flags);
		err = -EXDEV;
		goto out;
	}
	if (!options.records_explicit)
		options.record_capacity = runtime.record_capacity;
	err = require_live_loader_owner(&runtime);
	if (err) {
		fprintf(stderr, "runtime loader owner is not alive: %s\n",
			strerror(-err));
		goto out;
	}
	err = validate_attached_program((int)ifindex, xsk_map_id, stats_map_id,
		runtime.xdp_program_id);
	if (err)
		goto out;

	records_size = (size_t)options.record_capacity * sizeof(*records);
	/* 측정 루프에서 malloc/CSV 쓰기를 하지 않도록 저장 공간을 미리 잡는다.
	 * 이어서 메모리를 잠그고 페이지를 실제로 접근해 두어 첫 접근 시 발생하는
	 * page fault가 실험 도중 기록 지연으로 섞이는 것을 줄인다. */
	records = calloc(options.record_capacity, sizeof(*records));
	if (!records) {
		err = -ENOMEM;
		fprintf(stderr, "cannot allocate %zu-byte record buffer\n",
			records_size);
		goto out;
	}
	if (mlockall(MCL_CURRENT | MCL_FUTURE)) {
		err = -errno;
		fprintf(stderr, "cannot lock AF_XDP measurement memory: %s\n",
			strerror(-err));
		goto out;
	}
	/* Fault record pages in before receiving.  Volatile writes ensure the
	 * compiler cannot reduce this to another lazily allocated zero mapping. */
	for (size_t offset = 0; offset < records_size;
	     offset += (size_t)getpagesize())
		*((volatile unsigned char *)records + offset) = 0;

	err = create_umem(&umem);
	if (err) {
		fprintf(stderr, "UMEM creation failed: %s\n", strerror(-err));
		goto out;
	}
	err = fill_initial_frames(&umem);
	if (err) {
		fprintf(stderr, "FILL ring setup failed: %s\n", strerror(-err));
		goto out;
	}
	err = create_socket(&xsk, &umem, options.ifname, options.queue_id);
	if (err) {
		fprintf(stderr, "AF_XDP socket setup failed: %s\n", strerror(-err));
		goto out;
	}
	/* xsk_owners는 패킷 기록이 아니라 "이 queue의 수신기가 살아 있다"는
	 * 준비/종료 연락용 map이다. queue 사용을 먼저 표시하고 loader 상태를
	 * 다시 확인한 뒤 실제 XSKMAP에 등록한다. loader는 STOPPING을 공개한
	 * 뒤 이 표시를 확인하므로 수신기 준비 중의 XDP detach를 막을 수 있다. */
	err = claim_xsk_queue(xsk_owner_fd, options.queue_id, &self_owner);
	if (err) {
		if (err == -EEXIST)
			fprintf(stderr,
				"RX queue %u already has a receiver ownership claim\n",
				options.queue_id);
		else
			fprintf(stderr, "cannot claim RX queue %u: %s\n",
				options.queue_id, strerror(-err));
		goto out;
	}
	owner_claimed = true;
	{
		__u32 zero = 0;

		if (bpf_map_lookup_elem(runtime_fd, &zero, &latest_runtime)) {
			err = -errno;
			fprintf(stderr, "cannot re-read runtime configuration: %s\n",
				strerror(-err));
			goto out;
		}
	}
	if (memcmp(&latest_runtime, &runtime, sizeof(runtime))) {
		err = -ESTALE;
		fprintf(stderr,
			"loader runtime changed during AF_XDP setup; aborting registration\n");
		goto out;
	}
	err = require_live_loader_owner(&latest_runtime);
	if (err) {
		fprintf(stderr, "loader exited before XSKMAP registration: %s\n",
			strerror(-err));
		goto out;
	}
	err = validate_attached_program((int)ifindex, xsk_map_id, stats_map_id,
		latest_runtime.xdp_program_id);
	if (err)
		goto out;
	err = register_socket(&xsk, xsk_map_fd, options.queue_id);
	if (err) {
		if (err == -EEXIST)
			fprintf(stderr,
				"XSKMAP[%u] already has a live receiver; refusing to replace it\n",
				options.queue_id);
		else
			fprintf(stderr, "XSKMAP registration failed: %s\n",
				strerror(-err));
		goto out;
	}
	output_fd = open(options.output_path,
		O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
	if (output_fd < 0) {
		err = -errno;
		fprintf(stderr, "cannot create output file %s: %s\n",
			options.output_path, strerror(-err));
		goto out;
	}

	pollfd.fd = xsk_socket__fd(xsk.socket);
	pollfd.events = POLLIN;
	printf("AF_XDP ready: %s RX queue %u, XSKMAP[%u] registered; Ctrl-C to stop\n",
		options.ifname, options.queue_id, options.queue_id);
	err = 0;
	while (!stop) {
		int poll_result = poll(&pollfd, 1, 1000);
		if (poll_result < 0) {
			if (errno == EINTR) {
				if (stop)
					break;
				continue;
			}
			err = -errno;
			perror("poll");
			break;
		}
		if (!poll_result)
			continue;
		if (pollfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
			int socket_error = 0;
			socklen_t error_length = sizeof(socket_error);

			if (getsockopt(pollfd.fd, SOL_SOCKET, SO_ERROR, &socket_error,
					&error_length) < 0)
				socket_error = errno;
			else if (!socket_error)
				socket_error = EIO;
			fprintf(stderr, "AF_XDP poll failure: revents=%#x: %s\n",
				pollfd.revents, strerror(socket_error));
			err = -socket_error;
			break;
		}
		if (pollfd.revents & POLLIN) {
			int received = receive_batch(&xsk, &umem,
				options.queue_id, records, options.record_capacity,
				&record_count);

			if (received < 0) {
				fprintf(stderr, "AF_XDP receive failed: %s\n",
					strerror(-received));
				err = received;
				break;
			}
		}
	}
	print_receiver_stats(&umem);
	print_xsk_stats(&xsk);
	print_stats(stats_fd);
	/* 수신을 멈춘 뒤 한 번만 파일로 내보낸다. 다른 경로의 CSV와는
	 * 출력 행 번호가 아닌 flow_id + sequence로 연결해야 한다. */
	if (output_fd >= 0) {
		int save_err = save_user_records(output_fd, records, record_count);

		/* save_user_records consumes the descriptor on every path. */
		output_fd = -1;
		if (save_err) {
			fprintf(stderr, "cannot save AF_XDP records to %s: %s\n",
				options.output_path, strerror(errno));
			if (!err)
				err = -EIO;
		} else {
			printf("saved %u AF_XDP records to %s\n", record_count,
				options.output_path);
		}
	}

out:
	if (output_fd >= 0)
		close(output_fd);
	if (xsk.socket)
		/* The kernel removes this socket's own XSKMAP entries on close. */
		xsk_socket__delete(xsk.socket);
	if (owner_claimed) {
		int release_err = release_xsk_queue(xsk_owner_fd,
			options.queue_id, &self_owner);

		if (release_err) {
			fprintf(stderr, "cannot release RX queue %u ownership: %s\n",
				options.queue_id, strerror(-release_err));
			if (err >= 0)
				err = release_err;
		}
	}
	if (umem.umem)
		xsk_umem__delete(umem.umem);
	free(umem.buffer);
	if (stats_fd >= 0)
		close(stats_fd);
	if (runtime_fd >= 0)
		close(runtime_fd);
	if (xsk_map_fd >= 0)
		close(xsk_map_fd);
	if (xsk_owner_fd >= 0)
		close(xsk_owner_fd);
	free(records);
	munlockall();
	return err < 0 ? 1 : err;
}
