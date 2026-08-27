// SPDX-License-Identifier: GPL-2.0
//
// xdp_loader.c — XDP 프로그램 Userspace 로더 (Generic/SKB 모드)
//
// libbpf skeleton을 사용하여 xdp_kern.o BPF 프로그램을 로드하고
// 지정된 네트워크 인터페이스(기본값: eth0)에 Generic XDP로 attach한다.
//
// Generic XDP(SKB 모드)는 드라이버의 native XDP 지원 여부와 관계없이
// 모든 NIC에서 동작한다. 패킷이 SKB로 변환된 이후에 XDP 프로그램이
// 실행되므로 native 모드보다 약간 느리지만, 드라이버 호환성 문제가 없다.
//
// 실행 흐름:
//   1) BPF object 열기 및 커널에 로드
//   2) CPUMAP[3] 엔트리 설정 (큐 크기 256)
//   3) XDP 프로그램을 NIC에 generic 모드로 attach
//   4) 1초마다 per-CPU 통계를 합산하여 출력
//   5) Ctrl-C(SIGINT) 또는 SIGTERM 수신 시 detach 후 종료
//
// 빌드 전에 bpftool gen skeleton으로 xdp_kern.skel.h가 생성되어 있어야 한다.
// (Makefile이 자동으로 처리함)

#include <bpf/bpf.h>            /* bpf_map_update_elem(), bpf_xdp_attach/detach() 등 */
#include <bpf/libbpf.h>         /* libbpf API: open, load, destroy 등 */
#include <linux/if_link.h>      /* XDP_FLAGS_SKB_MODE (generic XDP 플래그) */
#include <errno.h>
#include <net/if.h>             /* if_nametoindex(): 인터페이스 이름 → index 변환 */
#include <signal.h>             /* signal(), SIGINT, SIGTERM */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>             /* sleep() */

/*
 * xdp_kern.skel.h — bpftool이 xdp_kern.o에서 자동 생성한 skeleton 헤더.
 *
 * 이 헤더는 다음을 제공한다:
 *   - struct xdp_kern: BPF object 구조체 (maps, progs 멤버 포함)
 *   - xdp_kern__open_and_load(): BPF object를 열고 커널에 로드
 *   - xdp_kern__destroy(): BPF object 및 관련 리소스 해제
 *
 * 빌드 시점에 생성되므로 IDE에서 "파일 없음" 경고가 뜰 수 있지만,
 * make 실행 후에는 정상적으로 해결된다.
 */
#include "xdp_kern.skel.h"

/* --------------------------------------------------------------------------
 * 전역 상태
 * -------------------------------------------------------------------------- */

/* 메인 루프 종료 플래그. SIGINT/SIGTERM 수신 시 1로 설정된다. */
static volatile sig_atomic_t stop;

/* XDP detach에 필요한 인터페이스 인덱스 (시그널 핸들러에서 직접 사용하진 않지만
 * cleanup 시점에 접근 가능하도록 전역으로 유지) */
static int g_ifindex;

/* 시그널 핸들러: stop 플래그를 설정하여 메인 루프를 종료시킨다. */
static void on_signal(int signo) { (void)signo; stop = 1; }

/* --------------------------------------------------------------------------
 * CPUMAP 설정 함수
 * -------------------------------------------------------------------------- */

/*
 * configure_cpu_map - CPUMAP의 특정 CPU 엔트리를 설정한다.
 *
 * @fd:         cpu_map의 파일 디스크립터
 * @cpu:        대상 CPU 번호 (map의 key)
 * @queue_size: 해당 CPU의 CPUMAP 큐 크기 (map의 value)
 *
 * XDP에서 bpf_redirect_map(&cpu_map, cpu, ...)을 호출하기 전에
 * 이 함수로 해당 CPU 엔트리를 미리 설정해야 한다.
 * 설정하지 않으면 redirect가 실패하고 fallback action이 실행된다.
 *
 * 반환값: 성공 시 0, 실패 시 음수 errno
 */
static int configure_cpu_map(int fd, __u32 cpu, __u32 queue_size)
{
	return bpf_map_update_elem(fd, &cpu, &queue_size, BPF_ANY);
}

/* --------------------------------------------------------------------------
 * 통계 출력 함수
 * -------------------------------------------------------------------------- */

/*
 * print_stats - per-CPU 통계 맵의 값을 모든 CPU에 대해 합산하여 출력한다.
 *
 * @fd: stats map의 파일 디스크립터
 *
 * BPF_MAP_TYPE_PERCPU_ARRAY이므로 bpf_map_lookup_elem()은
 * CPU 개수만큼의 __u64 배열을 반환한다. 각 CPU의 값을 합산하면
 * 전체 시스템의 통계가 된다.
 *
 * 출력 형식: stat[0]=PASS stat[1]=CPUMAP stat[2]=XSK stat[3]=TOTAL
 */
static void print_stats(int fd)
{
	int ncpu = libbpf_num_possible_cpus();
	if (ncpu < 1) return;

	/* per-CPU 값을 담을 배열 할당 */
	__u64 *values = calloc((size_t)ncpu, sizeof(*values));
	if (!values) return;

	/* stat_id 0~3 각각에 대해 모든 CPU의 값을 합산 */
	for (__u32 id = 0; id < 4; id++) {
		__u64 total = 0;
		if (bpf_map_lookup_elem(fd, &id, values) == 0)
			for (int cpu = 0; cpu < ncpu; cpu++) total += values[cpu];
		printf("stat[%u]=%llu%s", id, (unsigned long long)total, id == 3 ? "\n" : " ");
	}
	free(values);
}

