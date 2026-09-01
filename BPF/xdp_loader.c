// SPDX-License-Identifier: GPL-2.0
//
// xdp_loader.c — XDP userspace 로더 (Generic/SKB 모드)
//
// libbpf skeleton으로 BPF object를 로드하고, map을 bpffs에 pin한 뒤
// 프로세스가 살아 있는 동안 XDP program을 유지한다.
// 다음 순서로 동작한다.
//   1) 인터페이스 확인 및 BPF object 로드
//   2) CPUMAP[3] 설정
//   3) Generic XDP attach 및 map pin
//   4) per-CPU 통계 출력
//   5) 종료 시 XDP detach, map unpin 및 리소스 정리
//
// 다른 userspace consumer(예: afxdp_recv)는 pin된 map을 bpf_obj_get()으로
// 열어 사용한다. 따라서 consumer는 BPF object를 다시 load/attach하지 않는다.

#include <bpf/bpf.h>            /* map 조작 및 XDP attach/detach API */
#include <bpf/libbpf.h>         /* skeleton과 libbpf API */
#include <linux/if_link.h>      /* XDP_FLAGS_DRV_MODE */
#include <errno.h>
#include <net/if.h>             /* if_nametoindex() */
#include <signal.h>             /* signal(), SIGINT, SIGTERM */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <string.h>
#include <unistd.h>             /* sleep() */

/* bpftool이 생성한 skeleton: object open/load와 map/program 핸들을 제공한다. */
#include "xdp_kern.skel.h"
#include "config.h"

/* ===== 전역 상태 ===== */

/* SIGINT/SIGTERM을 받으면 루프를 끝내도록 설정한다. */
static volatile sig_atomic_t stop;

/* attach와 동일한 인터페이스에서 detach하기 위해 보관한다. */
static int g_ifindex;

/* 시그널 핸들러에서는 async-signal-safe한 플래그 변경만 수행한다. */
static void on_signal(int signo) { (void)signo; stop = 1; }

/* ===== map 설정 및 pin ===== */

/*
 * CPUMAP의 key(cpu)에 queue size를 등록한다.
 *
 * XDP program이 redirect를 수행하기 전에 엔트리가 필요하며, 없으면
 * 커널에서 fallback action(XDP_PASS)이 선택된다.
 */
static int configure_cpu_map(int fd, __u32 cpu, __u32 queue_size)
{
	return bpf_map_update_elem(fd, &cpu, &queue_size, BPF_ANY);
}

/* map을 pin할 디렉터리가 bpffs 위에 존재하는지 확인한다. */
static int ensure_pin_dir(void)
{
	struct stat st;

	if (mkdir(ACE_XDP_PIN_DIR, 0755) && errno != EEXIST)
		return -errno;
	if (stat(ACE_XDP_PIN_DIR, &st))
		return -errno;
	if (!S_ISDIR(st.st_mode))
		return -ENOTDIR;
	return 0;
}

/* skeleton의 모든 map(cpu_map, xsk_map, stats)을 ACE_XDP_PIN_DIR에 노출한다. */
static int pin_maps(struct xdp_kern *skel)
{
	int err = ensure_pin_dir();

	if (err)
		return err;
	return bpf_object__pin_maps(skel->obj, ACE_XDP_PIN_DIR);
}

/* loader가 소유한 map pin을 제거한다. */
static void unpin_maps(struct xdp_kern *skel)
{
	if (skel)
		bpf_object__unpin_maps(skel->obj, ACE_XDP_PIN_DIR);
	rmdir(ACE_XDP_PIN_DIR);
}

/* ===== 통계 출력 ===== */

/*
 * Per-CPU stats map의 값을 CPU별로 읽어 합산한다.
 *
 * lookup 결과는 possible CPU 수만큼의 __u64 배열이다. 출력 순서는
 * ace_xdp_stat_id 정의 순서를 따른다.
 */
static void print_stats(int fd)
{
	int ncpu = libbpf_num_possible_cpus();
	if (ncpu < 1) return;

	/* lookup 결과를 받을 per-CPU 배열 */
	__u64 *values = calloc((size_t)ncpu, sizeof(*values));
	if (!values) return;

	/* 각 stat_id에 대해 모든 CPU의 counter를 합산 */
	for (__u32 id = 0; id < (unsigned int)ACE_XDP_STAT_COUNT; id++) {
		__u64 total = 0;
		if (bpf_map_lookup_elem(fd, &id, values) == 0)
			for (int cpu = 0; cpu < ncpu; cpu++) total += values[cpu];
		printf("stat[%u]=%llu%s", id, (unsigned long long)total,
			id == (unsigned int)(ACE_XDP_STAT_COUNT - 1) ? "\n" : " ");
	}
	free(values);
}

