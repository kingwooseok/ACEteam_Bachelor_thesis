// SPDX-License-Identifier: GPL-2.0
//
// xdp_kern.c — XDP 패킷 분류기 (커널 BPF 프로그램)
//
// NIC에서 수신된 패킷을 가장 이른 시점(XDP hook)에서 분류하여
// 세 가지 처리 경로 중 하나로 보낸다:
//
//   1) CPUMAP  → RT 전용 CPU로 redirect (UDP dst 9001)
//   2) XSKMAP → AF_XDP 소켓으로 redirect (UDP dst 9002)
//   3) XDP_PASS → 일반 Linux 네트워크 스택으로 전달 (그 외)
//
// 현재 분류 기준은 UDP destination port를 사용하는 임시 규칙이다.
// 나중에 실제 automotive protocol이나 다른 패킷 헤더 조건으로 교체 가능하다.

#include "vmlinux.h"            /* bpftool로 현재 커널 BTF에서 생성한 전체 커널 타입 헤더 */
#include <bpf/bpf_helpers.h>    /* SEC(), bpf_map_lookup_elem() 등 BPF 헬퍼 매크로 */
#include <bpf/bpf_endian.h>     /* bpf_htons() 등 엔디안 변환 헬퍼 */

/* --------------------------------------------------------------------------
 * 상수 정의
 * -------------------------------------------------------------------------- */

#define ETH_P_IP   0x0800       /* IPv4 EtherType (네트워크 바이트 순서로 비교 시 bpf_htons 사용) */

#define RT_PORT    9001         /* 이 UDP dst port로 오는 패킷 → CPUMAP (RT CPU)으로 redirect */
#define XSK_PORT   9002         /* 이 UDP dst port로 오는 패킷 → XSKMAP (AF_XDP)으로 redirect */
#define RT_CPU     3            /* CPUMAP redirect 대상 CPU 번호 (RT 격리 CPU) */

/* --------------------------------------------------------------------------
 * 통계 ID (per-CPU 통계 map의 key)
 * -------------------------------------------------------------------------- */

enum stat_id {
	STAT_PASS   = 0,            /* XDP_PASS로 처리된 패킷 수 */
	STAT_CPUMAP = 1,            /* CPUMAP으로 redirect된 패킷 수 */
	STAT_XSK    = 2,            /* XSKMAP(AF_XDP)으로 redirect된 패킷 수 */
	STAT_TOTAL  = 3,            /* 전체 수신 패킷 수 */
};

/* --------------------------------------------------------------------------
 * BPF Map 선언
 * -------------------------------------------------------------------------- */

/*
 * cpu_map: CPUMAP 타입 맵
 *
 * XDP에서 bpf_redirect_map()을 사용해 특정 CPU의 CPUMAP processing path로
 * 패킷을 redirect한다. key는 CPU 번호, value는 해당 CPU의 큐 크기이다.
 * 큐 크기는 userspace loader(xdp_loader)에서 설정한다.
 *
 * 주의: CPUMAP이 NIC RX queue를 직접 polling하는 것이 아니라,
 *       XDP에서 redirect된 프레임이 지정 CPU의 처리 경로로 전달되고
 *       이후 커널 네트워크 스택으로 들어가는 구조이다.
 */
struct {
	__uint(type, BPF_MAP_TYPE_CPUMAP);
	__uint(max_entries, 128);   /* 최대 128개 CPU 지원 (임베디드 환경에선 충분) */
	__type(key, __u32);         /* CPU 번호 */
	__type(value, __u32);       /* 해당 CPU의 CPUMAP 큐 크기 (userspace에서 설정) */
} cpu_map SEC(".maps");

/*
 * xsk_map: XSKMAP 타입 맵
 *
 * XDP에서 AF_XDP 소켓으로 패킷을 redirect할 때 사용한다.
 * key는 RX queue index, value는 AF_XDP 소켓의 파일 디스크립터이다.
 *
 * XSKMAP 자체는 패킷 데이터를 저장하지 않는다.
 * 특정 key(RX queue)가 어느 AF_XDP 소켓으로 연결될지를 관리한다.
 *
 * AF_XDP 소켓 등록은 userspace에서 bpf_map_update_elem()으로 수행한다.
 * 현재 loader는 의도적으로 비워 두어, 소켓이 없으면 XDP_PASS로 fallback한다.
 */
struct {
	__uint(type, BPF_MAP_TYPE_XSKMAP);
	__uint(max_entries, 128);   /* 최대 128개 RX queue 지원 */
	__type(key, __u32);         /* RX queue index */
	__type(value, __u32);       /* AF_XDP 소켓 파일 디스크립터 (userspace에서 등록) */
} xsk_map SEC(".maps");

/*
 * stats: Per-CPU 통계 배열 맵
 *
 * BPF_MAP_TYPE_PERCPU_ARRAY를 사용하므로 각 CPU가 자신의 카운터를
 * 별도로 유지한다. lock 없이 동시 업데이트가 가능하다.
 * userspace에서 전체 CPU의 값을 합산하여 통계를 확인한다.
 *
 * key: stat_id enum (0~3)
 * value: 해당 통계의 누적 카운트
 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 4);     /* STAT_PASS, STAT_CPUMAP, STAT_XSK, STAT_TOTAL */
	__type(key, __u32);
	__type(value, __u64);
} stats SEC(".maps");

/* --------------------------------------------------------------------------
 * 헬퍼 함수
 * -------------------------------------------------------------------------- */