/* --------------------------------------------------------------------------
 * 메인 함수
 * -------------------------------------------------------------------------- */

int main(int argc, char **argv)
{
	/* 인터페이스 이름: 인자로 지정하지 않으면 기본값 "eth0" 사용 */
	const char *ifname = argc > 1 ? argv[1] : "eth0";

	struct xdp_kern *skel = NULL;   /* BPF skeleton 구조체 */
	int prog_fd = -1;               /* XDP 프로그램 파일 디스크립터 */
	int err = 1;
	bool attached = false;          /* XDP attach 여부 (cleanup 판단용) */

	/* ── 1단계: 인터페이스 이름을 인덱스로 변환 ──
	 * XDP attach에는 인터페이스 이름이 아닌 숫자 인덱스가 필요하다. */
	g_ifindex = if_nametoindex(ifname);
	if (!g_ifindex) {
		fprintf(stderr, "interface %s: %s\n", ifname, strerror(errno));
		return 1;
	}

	/* libbpf 엄격 모드 활성화 (deprecated API 사용 시 에러 발생) */
	libbpf_set_strict_mode(LIBBPF_STRICT_ALL);

	/* libbpf 내부 로그 출력 비활성화 (필요 시 NULL 대신 콜백 함수 지정) */
	libbpf_set_print(NULL);

	/* ── 2단계: BPF object 열기 및 커널에 로드 ──
	 *
	 * xdp_kern__open_and_load()는 skeleton이 제공하는 함수로,
	 * xdp_kern.o를 열고(open), BPF 프로그램과 맵을 커널에 로드(load)한다.
	 * 로드 시점에 BPF verifier가 프로그램의 안전성을 검증한다. */
	skel = xdp_kern__open_and_load();
	if (!skel) {
		fprintf(stderr, "failed to open/load BPF object\n");
		return 1;
	}

	/* ── 3단계: CPUMAP 설정 ──
	 *
	 * CPUMAP[3]에 큐 크기 256을 설정한다.
	 * 이렇게 해야 xdp_kern.c의 bpf_redirect_map(&cpu_map, 3, ...)이
	 * 실제로 CPU 3으로 패킷을 redirect할 수 있다. */
	if (configure_cpu_map(bpf_map__fd(skel->maps.cpu_map), 3, 256)) {
		fprintf(stderr, "CPUMAP[3] setup failed: %s\n", strerror(errno));
		goto out;
	}

	/* ── XSKMAP은 의도적으로 비워 둔다 ──
	 *
	 * AF_XDP 소켓을 사용하려면 userspace에서 소켓을 생성한 뒤
	 * bpf_map_update_elem(xsk_map_fd, &rxq, &xsk_fd, BPF_ANY)로
	 * 해당 RX queue에 소켓을 등록해야 한다.
	 *
	 * 현재는 AF_XDP 경로 테스트 전이므로 비워 두고,
	 * xdp_kern.c에서 XSKMAP redirect 실패 시 XDP_PASS로 fallback한다. */

	/* ── 4단계: XDP 프로그램을 NIC에 Generic(SKB) 모드로 attach ──
	 *
	 * bpf_xdp_attach()에 XDP_FLAGS_SKB_MODE 플래그를 전달하여
	 * generic XDP 모드로 attach한다.
	 *
	 * Generic XDP vs Native XDP:
	 *   - Native: NIC 드라이버가 직접 XDP를 지원해야 함. 최고 성능.
	 *   - Generic: SKB 할당 이후 실행. 모든 NIC에서 동작. 약간 느림.
	 *
	 * 임베디드 보드의 NIC 드라이버가 native XDP를 지원하지 않을 수 있으므로
	 * 호환성을 위해 generic 모드를 사용한다.
	 *
	 * CAP_NET_ADMIN 권한(보통 root)이 필요하다. */
	prog_fd = bpf_program__fd(skel->progs.xdp_dispatch);
	if (prog_fd < 0) {
		fprintf(stderr, "failed to get XDP program fd\n");
		goto out;
	}

	err = bpf_xdp_attach(g_ifindex, prog_fd, XDP_FLAGS_SKB_MODE, NULL);
	if (err) {
		fprintf(stderr, "XDP attach (generic) failed on %s: %s\n",
			ifname, strerror(errno));
		goto out;
	}
	attached = true;

	printf("attached xdp_dispatch to %s (generic/SKB mode); Ctrl-C to stop\n", ifname);

	/* SIGINT(Ctrl-C)와 SIGTERM에 핸들러 등록 */
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	/* ── 5단계: 통계 출력 루프 ──
	 * 1초마다 per-CPU 통계를 합산하여 출력한다.
	 * stop 플래그가 설정되면(시그널 수신) 루프를 빠져나간다. */
	while (!stop) {
		sleep(1);
		print_stats(bpf_map__fd(skel->maps.stats));
	}

	err = 0;

out:
	/* ── 정리 ──
	 *
	 * bpf_xdp_detach(): generic 모드로 attach된 XDP 프로그램을 NIC에서 제거.
	 *   bpf_program__attach_xdp()와 달리 bpf_link 객체가 없으므로
	 *   명시적으로 detach를 호출해야 한다.
	 *
	 * xdp_kern__destroy(): BPF object와 맵 등 모든 리소스 해제. */
	if (attached)
		bpf_xdp_detach(g_ifindex, XDP_FLAGS_SKB_MODE, NULL);
	xdp_kern__destroy(skel);
	return err;
}