/* ===== 실행 흐름 ===== */

int main(int argc, char **argv)
{
	/* 첫 번째 인자는 인터페이스 이름이며, 생략하면 eth0을 사용한다. */
	const char *ifname = argc > 1 ? argv[1] : ACE_XDP_DEFAULT_IFNAME;

	struct xdp_kern *skel = NULL;   /* BPF skeleton */
	int prog_fd = -1;               /* attach할 XDP program FD */
	int err = 1;
	bool attached = false;          /* detach 필요 여부 */
	bool maps_pinned = false;        /* consumer가 열 수 있는 map pin 존재 여부 */

	/* 1. 인터페이스 이름을 XDP API가 사용하는 index로 변환한다. */
	g_ifindex = if_nametoindex(ifname);
	if (!g_ifindex) {
		fprintf(stderr, "interface %s: %s\n", ifname, strerror(errno));
		return 1;
	}

	/* libbpf 동작을 엄격 모드로 설정한다. */
	libbpf_set_strict_mode(LIBBPF_STRICT_ALL);

	/* 기본 libbpf 로그는 끈다. 오류 메시지는 아래에서 직접 출력한다. */
	libbpf_set_print(NULL);

	/* 2. BPF object를 열고 program/map을 커널에 로드한다. */
	skel = xdp_kern__open_and_load();
	if (!skel) {
		fprintf(stderr, "failed to open/load BPF object\n");
		return 1;
	}

	/* 3. xdp_kern.c의 UDP/9001 경로가 사용할 CPUMAP[3]을 등록한다. */
	if (configure_cpu_map(bpf_map__fd(skel->maps.cpu_map), ACE_XDP_RT_CPU,
		ACE_XDP_CPU_MAP_QUEUE_SIZE)) {
		fprintf(stderr, "CPUMAP[%d] setup failed: %s\n", ACE_XDP_RT_CPU,
			strerror(errno));
		goto out;
	}

	/* 4. XDP program을 Generic(SKB) 모드로 attach한다.
	 *
	 * Generic mode는 SKB 생성 이후 실행되므로 native mode보다 느릴 수 있지만,
	 * NIC driver의 native XDP 지원 여부와 관계없이 사용할 수 있다. CAP_NET_ADMIN
	 * 권한(일반적으로 root)이 필요하다. */
	prog_fd = bpf_program__fd(skel->progs.xdp_dispatch);
	if (prog_fd < 0) {
		fprintf(stderr, "failed to get XDP program fd\n");
		goto out;
	}

	err = bpf_xdp_attach(g_ifindex, prog_fd, XDP_FLAGS_DRV_MODE, NULL);
	if (err) {
		fprintf(stderr, "XDP attach (generic) failed on %s: %s\n",
			ifname, strerror(errno));
		goto out;
	}
	attached = true;

	/*
	 * XDP가 실제로 attach된 뒤 map을 pin한다. 이 순서로 consumer가 map을
	 * 발견하는 시점에는 이미 packet path가 준비되어 있다.
	 */
	err = pin_maps(skel);
	if (err) {
		fprintf(stderr, "BPF map pin failed at %s: %s\n", ACE_XDP_PIN_DIR,
			strerror(-err));
		goto out;
	}
	maps_pinned = true;

	printf("attached xdp_dispatch to %s ; Ctrl-C to stop\n", ifname);

	/* Ctrl-C와 서비스 종료(SIGTERM)를 정상 cleanup으로 연결한다. */
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	/* 5. 1초마다 통계를 출력하고, stop 설정 시 루프를 종료한다. */
	while (!stop) {
		sleep(1);
		print_stats(bpf_map__fd(skel->maps.stats));
	}

	err = 0;

out:
	/* attach가 성공한 경우에만 같은 mode로 detach한 뒤 skeleton을 해제한다. */
	if (attached)
		bpf_xdp_detach(g_ifindex, XDP_FLAGS_DRV_MODE, NULL);
	if (maps_pinned)
		unpin_maps(skel);
	xdp_kern__destroy(skel);
	return err < 0 ? 1 : err;
}
