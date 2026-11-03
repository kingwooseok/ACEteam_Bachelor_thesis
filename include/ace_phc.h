#ifndef ACE_PHC_H
#define ACE_PHC_H
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <linux/ethtool.h>
#include <linux/ptp_clock.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

struct result {
	int64_t before_ns;
	int64_t phc_ns;
	int64_t after_ns;
	int64_t span_ns;
	int64_t offset_ns;
	bool valid;
};

/*
 * /dev/ptp0이 항상 실험 NIC의 시계라는 보장은 없으므로 -I 사용 시 드라이버가
 * 알려주는 phc_index를 조회한다. RX timestamp와 같은 NIC의 PHC를 보정해야
 * 이후의 HW→XDP/user 지연 계산에 이 offset을 사용할 수 있다.
 */
static int phc_device_for_interface(const char *interface, char *path,
		size_t path_size)
{
	struct ethtool_ts_info info = {
		.cmd = ETHTOOL_GET_TS_INFO,
	};
	struct ifreq request = {0};
	int socket_fd;
	int length;
	int saved_errno;

	socket_fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (socket_fd < 0)
		return -1;
	memcpy(request.ifr_name, interface, strlen(interface) + 1);
	request.ifr_data = (void *)&info;
	if (ioctl(socket_fd, SIOCETHTOOL, &request)) {
		saved_errno = errno;
		close(socket_fd);
		errno = saved_errno;
		return -1;
	}
	close(socket_fd);
	if (info.phc_index < 0) {
		errno = ENODEV;
		return -1;
	}
	length = snprintf(path, path_size, "/dev/ptp%d", info.phc_index);
	if (length < 0 || (size_t)length >= path_size) {
		errno = ENAMETOOLONG;
		return -1;
	}
	return 0;
}

static bool ptp_time_to_ns(const struct ptp_clock_time *time, int64_t *ns)
{
	if (time->sec < 0 || time->nsec >= 1000000000U ||
	    (uint64_t)time->sec > (uint64_t)INT64_MAX / 1000000000ULL)
		return false;
	*ns = time->sec * 1000000000LL + time->nsec;
	return true;
}

/*
 * 한 표본은 MONO_before → PHC → MONO_after 순서로 읽은 세 시각이다.
 * PHC를 읽은 정확한 MONOTONIC 시각을 모르므로 before/after의 중간을 대응시킨다.
 * span이 짧을수록 이 대응 시각의 불확실성이 작다고 보고 batch 안의 최소 span을
 * 고른다. midpoint는 근사이며, 최소 span 선택이 시계 오차를 완전히 없애지는 않는다.
 */
static int collect_batch(int fd, unsigned int count, struct result *results,
		unsigned int *best_index)
{
	struct ptp_sys_offset_extended request = {
		.n_samples = count,
		/* 분석할 BPF/user 시계와 맞춘다. 기본 REALTIME을 쓰는 ioctl과 구별한다. */
		.clockid = CLOCK_MONOTONIC,
	};
	int64_t best_span = INT64_MAX;
	unsigned int valid_count = 0;

	if (ioctl(fd, PTP_SYS_OFFSET_EXTENDED, &request))
		return -1;

	for (unsigned int i = 0; i < request.n_samples; i++) {
		struct result *result = &results[i];

		if (!ptp_time_to_ns(&request.ts[i][0], &result->before_ns) ||
		    !ptp_time_to_ns(&request.ts[i][1], &result->phc_ns) ||
		    !ptp_time_to_ns(&request.ts[i][2], &result->after_ns) ||
		    result->after_ns < result->before_ns)
			continue;

		/*
		 * CSV의 offset 부호는 PHC - MONOTONIC으로 통일한다.
		 * 따라서 분석 시 hw_mono = hw_phc - offset이며, user_mono와의 차이가
		 * HW→user 지연이 된다. offset 자체는 packet 처리 지연이 아니다.
		 */
		result->span_ns = result->after_ns - result->before_ns;
		result->offset_ns = result->phc_ns -
			(result->before_ns + result->span_ns / 2);
		result->valid = true;
		valid_count++;
		if (result->span_ns < best_span) {
			best_span = result->span_ns;
			*best_index = i;
		}
	}

	if (!valid_count) {
		errno = ERANGE;
		return -1;
	}
	return 0;
}

#endif
