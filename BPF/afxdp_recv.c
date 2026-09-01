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

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <linux/if_ether.h>
#include <linux/if_link.h>
#include <linux/if_xdp.h>
#include <xdp/xsk.h>            /* AF_XDP socket/ring API (libxdp) */

#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <netinet/ip.h>
#include <netinet/udp.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "config.h"

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

/* ===== 종료 처리 ===== */

static void on_signal(int signo)
{
	(void)signo;
	stop = 1;
}

/* ===== AF_XDP 자료구조 ===== */

/* UMEM과 그에 연결된 FILL/COMPLETION ring. 수신만 하므로 completion은
 * 생성만 하고 사용하지 않는다(TX packet을 보낼 때 필요). */
struct umem_info {
	void *buffer;
	struct xsk_umem *umem;
	struct xsk_ring_prod fill;
	struct xsk_ring_cons completion;
};

/* AF_XDP socket과 RX/TX ring. 이 예제에서는 RX만 사용한다. */
struct xsk_info {
	struct xsk_socket *socket;
	struct xsk_ring_cons rx;
	struct xsk_ring_prod tx;
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
	err = posix_memalign(&umem->buffer, getpagesize(), UMEM_SIZE);
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
	int i;

	if (xsk_ring_prod__reserve(&umem->fill, NUM_FRAMES, &idx) != NUM_FRAMES)
		return -ENOSPC;
	for (i = 0; i < NUM_FRAMES; i++)
		*xsk_ring_prod__fill_addr(&umem->fill, idx + i) =
			(__u64)i * FRAME_SIZE;
	xsk_ring_prod__submit(&umem->fill, NUM_FRAMES);
	return 0;
}

static int create_socket(struct xsk_info *xsk, struct umem_info *umem,
		const char *ifname, __u32 queue_id, int xsk_map_fd)
{
	struct xsk_socket_config config = {
		.rx_size = RING_SIZE,
		.tx_size = RING_SIZE,
		.libbpf_flags = XSK_LIBBPF_FLAGS__INHIBIT_PROG_LOAD,
		.xdp_flags = XDP_FLAGS_SKB_MODE,
		.bind_flags = XDP_COPY,
	};
	int err;

	/* XDP_FLAGS_SKB_MODE와 XDP_COPY는 Generic XDP 환경에서의 기본 조합이다. */
	err = xsk_socket__create(&xsk->socket, ifname, queue_id, umem->umem,
		&xsk->rx, &xsk->tx, &config);
	if (err)
		return err;

	/* 이 호출이 XSKMAP[queue_id] = AF_XDP socket FD를 수행한다. */
	err = xsk_socket__update_xskmap(xsk->socket, xsk_map_fd);
	if (err) {
		xsk_socket__delete(xsk->socket);
		xsk->socket = NULL;
	}
	return err;
}

/* ===== 수신 packet 처리 ===== */

static bool packet_udp_port(const void *packet, __u32 length, __u16 *port)
{
	const struct ethhdr *eth = packet;
	const struct iphdr *ip;
	const struct udphdr *udp;
	__u32 ip_header_len;

	if (length < sizeof(*eth) || ntohs(eth->h_proto) != ETH_P_IP)
		return false;
	if (length < sizeof(*eth) + sizeof(*ip))
		return false;

	ip = (const struct iphdr *)(eth + 1);
	if (ip->version != 4 || ip->ihl < 5 || ip->protocol != IPPROTO_UDP)
		return false;
	ip_header_len = (__u32)ip->ihl * 4;
	if (length < sizeof(*eth) + ip_header_len + sizeof(*udp))
		return false;

	udp = (const struct udphdr *)((const unsigned char *)ip + ip_header_len);
	*port = ntohs(udp->dest);
	return true;
}

