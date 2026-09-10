// SPDX-License-Identifier: GPL-2.0
//
// Native XDP classifier and per-packet measurement instrumentation.
//
//   UDP/9000 -> requested PASS path
//   UDP/9001 -> requested CPUMAP path (CPU 3)
//   UDP/9002 -> requested XSK path (current receiver uses XDP_COPY)
//
// requested_path records the classifier decision.  It is not proof that a
// redirect was delivered; consumers and redirect/error counters provide that
// evidence separately.
//
// 읽는 순서: xdp_dispatch() -> parse_experiment_packet()
//            -> prepare_measurement_metadata() -> dispatch_requested_path()
// CPUMAP으로 보낸 패킷은 목적 CPU에서 xdp_cpumap_measure()를 한 번 더 탄다.
// 첫 프로그램은 "NIC 수신 직후", 두 번째는 "CPU 이동 후"의 관찰 지점이다.
// 두 시점의 기록을 같은 flow_id + sequence로 연결하므로, CPU가 바뀌어도
// 나중에 분석할 때 어느 패킷의 지연인지 구분할 수 있다.

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#include "config.h"
#include "metadata.h"

#define ETH_P_IP                0x0800
#define ACE_IPV4_FRAGMENT_MASK  0x3fff /* MF plus fragment offset; DF excluded */
#define ACE_EEXIST              17

extern int bpf_xdp_metadata_rx_timestamp(const struct xdp_md *ctx,
					 __u64 *timestamp) __ksym;

/* CPUMAP entry also carries the destination-CPU XDP program FD. */
struct {
	__uint(type, BPF_MAP_TYPE_CPUMAP);
	__uint(max_entries, ACE_XDP_CPU_MAP_MAX_ENTRIES);
	__type(key, __u32);
	__type(value, struct bpf_cpumap_val);
} cpu_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_XSKMAP);
	__uint(max_entries, ACE_XDP_XSK_MAP_MAX_ENTRIES);
	__type(key, __u32);
	__type(value, __u32);
} xsk_map SEC(".maps");

/* Userspace-only XSK queue claims make receiver setup and loader teardown
 * mutually observable.  XSKMAP itself cannot report occupancy by lookup. */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, ACE_XDP_XSK_MAP_MAX_ENTRIES);
	__type(key, __u32);
	__type(value, struct ace_xsk_owner);
} xsk_owners SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, ACE_XDP_STAT_COUNT);
	__type(key, __u32);
	__type(value, __u64);
} stats SEC(".maps");

/* Default-preallocated HASH maps avoid allocation in the packet hot path.
 *
 * 이 두 map은 전달 경로가 아니라 측정용 장부다. ingress_records는 최초 XDP,
 * cpumap_records는 목적 CPU 도착을 기록한다. 서로 다른 flow가 sequence=0부터
 * 시작해도 복합 key가 달라 충돌하지 않는다. 별도 run_id는 key에 넣지 않으며,
 * 실험마다 새 map을 만들고 결과 디렉터리로 실행을 구분하는 것이 전제다.
 * LRU처럼 오래된 기록을 자동 제거하지 않는다. 용량 초과 시 새 기록 실패를
 * counter로 남기므로 loader의 --records는 한 실행의 예상 패킷 수에 맞춘다.
 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, ACE_XDP_RECORD_DEFAULT_ENTRIES);
	__type(key, struct ace_packet_key);
	__type(value, struct ace_ingress_record);
} ingress_records SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, ACE_XDP_RECORD_DEFAULT_ENTRIES);
	__type(key, struct ace_packet_key);
	__type(value, struct ace_cpumap_record);
} cpumap_records SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct ace_runtime_config);
} runtime_config SEC(".maps");

static __always_inline void count_stat(__u32 id)
{
	__u64 *value = bpf_map_lookup_elem(&stats, &id);

	if (value)
		(*value)++;
}

/*
 * Parse only the fixed experiment envelope.  Return 1 for a valid experiment
 * packet, 0 for unrelated traffic, and -1 for an experiment port carrying an
 * invalid/missing experiment header.  IPv4 fragments are deliberately passed
 * without L4 classification, so non-initial fragments cannot be mistaken for
 * UDP headers.
 */
