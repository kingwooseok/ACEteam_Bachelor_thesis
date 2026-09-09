#define _GNU_SOURCE

/**
 * @file phc_calibrate.c
 * @brief PTP PHC와 CLOCK_MONOTONIC 사이 offset을 측정함.
 *
 * PTP_SYS_OFFSET_EXTENDED의 각 [system-before, PHC, system-after] 표본에서
 * system midpoint와 PHC의 차이를 계산한다. 같은 ioctl batch에서 system span이
 * 가장 짧은 표본을 대표값으로 선택한다.
 *
 * NIC의 HW RX timestamp는 PHC 기준이고 BPF/user 수신 시각은 MONOTONIC 기준이다.
 * 둘 다 ns 단위여도 시작점과 진행 속도가 다르므로 바로 빼서는 안 된다.
 * 여기서 offset = PHC - MONOTONIC을 구해, 분석기가 HW 시각을 MONOTONIC 기준으로
 * 환산할 수 있게 한다. 이 프로그램은 시계 자체를 조정하거나 PTP 동기화를 하지 않는다.
 * run 전후 표본의 offset 변화를 분석기가 보간하며, CSV에는 원시 표본도 함께 남긴다.
 */

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

struct config {
	const char *device;
	const char *interface;
	const char *output;
	unsigned int samples;
	unsigned int iterations;
	bool device_explicit;
};

struct result {
	int64_t before_ns;
	int64_t phc_ns;
	int64_t after_ns;
	int64_t span_ns;
	int64_t offset_ns;
	bool valid;
};

static void usage(const char *program)
{
	fprintf(stderr,
		"Usage: %s [options]\n"
		"  -d, --device PATH       PHC device (default: /dev/ptp0)\n"
		"  -I, --interface IFACE   discover the PHC associated with IFACE\n"
		"  -s, --samples COUNT     samples per ioctl, 1-%u (default: %u)\n"
		"  -n, --iterations COUNT ioctl batches (default: 1)\n"
		"  -o, --output PATH       CSV output (default: stdout)\n",
		program, PTP_MAX_SAMPLES, PTP_MAX_SAMPLES);
}

static int parse_unsigned(const char *text, unsigned int minimum,
		unsigned int maximum, unsigned int *value)
{
	char *end;
	unsigned long parsed;

	if (!text || !text[0])
		return -1;
	for (const unsigned char *cursor = (const unsigned char *)text;
	     *cursor; cursor++)
		if (*cursor < '0' || *cursor > '9')
			return -1;
	errno = 0;
	parsed = strtoul(text, &end, 10);
	if (errno || text == end || *end != '\0' || parsed < minimum ||
	    parsed > maximum)
		return -1;
	*value = (unsigned int)parsed;
	return 0;
}

static int parse_arguments(int argc, char **argv, struct config *config)
{
	static const struct option options[] = {
		{"device", required_argument, NULL, 'd'},
		{"interface", required_argument, NULL, 'I'},
		{"samples", required_argument, NULL, 's'},
		{"iterations", required_argument, NULL, 'n'},
		{"output", required_argument, NULL, 'o'},
		{"help", no_argument, NULL, 'h'},
		{NULL, 0, NULL, 0},
	};
	int option;

	while ((option = getopt_long(argc, argv, "d:I:s:n:o:h", options, NULL)) != -1) {
		switch (option) {
		case 'd':
			config->device = optarg;
			config->device_explicit = true;
			break;
		case 'I':
			if (!optarg[0] || strlen(optarg) >= IFNAMSIZ)
				return -1;
			config->interface = optarg;
			break;
		case 's':
			if (parse_unsigned(optarg, 1, PTP_MAX_SAMPLES,
				&config->samples))
				return -1;
			break;
		case 'n':
			if (parse_unsigned(optarg, 1, 1000000, &config->iterations))
				return -1;
			break;
		case 'o':
			config->output = optarg;
			break;
		case 'h':
			usage(argv[0]);
			exit(0);
		default:
			return -1;
		}
	}

	if (config->device_explicit && config->interface)
		return -1;
	return optind == argc ? 0 : -1;
}

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

int main(int argc, char **argv)
{
	struct config config = {
		.device = "/dev/ptp0",
		.samples = PTP_MAX_SAMPLES,
		.iterations = 1,
	};
	FILE *output = stdout;
	char discovered_device[64];
	int fd = -1;
	int output_fd = -1;
	int exit_status = 1;

	if (parse_arguments(argc, argv, &config)) {
		usage(argv[0]);
		return 2;
	}
	if (config.interface) {
		if (phc_device_for_interface(config.interface, discovered_device,
			sizeof(discovered_device))) {
			perror("discover interface PHC");
			return 1;
		}
		config.device = discovered_device;
	}
	fd = open(config.device, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		perror("open PHC device");
		return 1;
	}
	if (config.output) {
		output_fd = open(config.output,
			O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
		if (output_fd < 0) {
			perror("open output");
			goto out;
		}
		output = fdopen(output_fd, "w");
		if (!output) {
			int saved_errno = errno;

			close(output_fd);
			output_fd = -1;
			errno = saved_errno;
			perror("fdopen output");
			goto out;
		}
		output_fd = -1; /* owned by output */
	}

	fprintf(output, "# device=%s clock=CLOCK_MONOTONIC offset=phc-minus-monotonic\n",
		config.device);
	fprintf(output,
		"iteration,sample,system_before_ns,phc_ns,system_after_ns,"
		"span_ns,offset_ns,selected\n");

	for (unsigned int iteration = 0; iteration < config.iterations; iteration++) {
		struct result results[PTP_MAX_SAMPLES] = {0};
		unsigned int best_index = 0;

		if (collect_batch(fd, config.samples, results, &best_index)) {
			perror("PTP_SYS_OFFSET_EXTENDED(CLOCK_MONOTONIC)");
			goto out;
		}
		/*
		 * 대표값만 저장하지 않고, 모든 유효 표본과 selected 표시를 남긴다.
		 * 원시 before/after/span을 보면 calibration 당시 읽기 지연도 확인할 수 있다.
		 * iterations는 연속 batch 횟수이며 run 전후 시점 선택은 실행 script가 맡는다.
		 */
		for (unsigned int sample = 0; sample < config.samples; sample++) {
			const struct result *result = &results[sample];

			if (!result->valid)
				continue;
			fprintf(output,
				"%u,%u,%" PRId64 ",%" PRId64 ",%" PRId64
				",%" PRId64 ",%" PRId64 ",%u\n",
				iteration, sample, result->before_ns, result->phc_ns,
				result->after_ns, result->span_ns, result->offset_ns,
				sample == best_index);
		}
	}

	if (ferror(output)) {
		errno = EIO;
		perror("write output");
		goto out;
	}
	if (fflush(output)) {
		perror("flush output");
		goto out;
	}
	if (output != stdout && fsync(fileno(output))) {
		perror("fsync output");
		goto out;
	}
	exit_status = 0;

out:
	if (output_fd >= 0)
		close(output_fd);
	if (output != stdout && fclose(output)) {
		perror("close output");
		exit_status = 1;
	}
	close(fd);
	return exit_status;
}