static unsigned int receive_batch(struct xsk_info *xsk, struct umem_info *umem)
{
	__u32 rx_index, fill_index;
	__u64 addresses[RX_BATCH_SIZE];
	unsigned int received, i, recycled = 0;

	received = xsk_ring_cons__peek(&xsk->rx, RX_BATCH_SIZE, &rx_index);
	if (!received)
		return 0;

	for (i = 0; i < received; i++) {
		const struct xdp_desc *desc = xsk_ring_cons__rx_desc(&xsk->rx, rx_index + i);
		__u64 addr = desc->addr;
		__u16 port;

		addresses[i] = addr;
		received_packets++;
		/* 커널이 준 address/length를 UMEM 범위 안에서만 사용한다. */
		if (addr < UMEM_SIZE && desc->len <= FRAME_SIZE &&
			addr + desc->len <= UMEM_SIZE) {
			const void *packet = (const unsigned char *)umem->buffer + addr;
			if (packet_udp_port(packet, desc->len, &port))
				printf("RX packet length=%u dst_port=%u\n", desc->len, port);
			else
				printf("RX packet length=%u (non-IPv4/UDP)\n", desc->len);
		}
	}
	xsk_ring_cons__release(&xsk->rx, received);

	/* 처리한 frame을 다시 FILL ring에 넣어 다음 packet을 받을 수 있게 한다. */
	if (xsk_ring_prod__reserve(&umem->fill, received, &fill_index) == received) {
		for (i = 0; i < received; i++) {
			*xsk_ring_prod__fill_addr(&umem->fill, fill_index + i) = addresses[i];
		}
		xsk_ring_prod__submit(&umem->fill, received);
		recycled = received;
	}

	return recycled;
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

/* ===== 프로그램 진입점 ===== */

int main(int argc, char **argv)
{
	const char *ifname = argc > 1 ? argv[1] : ACE_XDP_DEFAULT_IFNAME;
	__u32 queue_id = argc > 2 ? (unsigned int)strtoul(argv[2], NULL, 10) :
		ACE_XSK_DEFAULT_QUEUE;
	struct umem_info umem = {0};
	struct xsk_info xsk = {0};
	struct pollfd pollfd;
	int xsk_map_fd = -1, stats_fd = -1, err = 1;

	if (!if_nametoindex(ifname)) {
		fprintf(stderr, "interface %s: %s\n", ifname, strerror(errno));
		return 1;
	}
	if (queue_id >= (unsigned int)ACE_XDP_XSK_MAP_MAX_ENTRIES) {
		fprintf(stderr, "RX queue %u is outside xsk_map range\n", queue_id);
		return 1;
	}

	libbpf_set_strict_mode(LIBBPF_STRICT_ALL);
	libbpf_set_print(NULL);

	/* BPF loader가 pin한 map만 열고, 이 프로세스에서는 BPF object를 다시
	 * load하거나 XDP를 attach하지 않는다. */
	xsk_map_fd = bpf_obj_get(ACE_XDP_XSK_MAP_PIN);
	if (xsk_map_fd < 0) {
		fprintf(stderr, "cannot open pinned xsk_map (%s): %s\n",
			ACE_XDP_XSK_MAP_PIN, strerror(errno));
		goto out;
	}
	stats_fd = bpf_obj_get(ACE_XDP_STATS_MAP_PIN);
	if (stats_fd < 0) {
		fprintf(stderr, "cannot open pinned stats map (%s): %s\n",
			ACE_XDP_STATS_MAP_PIN, strerror(errno));
		goto out;
	}

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
	err = create_socket(&xsk, &umem, ifname, queue_id,
		xsk_map_fd);
	if (err) {
		fprintf(stderr, "AF_XDP socket/XSKMAP setup failed: %s\n", strerror(-err));
		goto out;
	}

	pollfd.fd = xsk_socket__fd(xsk.socket);
	pollfd.events = POLLIN;
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	printf("AF_XDP ready: %s RX queue %u, XSKMAP[%u] registered; Ctrl-C to stop\n",
		ifname, queue_id, queue_id);
	while (!stop) {
		int poll_result = poll(&pollfd, 1, 1000);
		if (poll_result < 0) {
			if (errno == EINTR)
				continue;
			perror("poll");
			break;
		}
		if (poll_result > 0 && (pollfd.revents & POLLIN))
			receive_batch(&xsk, &umem);
	}
	printf("received packets=%llu\n", received_packets);
	print_stats(stats_fd);
	err = 0;

out:
	if (xsk.socket) {
		/* socket 종료 전에 map entry를 제거해 다음 receiver가 같은 queue를 사용할 수 있게 한다. */
		bpf_map_delete_elem(xsk_map_fd, &queue_id);
		xsk_socket__delete(xsk.socket);
	}
	if (umem.umem)
		xsk_umem__delete(umem.umem);
	free(umem.buffer);
	if (stats_fd >= 0)
		close(stats_fd);
	if (xsk_map_fd >= 0)
		close(xsk_map_fd);
	return err < 0 ? 1 : err;
}
