// SPDX-License-Identifier: GPL-2.0
//
// xdp_kern.c — XDP 패킷 분류기 (커널에서 실행되는 BPF 프로그램)
//
// XDP hook에서 IPv4/UDP 패킷을 확인하고 destination port에 따라 경로를
// 선택한다. 현재 포트 규칙은 packet-path 검증을 위한 임시 정책이다.
//
//   UDP 9001 → CPUMAP[3] → RT CPU
//   UDP 9002 → XSKMAP[RX queue] → AF_XDP userspace
//   그 외    → XDP_PASS → 일반 Linux 네트워크 스택

#include "vmlinux.h"            /* 현재 커널 BTF에서 bpftool로 생성한 타입 정의 */
#include <bpf/bpf_helpers.h>    /* SEC(), map helper 등 BPF 공용 매크로 */
#include <bpf/bpf_endian.h>     /* bpf_htons() 등 바이트 순서 변환 helper */
#include "config.h"

/* ===== 분류 정책 ===== */

#define ETH_P_IP   0x0800       /* IPv4 EtherType; 비교할 때 bpf_htons() 사용 */

/* ===== BPF map ===== */

/*
 * CPUMAP: 패킷을 지정 CPU의 CPUMAP processing path로 보낸다.
 *
 * key는 CPU 번호, value는 해당 CPU의 CPUMAP queue size다. 엔트리는
 * userspace loader가 등록한다. CPUMAP은 NIC RX queue를 직접 polling하지
 * 않으며, redirect된 frame을 지정 CPU의 후속 처리 경로로 전달한다.
 */
struct {
	__uint(type, BPF_MAP_TYPE_CPUMAP);
	__uint(max_entries, ACE_XDP_CPU_MAP_MAX_ENTRIES);
	__type(key, __u32);         /* CPU 번호 */
	__type(value, __u32);       /* CPUMAP queue size */
} cpu_map SEC(".maps");

/*
 * XSKMAP: RX queue와 AF_XDP socket을 연결한다.
 *
 * XSKMAP은 packet data를 저장하지 않는다. key는 RX queue index이며,
 * userspace가 해당 key에 AF_XDP socket FD를 등록해야 redirect가 성공한다.
 * 등록된 socket이 없으면 프로그램은 XDP_PASS로 fallback한다.
 */
struct {
	__uint(type, BPF_MAP_TYPE_XSKMAP);
	__uint(max_entries, ACE_XDP_XSK_MAP_MAX_ENTRIES);
	__type(key, __u32);         /* RX queue index */
	__type(value, __u32);       /* userspace가 등록하는 AF_XDP socket FD */
} xsk_map SEC(".maps");

/*
 * Per-CPU 통계 배열
 *
 * CPU별로 counter를 따로 유지하므로 BPF 측에서 lock이 필요 없다.
 * userspace는 모든 CPU의 값을 합산해 전체 통계를 계산한다.
 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, ACE_XDP_STAT_COUNT);
	__type(key, __u32);
	__type(value, __u64);
} stats SEC(".maps");

/* ===== 통계 helper ===== */

/*
 * map에서 현재 CPU의 counter를 찾고 1 증가시킨다. lookup 결과는 NULL일
 * 수 있으므로, verifier가 요구하는 NULL 검사를 거친 뒤 값을 변경한다.
 */
static __always_inline void count_stat(__u32 id)
{
	__u64 *value = bpf_map_lookup_elem(&stats, &id);
	if (value)
		(*value)++;
}

/* ===== XDP entry point ===== */

/*
 * NIC에서 packet이 들어올 때마다 호출되는 분류 함수다. SEC("xdp")가
 * XDP program임을 지정한다.
 *
 * 반환값은 XDP_PASS 또는 bpf_redirect_map()이 반환한 XDP_REDIRECT다.
 */
SEC("xdp")
int xdp_dispatch(struct xdp_md *ctx)
{
	/* 1. 패킷 범위: 이후 모든 헤더 접근은 data_end 검사 후 수행한다. */
	void *data     = (void *)(long)ctx->data;
	void *data_end = (void *)(long)ctx->data_end;

	/* 2. Ethernet: IPv4 packet만 다음 단계로 보낸다. */
	struct ethhdr *eth = data;
	__u32 key;

	/* 경로를 선택하기 전에 전체 수신 수를 먼저 기록한다. */
	count_stat(ACE_XDP_STAT_TOTAL);

	/* Ethernet header가 packet 범위 안에 있는지 확인한다. */
	if ((void *)(eth + 1) > data_end)
		goto pass;

	/* IPv4가 아니면 이 분류기의 대상이 아니다. */
	if (eth->h_proto != bpf_htons(ETH_P_IP))
		goto pass;

	/* 3. IPv4: 최소 header와 UDP 여부를 확인한다. */
	struct iphdr *iph = (void *)(eth + 1);

	/* header 범위, 최소 IHL(5 = 20 bytes), UDP protocol을 확인한다. */
	if ((void *)(iph + 1) > data_end || iph->ihl < 5 || iph->protocol != IPPROTO_UDP)
		goto pass;

	/* IP option을 고려해 고정 20 bytes가 아닌 IHL 기반 길이를 사용한다. */
	__u32 ihl_len = (__u32)iph->ihl * 4;
	if ((void *)iph + ihl_len > data_end)
		goto pass;

	/* 4. UDP: IP header 길이 뒤에 있는 UDP header를 확인한다. */
	struct udphdr *udp = (void *)iph + ihl_len;
	if ((void *)(udp + 1) > data_end)
		goto pass;

	/* 5. 분류: destination port에 따라 redirect하고, 실패하면 PASS한다. */
	/* UDP dst 9001: CPUMAP을 통해 RT CPU로 보낸다. */
	if (udp->dest == bpf_htons(ACE_XDP_RT_PORT)) {
		key = ACE_XDP_RT_CPU;
		/*
		 * 세 번째 인자 XDP_PASS는 map 엔트리가 없을 때의 fallback이다.
		 *
		 * redirect 성공 시에만 CPUMAP 통계를 기록한다. 실패하면 pass label로
		 * 이동해 PASS 통계를 기록한다.
		 */
		int action = bpf_redirect_map(&cpu_map, key, XDP_PASS);
		if (action == XDP_REDIRECT) {
			count_stat(ACE_XDP_STAT_CPUMAP);
			return action;
		}
		goto pass; /* userspace가 CPUMAP[3]을 설정하기 전까지 fallback */
	}

	/* UDP dst 9002: RX queue에 등록된 AF_XDP socket으로 보낸다. */
	if (udp->dest == bpf_htons(ACE_XDP_XSK_PORT)) {
		key = ctx->rx_queue_index;   /* XSKMAP key = 현재 RX queue index */
		/*
		 * 해당 queue에 AF_XDP socket이 등록되어 있으면 redirect한다. 등록되지
		 * 않은 경우 세 번째 인자(XDP_PASS)가 적용되어 일반 경로로 전달된다.
		 */
		int action = bpf_redirect_map(&xsk_map, key, XDP_PASS);
		if (action == XDP_REDIRECT) {
			count_stat(ACE_XDP_STAT_XSK);
			return action;
		}
	}

pass:
	/* 분류 대상이 아니거나 redirect map이 준비되지 않은 packet. */
	count_stat(ACE_XDP_STAT_PASS);
	return XDP_PASS;
}

char LICENSE[] SEC("license") = "GPL";
