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

/* Default-preallocated HASH maps avoid allocation in the packet hot path. */
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
	void *ip_end;
	void *udp_end;
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
	ip_end = (void *)iph + ip_len;

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
	if ((void *)(udp + 1) > data_end || (void *)(udp + 1) > ip_end) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}

	udp_len = (__u32)bpf_ntohs(udp->len);
	if (udp_len < sizeof(*udp) || udp_len > ip_len - ihl_len ||
	    (void *)udp + udp_len > data_end || (void *)udp + udp_len > ip_end) {
		count_stat(ACE_XDP_STAT_IPV4_INVALID);
		return 0;
	}
	udp_end = (void *)udp + udp_len;

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
	    payload + sizeof(wire) > data_end ||
	    payload + sizeof(wire) > udp_end) {
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

	err = bpf_map_update_elem(&ingress_records, key, &record, BPF_NOEXIST);
	if (!err)
		count_stat(ACE_XDP_STAT_INGRESS_RECORDED);
	else if (err == -ACE_EEXIST)
		count_stat(ACE_XDP_STAT_INGRESS_DUPLICATE);
	else
		count_stat(ACE_XDP_STAT_INGRESS_UPDATE_ERROR);
}

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