static __always_inline int parse_experiment_packet(struct xdp_md *ctx,
						   struct ace_packet_key *key,
						   __u16 *requested_path)
{
	void *data = (void *)(long)ctx->data;
	void *data_end = (void *)(long)ctx->data_end;
	struct ace_experiment_header wire = {};
	struct ethhdr *eth = data;
	struct iphdr *iph;
	struct udphdr *udp;
	void *payload;
	__u32 ihl_len;
	__u32 ip_len;
	__u32 udp_len;
	__u16 frag_off;
	__u16 dest;

	if ((void *)(eth + 1) > data_end)
		return 0;
	if (eth->h_proto != bpf_htons(ETH_P_IP))
		return 0;

	iph = (void *)(eth + 1);
	if ((void *)(iph + 1) > data_end) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}
	if (iph->version != 4 || iph->ihl < 5) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}

	ihl_len = (__u32)iph->ihl * 4;
	ip_len = (__u32)bpf_ntohs(iph->tot_len);
	if (ip_len < ihl_len || (void *)iph + ihl_len > data_end ||
	    (void *)iph + ip_len > data_end) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}

	frag_off = bpf_ntohs(iph->frag_off);
	if (frag_off & ACE_IPV4_FRAGMENT_MASK) {
		count_stat(ACE_XDP_STAT_IPV4_FRAGMENT);
		return 0;
	}
	if (iph->protocol != IPPROTO_UDP)
		return 0;
	if (ip_len < ihl_len + sizeof(*udp)) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}

	udp = (void *)iph + ihl_len;
	/* IP/UDP 내부 길이는 위아래의 정수 비교로 확인하고, 실제 메모리 접근은
	 * 반드시 data_end와 직접 비교한다. ip_end 같은 중간 포인터와 함께
	 * 비교하면 LLVM이 data_end 검사를 중복으로 제거할 수 있는데, verifier는
	 * 두 packet 포인터 사이의 비교만으로는 읽을 수 있는 범위를 인정하지 않는다.
	 */
	if ((void *)(udp + 1) > data_end) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}

	udp_len = (__u32)bpf_ntohs(udp->len);
	if (udp_len < sizeof(*udp) || udp_len > ip_len - ihl_len ||
	    (void *)udp + udp_len > data_end) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}

	dest = bpf_ntohs(udp->dest);
	if (dest == ACE_XDP_PASS_PORT)
		*requested_path = ACE_PATH_PASS;
	else if (dest == ACE_XDP_RT_PORT)
		*requested_path = ACE_PATH_CPUMAP;
	else if (dest == ACE_XDP_XSK_PORT)
		*requested_path = ACE_PATH_XSK;
	else
		return 0;

	payload = (void *)(udp + 1);
	if (udp_len < sizeof(*udp) + sizeof(wire) ||
	    payload + sizeof(wire) > data_end) {
		count_stat(ACE_XDP_STAT_PACKET_HEADER_INVALID);
		return -1;
	}

	/* Packet payload starts at an unaligned Ethernet offset: copy before read. */
	__builtin_memcpy(&wire, payload, sizeof(wire));
	if (bpf_ntohl(wire.magic) != ACE_PACKET_MAGIC ||
	    bpf_ntohs(wire.version) != ACE_PACKET_VERSION ||
	    bpf_ntohs(wire.size) != ACE_PACKET_HEADER_SIZE ||
	    bpf_ntohl(wire.reserved) != 0) {
		count_stat(ACE_XDP_STAT_PACKET_HEADER_INVALID);
		return -1;
	}

	key->flow_id = bpf_ntohl(wire.flow_id);
	key->reserved = 0;
	key->sequence = bpf_be64_to_cpu(wire.sequence);
	return 1;
}

static __always_inline void record_ingress(const struct ace_packet_key *key,
						   const struct ace_rx_meta *meta)
{
	struct ace_ingress_record record = {
		.flow_id = key->flow_id,
		.rx_queue = meta->rx_queue,
		.sequence = key->sequence,
		.hw_rx_ns = meta->hw_rx_ns,
		.initial_xdp_ns = meta->initial_xdp_ns,
		.flags = meta->flags,
		.requested_path = meta->requested_path,
		.timestamp_error = meta->timestamp_error,
	};
	int err;

	/* 같은 ID가 또 와도 첫 관찰값을 덮어쓰지 않는다. 중복 패킷 수는 따로
	 * 세므로 "최초 수신 시간"과 "같은 ID가 여러 번 온 현상"을 구분한다.
	 */
	err = bpf_map_update_elem(&ingress_records, key, &record, BPF_NOEXIST);
	if (!err)
		count_stat(ACE_XDP_STAT_INGRESS_RECORDED);
	else if (err == -ACE_EEXIST)
		count_stat(ACE_XDP_STAT_INGRESS_DUPLICATE);
	else
		count_stat(ACE_XDP_STAT_INGRESS_UPDATE_ERROR);
}

