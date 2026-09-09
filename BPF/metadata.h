/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_XDP_METADATA_H
#define ACE_XDP_METADATA_H

/*
 * Shared packet/metadata ABI for the BPF program and userspace consumers.
 *
 * vmlinux.h supplies these types for BPF builds.  Normal userspace translation
 * units get them from linux/types.h instead, which keeps this header usable on
 * both sides without including kernel-internal headers in the BPF program.
 */
#ifndef __VMLINUX_H__
#include <linux/types.h>
#endif

#include "../include/ace_packet_abi.h"

#define ACE_RX_META_MAGIC         0x4143454dU /* "ACEM", native byte order */
#define ACE_RX_META_VERSION       1U
#define ACE_RX_META_SIZE          56U

/* Host-endian key shared by ingress_records and cpumap_records. */
struct ace_packet_key {
	__u32 flow_id;
	__u32 reserved;
	__u64 sequence;
};

/* This is the classifier's requested path, not proof of final delivery. */
enum ace_requested_path {
	ACE_PATH_PASS = 0,
	ACE_PATH_CPUMAP = 1,
	ACE_PATH_XSK = 2,
};

enum ace_rx_meta_flags {
	ACE_META_F_HWTS_VALID       = 1U << 0,
	ACE_META_F_PACKET_ID_VALID  = 1U << 1,
	ACE_META_F_CPUMAP_SEEN      = 1U << 2,
	ACE_META_F_TIMESTAMP_ERROR  = 1U << 3,
	ACE_META_F_PATH_REQUESTED   = 1U << 4,
};

/*
 * Private XDP metadata stored immediately before packet data.  Numeric fields
 * use native byte order because the producer and consumers run on one host.
 */
struct ace_rx_meta {
	__u32 magic;
	__u16 version;
	__u16 size;
	__u32 flags;
	__u32 flow_id;
	__u64 sequence;
	__u64 hw_rx_ns;
	__u64 initial_xdp_ns;
	__u64 cpumap_ns;
	__u32 rx_queue;
	__u16 requested_path;
	__s16 timestamp_error;
};

/* Stable values stored in the two measurement hash maps. */
struct ace_ingress_record {
	__u32 flow_id;
	__u32 rx_queue;
	__u64 sequence;
	__u64 hw_rx_ns;
	__u64 initial_xdp_ns;
	__u32 flags;
	__u16 requested_path;
	__s16 timestamp_error;
};

struct ace_cpumap_record {
	__u32 flow_id;
	__u32 cpu;
	__u64 sequence;
	__u64 cpumap_ns;
	__u32 flags;
	__u16 requested_path;
	__u16 reserved;
};

enum ace_xsk_record_flags {
	ACE_XSK_RECORD_F_META_VALID = 1U << 0,
	ACE_XSK_RECORD_F_KEY_MATCH = 1U << 1,
	ACE_XSK_RECORD_F_PATH_MATCH = 1U << 2,
};

/* AF_XDP userspace observation; buffered in memory and exported after a run. */
struct ace_xsk_user_record {
	__u32 flow_id;
	__u32 rx_queue;
	__u64 sequence;
	__s64 tx_realtime_ns;
	__u64 user_rx_mono_ns;
	__s64 user_rx_real_ns;
	__u64 hw_rx_ns;
	__u64 initial_xdp_ns;
	__u32 meta_flags;
	__u16 requested_path;
	__s16 timestamp_error;
	__u32 packet_length;
	__u32 validation_flags;
};

enum ace_runtime_flags {
	ACE_RUNTIME_F_HWTSTAMP_CAPTURED = 1U << 0,
	ACE_RUNTIME_F_HWTSTAMP_APPLYING = 1U << 1,
	ACE_RUNTIME_F_HWTSTAMP_APPLIED = 1U << 2,
	ACE_RUNTIME_F_STOPPING = 1U << 3,
};

/* Userspace lifecycle claim for one XSKMAP queue.  A separate HASH is needed
 * because XSKMAP get_next_key enumerates every possible queue, not only live
 * socket entries, and XSKMAP lookup is intentionally unsupported. */
struct ace_xsk_owner {
	__u64 process_start_ticks;
	__u32 pid;
	__u32 reserved;
};

/* Loader-owned identity/configuration published to map consumers and crash
 * recovery.  owner_start_ticks disambiguates PID reuse within the same boot. */
struct ace_runtime_config {
	__u32 abi_version;
	__u32 ifindex;
	__u32 record_capacity;
	__u32 xdp_program_id;
	__u64 owner_start_ticks;
	__u32 owner_pid;
	__u32 flags;
	__s32 saved_hwtstamp_flags;
	__s32 saved_hwtstamp_tx_type;
	__s32 saved_hwtstamp_rx_filter;
	__s32 effective_hwtstamp_flags;
	__s32 effective_hwtstamp_tx_type;
	__s32 effective_hwtstamp_rx_filter;
};

_Static_assert(sizeof(struct ace_packet_key) == 16,
	"packet key ABI changed");
_Static_assert(sizeof(struct ace_rx_meta) == ACE_RX_META_SIZE,
	"XDP metadata ABI changed");
_Static_assert(sizeof(struct ace_ingress_record) == 40,
	"ingress record ABI changed");
_Static_assert(sizeof(struct ace_cpumap_record) == 32,
	"CPUMAP record ABI changed");
_Static_assert(sizeof(struct ace_xsk_user_record) == 72,
	"AF_XDP userspace record ABI changed");
_Static_assert(sizeof(struct ace_runtime_config) == 56,
	"runtime config ABI changed");
_Static_assert(sizeof(struct ace_xsk_owner) == 16,
	"XSK owner ABI changed");

#endif /* ACE_XDP_METADATA_H */