/*
 * count_stat - per-CPU 통계 카운터를 1 증가시킨다.
 *
 * @id: stat_id enum 값 (어떤 통계를 증가시킬지)
 *
 * bpf_map_lookup_elem()이 NULL을 반환할 수 있으므로 (verifier 요구사항)
 * 반드시 NULL 체크 후 접근해야 한다.
 */
static __always_inline void count_stat(__u32 id)
{
	__u64 *value = bpf_map_lookup_elem(&stats, &id);
	if (value)
		(*value)++;
}

/* --------------------------------------------------------------------------
 * XDP 메인 프로그램
 * -------------------------------------------------------------------------- */

/*
 * xdp_dispatch - XDP 패킷 분류 및 경로 결정 함수
 *
 * SEC("xdp")로 지정하여 이 함수가 XDP 프로그램임을 BPF 로더에 알린다.
 * NIC에서 패킷이 수신될 때마다 커널이 이 함수를 호출한다.
 *
 * @ctx: XDP 메타데이터 (패킷 데이터 시작/끝 포인터, RX queue index 등)
 *
 * 반환값:
 *   XDP_PASS     — 일반 Linux 네트워크 스택으로 전달
 *   XDP_REDIRECT — bpf_redirect_map()에 의해 CPUMAP 또는 XSKMAP으로 redirect
 */
SEC("xdp")
int xdp_dispatch(struct xdp_md *ctx)
{
	/* ── 패킷 데이터 경계 가져오기 ──
	 *
	 * XDP context에서 패킷의 시작(data)과 끝(data_end)을 가져온다.
	 * 모든 헤더 접근 전에 반드시 data_end 범위 검사를 해야 한다.
	 * (BPF verifier가 패킷 메모리 접근 범위를 정적으로 검증함) */
	void *data     = (void *)(long)ctx->data;
	void *data_end = (void *)(long)ctx->data_end;

	/* ── Ethernet 헤더 파싱 ── */
	struct ethhdr *eth = data;
	__u32 key;

	/* 전체 수신 패킷 수 카운트 (경로 결정 전에 먼저 기록) */
	count_stat(STAT_TOTAL);

	/* Ethernet 헤더가 패킷 범위 안에 있는지 검사 */
	if ((void *)(eth + 1) > data_end)
		goto pass;

	/* IPv4가 아닌 패킷은 분류 대상이 아니므로 그대로 통과 */
	if (eth->h_proto != bpf_htons(ETH_P_IP))
		goto pass;

	/* ── IPv4 헤더 파싱 ── */
	struct iphdr *iph = (void *)(eth + 1);

	/* IP 헤더 범위 검사 + 최소 IHL 검증(5 = 20바이트) + UDP 프로토콜 확인 */
	if ((void *)(iph + 1) > data_end || iph->ihl < 5 || iph->protocol != IPPROTO_UDP)
		goto pass;

	/* IP 헤더의 실제 길이 계산 (IHL 필드 × 4바이트)
	 * IP 옵션이 있을 수 있으므로 고정 20바이트가 아닌 IHL을 사용한다. */
	__u32 ihl_len = (__u32)iph->ihl * 4;
	if ((void *)iph + ihl_len > data_end)
		goto pass;

	/* ── UDP 헤더 파싱 ──
	 * UDP 헤더는 IP 헤더 바로 뒤에 위치한다 (IP 헤더 길이만큼 오프셋). */
	struct udphdr *udp = (void *)iph + ihl_len;
	if ((void *)(udp + 1) > data_end)
		goto pass;

	/* ── 분류 및 경로 결정 ──
	 *
	 * UDP destination port를 기준으로 패킷의 처리 경로를 선택한다.
	 * 이것은 패킷 경로 메커니즘을 먼저 검증하기 위한 임시 분류 규칙이다. */

	/* 경로 1: UDP dst 9001 → CPUMAP을 통해 RT CPU(CPU 3)로 redirect */
	if (udp->dest == bpf_htons(RT_PORT)) {
		key = RT_CPU;
		/*
		 * bpf_redirect_map()의 3번째 인자(XDP_PASS)는 fallback action이다.
		 * CPUMAP에 해당 CPU 엔트리가 없으면 XDP_PASS로 안전하게 처리된다.
		 *
		 * 반환값이 XDP_REDIRECT이면 redirect 성공 → 통계 기록 후 반환.
		 * 그렇지 않으면 CPUMAP이 아직 설정되지 않은 것이므로 pass로 이동.
		 */
		int action = bpf_redirect_map(&cpu_map, key, XDP_PASS);
		if (action == XDP_REDIRECT) {
			count_stat(STAT_CPUMAP);
			return action;
		}
		goto pass; /* userspace가 CPUMAP[3]을 설정하기 전까지 안전하게 fallback */
	}

	/* 경로 2: UDP dst 9002 → XSKMAP을 통해 AF_XDP 소켓으로 redirect */
	if (udp->dest == bpf_htons(XSK_PORT)) {
		key = ctx->rx_queue_index;   /* XSKMAP의 key는 RX queue index를 사용 */
		/*
		 * AF_XDP 소켓이 해당 RX queue에 등록되어 있으면 redirect 성공.
		 * 등록되지 않았으면 fallback(XDP_PASS)으로 일반 경로 처리.
		 */
		int action = bpf_redirect_map(&xsk_map, key, XDP_PASS);
		if (action == XDP_REDIRECT) {
			count_stat(STAT_XSK);
			return action;
		}
	}

pass:
	count_stat(STAT_PASS);
	return XDP_PASS;
}

char LICENSE[] SEC("license") = "GPL";