/* wire header와 달리 이 metadata는 송신자가 보내는 데이터가 아니다.
 * 수신 장치가 Ethernet header 앞의 headroom에 추가하는 내부 메모다.
 *
 *   [남은 headroom][ace_rx_meta][Ethernet][IPv4][UDP][실험 header][payload]
 * data_meta는 메모의 시작, data는 Ethernet header의 시작을 가리킨다.
 *
 * 패킷 본문은 유지되고 CPUMAP/AF_XDP 소비자는 본문 앞의 메모를 이어받는다.
 * 동시에 ingress map에도 복사해 두므로 일반 UDP 수신처럼 이 메모를 직접
 * 읽지 않는 경로도 packet ID로 최초 XDP 기록과 연결할 수 있다.
 */
static __always_inline int prepare_measurement_metadata(struct xdp_md *ctx,
							 const struct ace_packet_key *key,
							 __u16 requested_path,
							 __u64 initial_xdp_ns)
{
	void *data;
	void *data_meta;
	struct ace_rx_meta *meta;
	__u64 hw_rx_ns = 0;
	int ts_err;

	if (bpf_xdp_adjust_meta(ctx, -(int)sizeof(*meta))) {
		count_stat(ACE_XDP_STAT_META_ADJUST_ERROR);
		return -1;
	}

	/* adjust_meta invalidates all packet pointers from the parser. */
	data = (void *)(long)ctx->data;
	data_meta = (void *)(long)ctx->data_meta;
	meta = data_meta;
	if ((void *)(meta + 1) > data) {
		count_stat(ACE_XDP_STAT_META_BOUNDS_ERROR);
		return -1;
	}

	__builtin_memset(meta, 0, sizeof(*meta));
	meta->magic = ACE_RX_META_MAGIC;
	meta->version = ACE_RX_META_VERSION;
	meta->size = sizeof(*meta);
	meta->flags = ACE_META_F_PACKET_ID_VALID | ACE_META_F_PATH_REQUESTED;
	meta->flow_id = key->flow_id;
	meta->sequence = key->sequence;
	meta->initial_xdp_ns = initial_xdp_ns;
	meta->rx_queue = ctx->rx_queue_index;
	meta->requested_path = requested_path;

	/* 포팅한 macb callback이 현재 RX descriptor의 timestamp를 읽는다.
	 * hw_rx_ns는 NIC의 PHC 시계, initial_xdp_ns는 커널 monotonic 시계다.
	 * 둘 다 ns여도 원점이 다르므로 여기서 빼지 않고 분석 단계에서 보정한다.
	 * HW 값이 없으면 소프트웨어 시간으로 대체하지 않고 validity flag로 남긴다.
	 */
	ts_err = bpf_xdp_metadata_rx_timestamp(ctx, &hw_rx_ns);
	if (!ts_err) {
		meta->hw_rx_ns = hw_rx_ns;
		meta->flags |= ACE_META_F_HWTS_VALID;
		count_stat(ACE_XDP_STAT_HWTS_VALID);
	} else {
		meta->flags |= ACE_META_F_TIMESTAMP_ERROR;
		meta->timestamp_error = (__s16)ts_err;
		count_stat(ACE_XDP_STAT_HWTS_ERROR);
	}

	record_ingress(key, meta);
	return 0;
}

/* CPUMAP key는 목적 CPU 번호, XSKMAP key는 현재 NIC의 RX queue 번호다.
 * 즉 같은 redirect helper를 쓰더라도 두 map이 가리키는 대상은 다르다.
 * XDP_REDIRECT 반환은 전달 요청이 접수됐다는 뜻이지 최종 수신의 증거가 아니다.
 * map entry가 없으면 helper의 마지막 인자(XDP_PASS)에 따라 일반 stack으로 간다.
 */
