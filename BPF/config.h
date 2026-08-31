/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_XDP_CONFIG_H
#define ACE_XDP_CONFIG_H

/* BPF program, loader, AF_XDP receiver가 공유하는 고정 정책과 경로. */
#define ACE_XDP_DEFAULT_IFNAME   "eth0"
#define ACE_XDP_PIN_DIR          "/sys/fs/bpf/ace_xdp"
#define ACE_XDP_XSK_MAP_PIN      ACE_XDP_PIN_DIR "/xsk_map"
#define ACE_XDP_STATS_MAP_PIN    ACE_XDP_PIN_DIR "/stats"

enum {
	ACE_XDP_RT_PORT = 9001,
	ACE_XDP_XSK_PORT = 9002,
	ACE_XDP_RT_CPU = 3,
	ACE_XDP_CPU_MAP_QUEUE_SIZE = 256,
	ACE_XDP_MAP_MAX_ENTRIES = 128,
	ACE_XDP_STAT_COUNT = 4,
	ACE_XSK_DEFAULT_QUEUE = 0,
};

enum ace_xdp_stat_id {
	ACE_XDP_STAT_PASS = 0,
	ACE_XDP_STAT_CPUMAP = 1,
	ACE_XDP_STAT_XSK = 2,
	ACE_XDP_STAT_TOTAL = 3,
};

_Static_assert(ACE_XDP_RT_CPU < ACE_XDP_MAP_MAX_ENTRIES,
	"RT CPU must fit in cpu_map");
_Static_assert(ACE_XDP_STAT_TOTAL + 1 == ACE_XDP_STAT_COUNT,
	"stat count must match ace_xdp_stat_id");

#endif