static __always_inline int dispatch_requested_path(struct xdp_md *ctx,
							   __u16 requested_path)
{
	__u32 key;
	int action;

	if (requested_path == ACE_PATH_CPUMAP) {
		key = ACE_XDP_RT_CPU;
		action = bpf_redirect_map(&cpu_map, key, XDP_PASS);
		if (action == XDP_REDIRECT) {
			count_stat(ACE_XDP_STAT_CPUMAP);
			return action;
		}
		count_stat(ACE_XDP_STAT_CPUMAP_FALLBACK);
	} else if (requested_path == ACE_PATH_XSK) {
		key = ctx->rx_queue_index;
		action = bpf_redirect_map(&xsk_map, key, XDP_PASS);
		if (action == XDP_REDIRECT) {
			count_stat(ACE_XDP_STAT_XSK);
			return action;
		}
		count_stat(ACE_XDP_STAT_XSK_FALLBACK);
	} else {
		count_stat(ACE_XDP_STAT_EXPERIMENT_PASS);
	}

	count_stat(ACE_XDP_STAT_PASS);
	return XDP_PASS;
}

SEC("xdp")
int xdp_dispatch(struct xdp_md *ctx)
{
	struct ace_packet_key packet_key = {};
	/* 파싱/분류/기록 비용도 이후 관찰 시점까지의 구간에 포함시키기 위해
	 * 프로그램 진입 초기에 찍는다. NIC hardware 수신 시각 그 자체는 아니다.
	 */
	__u64 initial_xdp_ns = bpf_ktime_get_ns();
	__u16 requested_path = ACE_PATH_PASS;
	int parsed;

	count_stat(ACE_XDP_STAT_TOTAL);
	parsed = parse_experiment_packet(ctx, &packet_key, &requested_path);
	if (parsed > 0 &&
	    !prepare_measurement_metadata(ctx, &packet_key, requested_path,
					  initial_xdp_ns))
		return dispatch_requested_path(ctx, requested_path);

	/* Malformed experiment envelopes and metadata setup failures are never
	 * redirected into a consumer that could mistake stale metadata as valid. */
	count_stat(ACE_XDP_STAT_PASS);
	return XDP_PASS;
}

/* CPUMAP worker가 목적 CPU에서 실행하는 별도 XDP 프로그램이다.
 * metadata 검사 뒤 찍는 cpumap_ns와 최초 XDP 시간의 차이는 CPU 전달/대기와
 * 두 관찰 지점 사이의 처리 비용을 포함한다. 순수 scheduler latency는 아니다.
 * 마지막 XDP_PASS는 원래 CPU로 되돌린다는 뜻이 아니라 이 CPU에서 이어서
 * 일반 network stack으로 올린다는 뜻이다. 여기서는 HW kfunc를 다시 부르지
 * 않고 최초 NIC RX 때 저장한 metadata를 사용한다.
 */
SEC("xdp/cpumap")
int xdp_cpumap_measure(struct xdp_md *ctx)
{
	void *data = (void *)(long)ctx->data;
	void *data_meta = (void *)(long)ctx->data_meta;
	struct ace_cpumap_record record = {};
	struct ace_packet_key key = {};
	struct ace_rx_meta *meta = data_meta;
	__u64 cpumap_ns;
	int err;

	if ((void *)(meta + 1) > data ||
	    meta->magic != ACE_RX_META_MAGIC ||
	    meta->version != ACE_RX_META_VERSION ||
	    meta->size != sizeof(*meta) ||
	    !(meta->flags & ACE_META_F_PACKET_ID_VALID) ||
	    meta->requested_path != ACE_PATH_CPUMAP) {
		count_stat(ACE_XDP_STAT_CPUMAP_META_INVALID);
		return XDP_PASS;
	}

	cpumap_ns = bpf_ktime_get_ns();
	meta->cpumap_ns = cpumap_ns;
	meta->flags |= ACE_META_F_CPUMAP_SEEN;

	key.flow_id = meta->flow_id;
	key.sequence = meta->sequence;
	record.flow_id = meta->flow_id;
	record.cpu = bpf_get_smp_processor_id();
	record.sequence = meta->sequence;
	record.cpumap_ns = cpumap_ns;
	record.flags = meta->flags;
	record.requested_path = meta->requested_path;

	err = bpf_map_update_elem(&cpumap_records, &key, &record, BPF_NOEXIST);
	if (!err)
		count_stat(ACE_XDP_STAT_CPUMAP_RECORDED);
	else if (err == -ACE_EEXIST)
		count_stat(ACE_XDP_STAT_CPUMAP_DUPLICATE);
	else
		count_stat(ACE_XDP_STAT_CPUMAP_UPDATE_ERROR);

	return XDP_PASS;
}

char LICENSE[] SEC("license") = "GPL";
