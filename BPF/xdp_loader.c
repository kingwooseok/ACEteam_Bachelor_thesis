// SPDX-License-Identifier: GPL-2.0
//
// Native/driver-mode XDP loader.  The loader owns the program and all pinned
// maps for one run, refuses to replace an existing XDP program, and detaches
// only when the currently attached program is still its own.
//
// 이 파일은 패킷을 직접 처리하는 수신기가 아니라 실험의 준비·종료 담당이다.
// 패킷 분류는 xdp_kern.c, AF_XDP 수신은 afxdp_recv.c에서 수행한다.
//
// 정상 실행 순서:
//   기존 HW timestamp 설정 저장 → BPF load → CPUMAP 설정·map pin
//   → native XDP attach → HW RX timestamp 활성화 → 수신기/송신기 실행
//   → 송신 중단·수신기 종료 → loader 종료 → CPUMAP 잔여 처리 대기
//   → map 기록을 CSV로 저장 → timestamp 설정 복원·pin 해제
// pin은 커널 map에 다른 프로세스도 열 수 있는 bpffs 경로를 붙이는 것이다.
// 디스크 결과 파일은 아니며, 실험 기록의 영구 저장은 종료 시 CSV가 맡는다.

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <linux/bpf.h>
#include <linux/if_link.h>

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <net/if.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "../include/ace_hwtstamp.h"
#include "../include/ace_process.h"
#include "xdp_kern.skel.h"
#include "config.h"
#include "metadata.h"

static volatile sig_atomic_t stop;

static const char *const stat_names[ACE_XDP_STAT_COUNT] = {
	[ACE_XDP_STAT_PASS] = "pass",
	[ACE_XDP_STAT_CPUMAP] = "cpumap_redirect_requested",
	[ACE_XDP_STAT_XSK] = "xsk_redirect_requested",
	[ACE_XDP_STAT_TOTAL] = "total",
	[ACE_XDP_STAT_EXPERIMENT_PASS] = "experiment_pass_requested",
	[ACE_XDP_STAT_IPV4_INVALID] = "ipv4_invalid",
	[ACE_XDP_STAT_IPV4_FRAGMENT] = "ipv4_fragment_passed",
	[ACE_XDP_STAT_PACKET_HEADER_INVALID] = "packet_header_invalid",
	[ACE_XDP_STAT_META_ADJUST_ERROR] = "metadata_adjust_error",
	[ACE_XDP_STAT_META_BOUNDS_ERROR] = "metadata_bounds_error",
	[ACE_XDP_STAT_HWTS_VALID] = "hw_timestamp_valid",
	[ACE_XDP_STAT_HWTS_ERROR] = "hw_timestamp_error",
	[ACE_XDP_STAT_INGRESS_RECORDED] = "ingress_recorded",
	[ACE_XDP_STAT_INGRESS_DUPLICATE] = "ingress_duplicate",
	[ACE_XDP_STAT_INGRESS_UPDATE_ERROR] = "ingress_update_error",
	[ACE_XDP_STAT_CPUMAP_META_INVALID] = "cpumap_metadata_invalid",
	[ACE_XDP_STAT_CPUMAP_RECORDED] = "cpumap_recorded",
	[ACE_XDP_STAT_CPUMAP_DUPLICATE] = "cpumap_duplicate",
	[ACE_XDP_STAT_CPUMAP_UPDATE_ERROR] = "cpumap_update_error",
	[ACE_XDP_STAT_CPUMAP_FALLBACK] = "cpumap_redirect_fallback",
	[ACE_XDP_STAT_XSK_FALLBACK] = "xsk_redirect_fallback",
};

struct loader_options {
	const char *ifname;
	const char *output_dir;
	const char *output_prefix;
	__u32 record_capacity;
	bool quiet;
	bool recover;
	bool cleanup_pins;
};

static void on_signal(int signo)
{
	(void)signo;
	stop = 1;
}

static int install_signal_handlers(void)
{
	struct sigaction action = {
		.sa_handler = on_signal,
	};

	sigemptyset(&action.sa_mask);
	if (sigaction(SIGINT, &action, NULL) ||
	    sigaction(SIGTERM, &action, NULL))
		return -errno;
	return 0;
}

static void usage(const char *program)
{
	fprintf(stderr,
		"usage: %s --output-dir DIR [OPTIONS] [IFACE]\n"
		"  --output-dir, -o DIR  existing directory for end-of-run CSV files\n"
		"  --prefix, -p PREFIX  safe filename prefix (default: none)\n"
		"  --quiet, -q          print counters only at shutdown\n"
		"  --verbose, -v        print counters once per second\n"
		"  --records, -n COUNT set HASH capacity (default %u, max %u)\n"
		"  --recover, -r        export an inactive run from pinned maps\n"
		"  --cleanup-pins, -c   after successful recovery, remove its pins\n",
		program, ACE_XDP_RECORD_DEFAULT_ENTRIES,
		ACE_XDP_RECORD_MAX_ENTRIES);
}

static int parse_u32(const char *text, __u32 min, __u32 max, __u32 *value)
{
	char *end = NULL;
	unsigned long parsed;

	if (!text || !text[0])
		return -EINVAL;
	for (const unsigned char *cursor = (const unsigned char *)text;
	     *cursor; cursor++)
		if (*cursor < '0' || *cursor > '9')
			return -EINVAL;
	errno = 0;
	parsed = strtoul(text, &end, 10);
	if (errno || !end || *end != '\0' || parsed < min || parsed > max)
		return -EINVAL;
	*value = (__u32)parsed;
	return 0;
}

static int parse_options(int argc, char **argv, struct loader_options *options)
{
	static const struct option long_options[] = {
		{ "output-dir", required_argument, NULL, 'o' },
		{ "prefix", required_argument, NULL, 'p' },
		{ "quiet", no_argument, NULL, 'q' },
		{ "verbose", no_argument, NULL, 'v' },
		{ "records", required_argument, NULL, 'n' },
		{ "recover", no_argument, NULL, 'r' },
		{ "cleanup-pins", no_argument, NULL, 'c' },
		{ "help", no_argument, NULL, 'h' },
		{},
	};
	int option;

	options->ifname = ACE_XDP_DEFAULT_IFNAME;
	options->output_prefix = "";
	options->record_capacity = ACE_XDP_RECORD_DEFAULT_ENTRIES;
	options->quiet = true;

	while ((option = getopt_long(argc, argv, "o:p:qvn:rch",
				 long_options, NULL)) != -1) {
		switch (option) {
		case 'o':
			options->output_dir = optarg;
			break;
		case 'p':
			options->output_prefix = optarg;
			break;
		case 'q':
			options->quiet = true;
			break;
		case 'v':
			options->quiet = false;
			break;
		case 'n':
			if (parse_u32(optarg, 1, ACE_XDP_RECORD_MAX_ENTRIES,
				      &options->record_capacity)) {
				fprintf(stderr, "invalid record capacity: %s\n", optarg);
				return -EINVAL;
			}
			break;
		case 'r':
			options->recover = true;
			break;
		case 'c':
			options->cleanup_pins = true;
			break;
		case 'h':
			usage(argv[0]);
			return 1;
		default:
			usage(argv[0]);
			return -EINVAL;
		}
	}

	if (optind < argc)
		options->ifname = argv[optind++];
	if (optind != argc) {
		usage(argv[0]);
		return -EINVAL;
	}
	if (!options->output_dir) {
		fprintf(stderr, "--output-dir is required so measurement records are not lost\n");
		usage(argv[0]);
		return -EINVAL;
	}
	if (options->cleanup_pins && !options->recover) {
		fprintf(stderr, "--cleanup-pins is valid only with --recover\n");
		return -EINVAL;
	}
	for (const char *cursor = options->output_prefix; *cursor; cursor++) {
		char c = *cursor;

		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
		      (c >= '0' && c <= '9') || c == '_' || c == '-' ||
		      c == '.')) {
			fprintf(stderr, "unsafe output prefix: %s\n",
				options->output_prefix);
			return -EINVAL;
		}
	}
	if (strlen(options->output_prefix) > 64) {
		fprintf(stderr, "output prefix is too long (max 64 bytes)\n");
		return -EINVAL;
	}
	return 0;
}

static int validate_output_dir(const char *path)
{
	struct stat st;

	if (stat(path, &st))
		return -errno;
	if (!S_ISDIR(st.st_mode))
		return -ENOTDIR;
	if (access(path, W_OK))
		return -errno;
	return 0;
}

static int make_output_path(char *path, size_t path_size,
			    const struct loader_options *options,
			    const char *suffix)
{
	int length;

	length = snprintf(path, path_size, "%s/%s%s", options->output_dir,
			  options->output_prefix, suffix);
	if (length < 0 || (size_t)length >= path_size)
		return -ENAMETOOLONG;
	return 0;
}

static FILE *create_csv(const char *path)
{
	int fd;
	FILE *file;

	fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
	if (fd < 0)
		return NULL;
	file = fdopen(fd, "w");
	if (!file) {
		int saved_errno = errno;

		close(fd);
		errno = saved_errno;
		return NULL;
	}
	return file;
}

static int finish_csv(FILE *file)
{
	int saved_errno;

	if (ferror(file) || fflush(file)) {
		saved_errno = errno ? errno : EIO;
		fclose(file);
		errno = saved_errno;
		return -1;
	}
	if (fsync(fileno(file))) {
		saved_errno = errno;
		fclose(file);
		errno = saved_errno;
		return -1;
	}
	return fclose(file);
}

static int ensure_empty_pin_dir(void)
{
	struct dirent *entry;
	struct stat st;
	DIR *directory;

	if (mkdir(ACE_XDP_PIN_DIR, 0755) && errno != EEXIST)
		return -errno;
	if (stat(ACE_XDP_PIN_DIR, &st))
		return -errno;
	if (!S_ISDIR(st.st_mode))
		return -ENOTDIR;

	directory = opendir(ACE_XDP_PIN_DIR);
	if (!directory)
		return -errno;
	while ((entry = readdir(directory)) != NULL) {
		if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, ".."))
			continue;
		closedir(directory);
		return -EEXIST;
	}
	closedir(directory);
	return 0;
}

static int validate_map(int fd, const char *name, __u32 type,
			__u32 key_size, __u32 value_size, __u32 max_entries)
{
	struct bpf_map_info info = {};
	__u32 info_len = sizeof(info);

	if (bpf_obj_get_info_by_fd(fd, &info, &info_len))
		return -errno;
	if (strcmp(info.name, name) || info.type != type || info.key_size != key_size ||
	    info.value_size != value_size || info.max_entries != max_entries) {
		fprintf(stderr,
			"map %s schema mismatch: actual_name=%s type=%u key=%u value=%u max=%u\n",
			name, info.name, info.type, info.key_size, info.value_size,
			info.max_entries);
		return -EINVAL;
	}
	return 0;
}

static int validate_maps(struct xdp_kern *skel, __u32 record_capacity)
{
	int err;

#define VALIDATE_MAP(map_name, map_type, key_type, value_type, entries)       \
	do {                                                                    \
		err = validate_map(bpf_map__fd(skel->maps.map_name), #map_name,    \
				   map_type, sizeof(key_type), sizeof(value_type),  \
				   entries);                                      \
		if (err)                                                         \
			return err;                                               \
	} while (0)

	VALIDATE_MAP(cpu_map, BPF_MAP_TYPE_CPUMAP, __u32,
		     struct bpf_cpumap_val, ACE_XDP_CPU_MAP_MAX_ENTRIES);
	VALIDATE_MAP(xsk_map, BPF_MAP_TYPE_XSKMAP, __u32, __u32,
		     ACE_XDP_XSK_MAP_MAX_ENTRIES);
	VALIDATE_MAP(xsk_owners, BPF_MAP_TYPE_HASH, __u32,
		     struct ace_xsk_owner, ACE_XDP_XSK_MAP_MAX_ENTRIES);
	VALIDATE_MAP(stats, BPF_MAP_TYPE_PERCPU_ARRAY, __u32, __u64,
		     ACE_XDP_STAT_COUNT);
	VALIDATE_MAP(ingress_records, BPF_MAP_TYPE_HASH,
		     struct ace_packet_key, struct ace_ingress_record,
		     record_capacity);
	VALIDATE_MAP(cpumap_records, BPF_MAP_TYPE_HASH,
		     struct ace_packet_key, struct ace_cpumap_record,
		     record_capacity);
	VALIDATE_MAP(runtime_config, BPF_MAP_TYPE_ARRAY, __u32,
		     struct ace_runtime_config, 1);

#undef VALIDATE_MAP
	return 0;
}

/* runtime_config는 loader와 AF_XDP 수신기가 공유하는 준비 상태다.
 * CAPTURED = 기존 설정 저장, APPLYING = 장치 설정 적용 중,
 * APPLIED = 적용 완료, STOPPING = 종료 중이므로 새 수신기 등록 금지.
 * 이 상태와 저장된 설정으로 --recover도 중단 지점 이후 정리를 이어간다. */
static void runtime_set_hwtstamp(struct ace_runtime_config *runtime,
		const struct ace_hwtstamp_state *state, bool applied)
{
	runtime->saved_hwtstamp_flags = state->saved.flags;
	runtime->saved_hwtstamp_tx_type = state->saved.tx_type;
	runtime->saved_hwtstamp_rx_filter = state->saved.rx_filter;
	runtime->effective_hwtstamp_flags = state->effective.flags;
	runtime->effective_hwtstamp_tx_type = state->effective.tx_type;
	runtime->effective_hwtstamp_rx_filter = state->effective.rx_filter;
	runtime->flags |= ACE_RUNTIME_F_HWTSTAMP_CAPTURED;
	runtime->flags &= ~(ACE_RUNTIME_F_HWTSTAMP_APPLYING |
			    ACE_RUNTIME_F_HWTSTAMP_APPLIED);
	if (applied)
		runtime->flags |= ACE_RUNTIME_F_HWTSTAMP_APPLIED;
}

static void runtime_set_hwtstamp_applying(
		struct ace_runtime_config *runtime,
		const struct ace_hwtstamp_state *state)
{
	runtime_set_hwtstamp(runtime, state, false);
	runtime->flags |= ACE_RUNTIME_F_HWTSTAMP_APPLYING;
}

/* CPUMAP의 key는 목적 CPU 번호, value는 그 CPU의 대기열과 실행할 BPF다.
 * 따라서 UDP/9001은 CPU를 옮긴 뒤 xdp_cpumap_measure에서 도착 시각을
 * 기록한다. 여기서는 CPU 이동 이후의 일반 사용자 프로세스를 만들지 않는다.
 * XSKMAP은 비워 두고, 실제 socket FD를 가진 AF_XDP 수신기가 채운다. */
static int configure_maps(struct xdp_kern *skel,
		const struct ace_runtime_config *runtime)
{
	struct bpf_cpumap_val cpu_value = {
		.qsize = ACE_XDP_CPU_MAP_QUEUE_SIZE,
	};
	__u32 cpu = ACE_XDP_RT_CPU;
	__u32 zero = 0;
	int remote_fd;

	remote_fd = bpf_program__fd(skel->progs.xdp_cpumap_measure);
	if (remote_fd < 0)
		return -EINVAL;
	cpu_value.bpf_prog.fd = remote_fd;
	if (bpf_map_update_elem(bpf_map__fd(skel->maps.cpu_map), &cpu,
				&cpu_value, BPF_ANY))
		return -errno;
	if (bpf_map_update_elem(bpf_map__fd(skel->maps.runtime_config), &zero,
				runtime, BPF_ANY))
		return -errno;
	return 0;
}

static int update_runtime_config(struct xdp_kern *skel,
		const struct ace_runtime_config *runtime)
{
	__u32 zero = 0;

	if (bpf_map_update_elem(bpf_map__fd(skel->maps.runtime_config), &zero,
				runtime, BPF_ANY))
		return -errno;
	return 0;
}

static int get_program_id(int fd, __u32 *id)
{
	struct bpf_prog_info info = {};
	__u32 info_len = sizeof(info);

	if (bpf_obj_get_info_by_fd(fd, &info, &info_len))
		return -errno;
	*id = info.id;
	return 0;
}

static int hwtstamp_state_from_runtime(
		const struct ace_runtime_config *runtime, const char *ifname,
		struct ace_hwtstamp_state *state)
{
	size_t length = strlen(ifname);

	if (!(runtime->flags & ACE_RUNTIME_F_HWTSTAMP_CAPTURED))
		return -ENODATA;
	if (length >= sizeof(state->interface_name))
		return -ENAMETOOLONG;
	memset(state, 0, sizeof(*state));
	memcpy(state->interface_name, ifname, length + 1);
	state->saved.flags = runtime->saved_hwtstamp_flags;
	state->saved.tx_type = runtime->saved_hwtstamp_tx_type;
	state->saved.rx_filter = runtime->saved_hwtstamp_rx_filter;
	state->effective.flags = runtime->effective_hwtstamp_flags;
	state->effective.tx_type = runtime->effective_hwtstamp_tx_type;
	state->effective.rx_filter = runtime->effective_hwtstamp_rx_filter;
	state->active = true;
	state->changed = !ace_hwtstamp_config_equal(&state->saved,
						    &state->effective);
	return 0;
}

static int query_driver_program(int ifindex, __u32 *id)
{
	int err = bpf_xdp_query_id(ifindex, XDP_FLAGS_DRV_MODE, id);

	return err < 0 ? err : 0;
}

static int xsk_owners_have_live_claim(int fd, bool *has_live)
{
	__u32 key;

	*has_live = false;
	while (!bpf_map_get_next_key(fd, NULL, &key)) {
		struct ace_xsk_owner owner;
		bool alive;
		int err;

		if (bpf_map_lookup_elem(fd, &key, &owner)) {
			if (errno == ENOENT)
				continue;
			return -errno;
		}
		if (!owner.pid || owner.pid > (unsigned int)INT_MAX ||
		    !owner.process_start_ticks || owner.reserved)
			return -EINVAL;
		err = ace_process_identity_is_alive((pid_t)owner.pid,
			owner.process_start_ticks, &alive);
		if (err)
			return err;
		if (alive) {
			struct ace_xsk_owner latest;

			if (bpf_map_lookup_elem(fd, &key, &latest)) {
				if (errno == ENOENT)
					continue;
				return -errno;
			}
			/* A simultaneous receiver shutdown may remove the claim after
			 * the liveness check.  Any still-present value is either that
			 * owner or a newer claim, and both block detach. */
			*has_live = true;
			return 0;
		}
		/* A dead receiver cannot replace its NOEXIST claim.  Removing the
		 * stale entry lets recovery proceed after SIGKILL. */
		if (bpf_map_delete_elem(fd, &key) && errno != ENOENT)
			return -errno;
	}
	if (errno != ENOENT)
		return -errno;
	return 0;
}

static void print_stats(int fd)
{
	int ncpu = libbpf_num_possible_cpus();
	__u64 *values;

	if (ncpu < 1)
		return;
	values = calloc((size_t)ncpu, sizeof(*values));
	if (!values)
		return;

	for (__u32 id = 0; id < (__u32)ACE_XDP_STAT_COUNT; id++) {
		__u64 total = 0;

		if (bpf_map_lookup_elem(fd, &id, values))
			continue;
		for (int cpu = 0; cpu < ncpu; cpu++)
			total += values[cpu];
		printf("%s=%llu%s", stat_names[id],
		       (unsigned long long)total,
		       id + 1 == (__u32)ACE_XDP_STAT_COUNT ? "\n" : " ");
	}
	free(values);
}

static unsigned long long count_records(int fd)
{
	struct ace_packet_key current;
	struct ace_packet_key next;
	const void *previous = NULL;
	unsigned long long count = 0;

	while (!bpf_map_get_next_key(fd, previous, &next)) {
		current = next;
		previous = &current;
		count++;
	}
	return count;
}

static void print_record_summary(struct xdp_kern *skel)
{
	printf("records ingress=%llu cpumap=%llu\n",
	       count_records(bpf_map__fd(skel->maps.ingress_records)),
	       count_records(bpf_map__fd(skel->maps.cpumap_records)));
}

/* 패킷마다 파일을 쓰면 측정 경로에 I/O 부하가 들어가므로 수신 중에는
 * HASH map에만 기록하고, 신규 수신·CPUMAP 처리가 멎은 뒤 CSV로 꺼낸다.
 * HASH 순회 순서는 패킷 순서가 아니다. 분석기는 flow_id + sequence로
 * ingress/CPUMAP/사용자 수신 기록을 합친다. run_id는 패킷 key에 없다. */
static int export_ingress_records(int fd, const char *path)
{
	struct ace_packet_key current;
	struct ace_packet_key next;
	const void *previous = NULL;
	FILE *file = create_csv(path);

	if (!file)
		return -errno;
	fprintf(file,
		"flow_id,sequence,rx_queue,hw_rx_ns,initial_xdp_ns,flags,"
		"requested_path,timestamp_error\n");
	while (!bpf_map_get_next_key(fd, previous, &next)) {
		struct ace_ingress_record record;

		current = next;
		if (bpf_map_lookup_elem(fd, &current, &record)) {
			int err = -errno;

			fclose(file);
			return err;
		}
		fprintf(file, "%u,%" PRIu64 ",%u,%" PRIu64 ",%" PRIu64
			",%u,%u,%d\n",
			current.flow_id, (uint64_t)current.sequence,
			record.rx_queue, (uint64_t)record.hw_rx_ns,
			(uint64_t)record.initial_xdp_ns, record.flags,
			record.requested_path, record.timestamp_error);
		previous = &current;
	}
	if (errno != ENOENT) {
		int err = -errno;

		fclose(file);
		return err;
	}
	return finish_csv(file) ? -errno : 0;
}

static int export_cpumap_records(int fd, const char *path)
{
	struct ace_packet_key current;
	struct ace_packet_key next;
	const void *previous = NULL;
	FILE *file = create_csv(path);

	if (!file)
		return -errno;
	fprintf(file,
		"flow_id,sequence,cpu,cpumap_ns,flags,requested_path\n");
	while (!bpf_map_get_next_key(fd, previous, &next)) {
		struct ace_cpumap_record record;

		current = next;
		if (bpf_map_lookup_elem(fd, &current, &record)) {
			int err = -errno;

			fclose(file);
			return err;
		}
		fprintf(file, "%u,%" PRIu64 ",%u,%" PRIu64 ",%u,%u\n",
			current.flow_id, (uint64_t)current.sequence, record.cpu,
			(uint64_t)record.cpumap_ns, record.flags,
			record.requested_path);
		previous = &current;
	}
	if (errno != ENOENT) {
		int err = -errno;

		fclose(file);
		return err;
	}
	return finish_csv(file) ? -errno : 0;
}

static int export_stats(int fd, const char *path)
{
	int ncpu = libbpf_num_possible_cpus();
	__u64 *values;
	FILE *file;
	int result = 0;

	if (ncpu < 1)
		return -EINVAL;
	values = calloc((size_t)ncpu, sizeof(*values));
	if (!values)
		return -ENOMEM;
	file = create_csv(path);
	if (!file) {
		result = -errno;
		goto out;
	}
	fprintf(file, "stat_id,name,value\n");
	for (__u32 id = 0; id < (__u32)ACE_XDP_STAT_COUNT; id++) {
		__u64 total = 0;

		if (bpf_map_lookup_elem(fd, &id, values)) {
			result = -errno;
			fclose(file);
			goto out;
		}
		for (int cpu = 0; cpu < ncpu; cpu++)
			total += values[cpu];
		fprintf(file, "%u,%s,%" PRIu64 "\n", id, stat_names[id],
			(uint64_t)total);
	}
	if (finish_csv(file))
		result = -errno;
out:
	free(values);
	return result;
}

static int prepare_measurement_paths(const struct loader_options *options,
				     char *ingress_path, size_t ingress_size,
				     char *cpumap_path, size_t cpumap_size,
				     char *stats_path, size_t stats_size)
{
	struct stat st;
	int err;

	err = validate_output_dir(options->output_dir);
	if (err)
		return err;
	if ((err = make_output_path(ingress_path, ingress_size, options,
				    "ingress_records.csv")) ||
	    (err = make_output_path(cpumap_path, cpumap_size, options,
				    "cpumap_records.csv")) ||
	    (err = make_output_path(stats_path, stats_size, options,
				    "xdp_stats.csv")))
		return err;

	/* Never overwrite another run. The prefix/output directory is the run ID. */
	if (!lstat(ingress_path, &st))
		return -EEXIST;
	if (errno != ENOENT)
		return -errno;
	if (!lstat(cpumap_path, &st))
		return -EEXIST;
	if (errno != ENOENT)
		return -errno;
	if (!lstat(stats_path, &st))
		return -EEXIST;
	if (errno != ENOENT)
		return -errno;
	return 0;
}

static int export_measurement_files(int ingress_fd, int cpumap_fd, int stats_fd,
				    const struct loader_options *options)
{
	char ingress_path[PATH_MAX];
	char cpumap_path[PATH_MAX];
	char stats_path[PATH_MAX];
	int err;

	err = prepare_measurement_paths(options, ingress_path,
		sizeof(ingress_path), cpumap_path, sizeof(cpumap_path),
		stats_path, sizeof(stats_path));
	if (err)
		return err;

	err = export_ingress_records(ingress_fd, ingress_path);
	if (err)
		return err;
	err = export_cpumap_records(cpumap_fd, cpumap_path);
	if (err)
		return err;
	err = export_stats(stats_fd, stats_path);
	if (err)
		return err;

	printf("saved measurement CSV: %s, %s, %s\n",
	       ingress_path, cpumap_path, stats_path);
	return 0;
}

static int detach_if_owned(int ifindex, int prog_fd, __u32 owned_id)
{
	LIBBPF_OPTS(bpf_xdp_attach_opts, detach_opts,
		.old_prog_fd = prog_fd);
	__u32 current_id = 0;
	int err;

	err = query_driver_program(ifindex, &current_id);
	if (err)
		return err;
	if (!current_id)
		return 0;
	if (current_id != owned_id) {
		fprintf(stderr,
			"not detaching: current driver-mode XDP id %u is not owned id %u\n",
			current_id, owned_id);
		return -ESTALE;
	}
	return bpf_xdp_detach(ifindex, XDP_FLAGS_DRV_MODE, &detach_opts);
}

static int lookup_stat_total(int fd, __u32 id, __u64 *values, int ncpu,
		__u64 *total)
{
	*total = 0;
	if (bpf_map_lookup_elem(fd, &id, values))
		return -errno;
	for (int cpu = 0; cpu < ncpu; cpu++)
		*total += values[cpu];
	return 0;
}

/* Detaching ingress prevents new redirects, but it is not itself a CPUMAP
 * drain barrier.  Wait for request/terminal counters to become stable before
 * iterating the record maps.  A stable deficit is reported as an unobserved
 * redirect/enqueue loss instead of making shutdown wait forever. */
/* XDP detach는 새 유입만 끊는다. 이미 목적 CPU 대기열에 들어간 패킷의
 * 기록은 조금 늦게 생길 수 있어, 여기서는 카운터가 안정될 때까지 기다린다.
 * requested는 redirect 요청, terminal은 목적 CPU에서 관측한 처리 결과다.
 * 둘의 차이는 성공한 패킷으로 채우지 않고 미관측 수로 보고한다. */
struct cpumap_drain_report {
	__u64 requested;
	__u64 terminal;
	__u64 unobserved;
};

static int wait_for_cpumap_records(int stats_fd,
		struct cpumap_drain_report *report)
{
	struct timespec interval = {
		.tv_nsec = ACE_XDP_CPUMAP_DRAIN_POLL_MS * 1000000L,
	};
	__u64 requested, recorded, duplicate, update_error, invalid, terminal;
	__u64 previous_requested = UINT64_MAX;
	__u64 previous_terminal = UINT64_MAX;
	__u64 *values;
	unsigned int last_change_ms = 0;
	unsigned int stable_snapshots = 0;
	int ncpu = libbpf_num_possible_cpus();
	int err = 0;

	if (ncpu < 1)
		return -EINVAL;
	values = calloc((size_t)ncpu, sizeof(*values));
	if (!values)
		return -ENOMEM;

	for (unsigned int elapsed = 0;
	     elapsed <= ACE_XDP_CPUMAP_DRAIN_TIMEOUT_MS;
	     elapsed += ACE_XDP_CPUMAP_DRAIN_POLL_MS) {
		/* Read terminal outcomes first and the ingress-side request count
		 * last.  Concurrent completion can only make this conservative. */
		if ((err = lookup_stat_total(stats_fd,
					     ACE_XDP_STAT_CPUMAP_RECORDED,
					     values, ncpu, &recorded)) ||
		    (err = lookup_stat_total(stats_fd,
					     ACE_XDP_STAT_CPUMAP_DUPLICATE,
					     values, ncpu, &duplicate)) ||
		    (err = lookup_stat_total(stats_fd,
					     ACE_XDP_STAT_CPUMAP_UPDATE_ERROR,
					     values, ncpu, &update_error)) ||
		    (err = lookup_stat_total(stats_fd,
					     ACE_XDP_STAT_CPUMAP_META_INVALID,
					     values, ncpu, &invalid)) ||
		    (err = lookup_stat_total(stats_fd, ACE_XDP_STAT_CPUMAP,
					     values, ncpu, &requested)))
			break;
		terminal = recorded + duplicate + update_error + invalid;
		if (requested == previous_requested && terminal == previous_terminal)
			stable_snapshots++;
		else {
			stable_snapshots = 0;
			last_change_ms = elapsed;
		}
		if (requested == terminal &&
		    elapsed >= ACE_XDP_CPUMAP_DRAIN_GRACE_MS &&
		    stable_snapshots >= ACE_XDP_CPUMAP_STABLE_POLLS) {
			report->requested = requested;
			report->terminal = terminal;
			report->unobserved = 0;
			err = 0;
			break;
		}
		previous_requested = requested;
		previous_terminal = terminal;
		if (elapsed == ACE_XDP_CPUMAP_DRAIN_TIMEOUT_MS) {
			/* A redirect can be accepted by bpf_redirect_map() and still
			 * be lost while it is enqueued to CPUMAP.  Only classify a
			 * remaining deficit as that loss after the full drain window
			 * and a long, quiet final interval. */
			if (requested > terminal &&
			    elapsed - last_change_ms >= ACE_XDP_CPUMAP_LOSS_QUIET_MS &&
			    stable_snapshots >= ACE_XDP_CPUMAP_STABLE_POLLS) {
				report->requested = requested;
				report->terminal = terminal;
				report->unobserved = requested - terminal;
				err = 0;
			} else {
				err = -ETIMEDOUT;
			}
			break;
		}
		while (nanosleep(&interval, &interval) && errno == EINTR)
			;
		interval.tv_sec = 0;
		interval.tv_nsec = ACE_XDP_CPUMAP_DRAIN_POLL_MS * 1000000L;
	}
	free(values);
	return err;
}

static int cleanup_recovery_pins(void)
{
	static const char *const paths[] = {
		ACE_XDP_CPU_MAP_PIN,
		ACE_XDP_XSK_MAP_PIN,
		ACE_XDP_XSK_OWNER_MAP_PIN,
		ACE_XDP_STATS_MAP_PIN,
		ACE_XDP_INGRESS_MAP_PIN,
		ACE_XDP_CPUMAP_MAP_PIN,
		ACE_XDP_RUNTIME_MAP_PIN,
	};

	for (size_t i = 0; i < sizeof(paths) / sizeof(paths[0]); i++)
		if (unlink(paths[i]) && errno != ENOENT)
			return -errno;
	if (rmdir(ACE_XDP_PIN_DIR) && errno != ENOENT)
		return -errno;
	return 0;
}

/* --recover는 새 실험을 시작하지 않는다. 이전 loader가 종료된 뒤 남은
 * pin을 열어 XDP 정지·timestamp 복원·CSV 저장을 마무리하는 별도 경로다.
 * 정상 실험에서는 main()의 시작/종료 흐름만 따라 읽으면 된다. */
static int recover_pinned_measurement(const struct loader_options *options)
{
	struct ace_runtime_config runtime;
	struct ace_hwtstamp_state hwtstamp = {};
	struct cpumap_drain_report drain = {};
	__u32 current_id = 0;
	__u32 zero = 0;
	int cpu_fd = -1, xsk_fd = -1, xsk_owner_fd = -1, stats_fd = -1;
	int ingress_fd = -1, cpumap_fd = -1, runtime_fd = -1;
	int owned_prog_fd = -1, hwtstamp_fd = -1;
	int ifindex;
	int err;
	int result = 1;
	bool owner_alive;
	bool xsk_active = false;
	bool state_error = false;

	ifindex = if_nametoindex(options->ifname);
	if (!ifindex) {
		fprintf(stderr, "interface %s: %s\n", options->ifname,
			strerror(errno));
		return 1;
	}

#define OPEN_PIN(fd_name, path)                                                \
	do {                                                                       \
		fd_name = bpf_obj_get(path);                                          \
		if (fd_name < 0) {                                                    \
			err = -errno;                                                    \
			fprintf(stderr, "cannot open pinned map %s: %s\n", path,       \
				strerror(-err));                                          \
			goto out;                                                        \
		}                                                                      \
	} while (0)

	OPEN_PIN(runtime_fd, ACE_XDP_RUNTIME_MAP_PIN);
	if ((err = validate_map(runtime_fd, "runtime_config", BPF_MAP_TYPE_ARRAY,
				 sizeof(__u32), sizeof(runtime), 1)) ||
	    bpf_map_lookup_elem(runtime_fd, &zero, &runtime)) {
		if (!err)
			err = -errno;
		fprintf(stderr, "invalid pinned runtime configuration: %s\n",
			strerror(-err));
		goto out;
	}
	if (runtime.abi_version != ACE_XDP_RUNTIME_ABI_VERSION ||
	    runtime.ifindex != (__u32)ifindex || !runtime.record_capacity ||
	    runtime.record_capacity > ACE_XDP_RECORD_MAX_ENTRIES ||
	    !runtime.xdp_program_id || !runtime.owner_pid ||
	    !runtime.owner_start_ticks ||
	    !(runtime.flags & ACE_RUNTIME_F_HWTSTAMP_CAPTURED) ||
	    ((runtime.flags & (ACE_RUNTIME_F_HWTSTAMP_APPLYING |
			       ACE_RUNTIME_F_HWTSTAMP_APPLIED)) ==
	     (ACE_RUNTIME_F_HWTSTAMP_APPLYING |
	      ACE_RUNTIME_F_HWTSTAMP_APPLIED)) ||
	    (runtime.flags & ~(ACE_RUNTIME_F_HWTSTAMP_CAPTURED |
			       ACE_RUNTIME_F_HWTSTAMP_APPLYING |
			       ACE_RUNTIME_F_HWTSTAMP_APPLIED |
			       ACE_RUNTIME_F_STOPPING))) {
		fprintf(stderr,
			"pinned runtime mismatch: abi=%u ifindex=%u records=%u "
			"program=%u owner=%u flags=%#x\n",
			runtime.abi_version, runtime.ifindex,
			runtime.record_capacity, runtime.xdp_program_id,
			runtime.owner_pid, runtime.flags);
		goto out;
	}
	err = ace_process_identity_is_alive((pid_t)runtime.owner_pid,
		runtime.owner_start_ticks, &owner_alive);
	if (err) {
		fprintf(stderr, "cannot validate pinned loader owner: %s\n",
			strerror(-err));
		goto out;
	}
	if (owner_alive) {
		fprintf(stderr,
			"refusing recovery while original loader pid %u is still alive\n",
			runtime.owner_pid);
		goto out;
	}
	/* Block any receiver that began setup before the loader died.  AF_XDP
	 * claims its queue before re-reading this flag, so it either appears in
	 * xsk_owners below or aborts without registering in XSKMAP. */
	runtime.flags |= ACE_RUNTIME_F_STOPPING;
	if (bpf_map_update_elem(runtime_fd, &zero, &runtime, BPF_ANY)) {
		err = -errno;
		fprintf(stderr, "cannot publish recovery STOPPING state: %s\n",
			strerror(-err));
		goto out;
	}

	OPEN_PIN(cpu_fd, ACE_XDP_CPU_MAP_PIN);
	OPEN_PIN(xsk_fd, ACE_XDP_XSK_MAP_PIN);
	OPEN_PIN(xsk_owner_fd, ACE_XDP_XSK_OWNER_MAP_PIN);
	OPEN_PIN(stats_fd, ACE_XDP_STATS_MAP_PIN);
	OPEN_PIN(ingress_fd, ACE_XDP_INGRESS_MAP_PIN);
	OPEN_PIN(cpumap_fd, ACE_XDP_CPUMAP_MAP_PIN);
	if ((err = validate_map(cpu_fd, "cpu_map", BPF_MAP_TYPE_CPUMAP,
				 sizeof(__u32), sizeof(struct bpf_cpumap_val),
				 ACE_XDP_CPU_MAP_MAX_ENTRIES)) ||
	    (err = validate_map(xsk_fd, "xsk_map", BPF_MAP_TYPE_XSKMAP,
				 sizeof(__u32), sizeof(__u32),
				 ACE_XDP_XSK_MAP_MAX_ENTRIES)) ||
	    (err = validate_map(xsk_owner_fd, "xsk_owners", BPF_MAP_TYPE_HASH,
				 sizeof(__u32), sizeof(struct ace_xsk_owner),
				 ACE_XDP_XSK_MAP_MAX_ENTRIES)) ||
	    (err = validate_map(stats_fd, "stats", BPF_MAP_TYPE_PERCPU_ARRAY,
				 sizeof(__u32), sizeof(__u64), ACE_XDP_STAT_COUNT)) ||
	    (err = validate_map(ingress_fd, "ingress_records", BPF_MAP_TYPE_HASH,
				 sizeof(struct ace_packet_key),
				 sizeof(struct ace_ingress_record),
				 runtime.record_capacity)) ||
	    (err = validate_map(cpumap_fd, "cpumap_records", BPF_MAP_TYPE_HASH,
				 sizeof(struct ace_packet_key),
				 sizeof(struct ace_cpumap_record),
				 runtime.record_capacity))) {
		fprintf(stderr, "pinned measurement map validation failed: %s\n",
			strerror(-err));
		goto out;
	}
	err = xsk_owners_have_live_claim(xsk_owner_fd, &xsk_active);
	if (err) {
		fprintf(stderr, "cannot inspect pinned XSK owner claims: %s\n",
			strerror(-err));
		goto out;
	}
	if (xsk_active) {
		fprintf(stderr,
			"refusing recovery while an AF_XDP socket is still registered; stop the receiver first\n");
		goto out;
	}

	if ((err = query_driver_program(ifindex, &current_id))) {
		fprintf(stderr, "cannot query driver-mode XDP on %s: %s\n",
			options->ifname, strerror(-err));
		goto out;
	}
	if (current_id && current_id != runtime.xdp_program_id) {
		fprintf(stderr,
			"refusing recovery: attached driver-mode XDP id %u differs "
			"from pinned owner id %u\n",
			current_id, runtime.xdp_program_id);
		goto out;
	}
	if (current_id) {
		owned_prog_fd = bpf_prog_get_fd_by_id(runtime.xdp_program_id);
		if (owned_prog_fd < 0) {
			err = -errno;
			fprintf(stderr, "cannot open owned XDP program id %u: %s\n",
				runtime.xdp_program_id, strerror(-err));
			goto out;
		}
		err = detach_if_owned(ifindex, owned_prog_fd,
				      runtime.xdp_program_id);
		if (err) {
			fprintf(stderr, "cannot detach crashed loader's XDP program: %s\n",
				strerror(-err));
			goto out;
		}
		printf("detached recovered native XDP program id %u from %s\n",
		       runtime.xdp_program_id, options->ifname);
	}

	hwtstamp_fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (hwtstamp_fd < 0) {
		fprintf(stderr,
			"warning: cannot create recovery HWTSTAMP socket: %s\n",
			strerror(errno));
		state_error = true;
	} else {
		err = hwtstamp_state_from_runtime(&runtime, options->ifname,
						 &hwtstamp);
		if (!err)
			err = ace_hwtstamp_restore_allow_saved(
				hwtstamp_fd, &hwtstamp, true);
		if (err < 0) {
			fprintf(stderr, "warning: cannot restore recovered HWTSTAMP: %s\n",
				strerror(-err));
			state_error = true;
		} else if (err > 0) {
			fprintf(stderr,
				"warning: HWTSTAMP changed externally; saved state was not restored\n");
			state_error = true;
		} else {
			printf("restored recovered HWTSTAMP configuration on %s\n",
			       options->ifname);
			runtime.effective_hwtstamp_flags =
				runtime.saved_hwtstamp_flags;
			runtime.effective_hwtstamp_tx_type =
				runtime.saved_hwtstamp_tx_type;
			runtime.effective_hwtstamp_rx_filter =
				runtime.saved_hwtstamp_rx_filter;
			runtime.flags &= ~(ACE_RUNTIME_F_HWTSTAMP_APPLYING |
					   ACE_RUNTIME_F_HWTSTAMP_APPLIED);
			if (bpf_map_update_elem(runtime_fd, &zero, &runtime,
						BPF_ANY)) {
				fprintf(stderr,
					"warning: cannot publish restored HWTSTAMP state: %s\n",
					strerror(errno));
				state_error = true;
			}
		}
	}

	err = wait_for_cpumap_records(stats_fd, &drain);
	if (err) {
		fprintf(stderr, "pinned CPUMAP data is not quiescent: %s\n",
			strerror(-err));
		goto out;
	}
	if (drain.unobserved)
		fprintf(stderr,
			"warning: CPUMAP requested=%" PRIu64 " terminal=%" PRIu64
			" unobserved=%" PRIu64 " (redirect/enqueue loss)\n",
			(uint64_t)drain.requested, (uint64_t)drain.terminal,
			(uint64_t)drain.unobserved);
	print_stats(stats_fd);
	printf("records ingress=%llu cpumap=%llu\n",
	       count_records(ingress_fd), count_records(cpumap_fd));
	err = export_measurement_files(ingress_fd, cpumap_fd, stats_fd, options);
	if (err) {
		fprintf(stderr, "recovery export failed: %s; pins retained\n",
			strerror(-err));
		goto out;
	}
	if (options->cleanup_pins && !state_error) {
		err = cleanup_recovery_pins();
		if (err) {
			fprintf(stderr, "recovery succeeded but pin cleanup failed: %s\n",
				strerror(-err));
			goto out;
		}
		printf("removed recovered pins from %s\n", ACE_XDP_PIN_DIR);
	} else {
		if (options->cleanup_pins && state_error)
			fprintf(stderr,
				"recovery state restoration failed; retaining pins despite --cleanup-pins\n");
		printf("recovery pins retained at %s (use --cleanup-pins after archiving)\n",
		       ACE_XDP_PIN_DIR);
	}
	result = state_error ? 1 : 0;

out:
	if (hwtstamp_fd >= 0)
		close(hwtstamp_fd);
	if (owned_prog_fd >= 0)
		close(owned_prog_fd);
	if (runtime_fd >= 0)
		close(runtime_fd);
	if (cpumap_fd >= 0)
		close(cpumap_fd);
	if (ingress_fd >= 0)
		close(ingress_fd);
	if (stats_fd >= 0)
		close(stats_fd);
	if (xsk_fd >= 0)
		close(xsk_fd);
	if (xsk_owner_fd >= 0)
		close(xsk_owner_fd);
	if (cpu_fd >= 0)
		close(cpu_fd);
#undef OPEN_PIN
	return result;
}

int main(int argc, char **argv)
{
	struct loader_options options = {};
	struct xdp_kern *skel = NULL;
	struct ace_hwtstamp_state hwtstamp = {};
	struct ace_runtime_config runtime = {};
	struct cpumap_drain_report drain = {};
	__u32 existing_id = 0;
	__u32 owned_id = 0;
	int prog_fd = -1;
	int hwtstamp_fd = -1;
	int ifindex;
	int err;
	int result = 1;
	bool attached = false;
	bool maps_pinned = false;
	bool measurement_complete = false;
	bool preserve_pins = false;
	bool xsk_active = false;

	err = parse_options(argc, argv, &options);
	if (err > 0)
		return 0;
	if (err)
		return 1;
	{
		char ingress_path[PATH_MAX];
		char cpumap_path[PATH_MAX];
		char stats_path[PATH_MAX];

		err = prepare_measurement_paths(&options, ingress_path,
			sizeof(ingress_path), cpumap_path, sizeof(cpumap_path),
			stats_path, sizeof(stats_path));
		if (err) {
			fprintf(stderr, "measurement output is not usable: %s\n",
				strerror(-err));
			return 1;
		}
	}
	if (options.recover)
		return recover_pinned_measurement(&options);

	ifindex = if_nametoindex(options.ifname);
	if (!ifindex) {
		fprintf(stderr, "interface %s: %s\n", options.ifname,
			strerror(errno));
		return 1;
	}
	if ((err = install_signal_handlers())) {
		fprintf(stderr, "signal setup failed: %s\n", strerror(-err));
		return 1;
	}

	libbpf_set_strict_mode(LIBBPF_STRICT_ALL);
	libbpf_set_print(NULL);

	err = query_driver_program(ifindex, &existing_id);
	if (err) {
		fprintf(stderr, "cannot query driver-mode XDP on %s: %s\n",
			options.ifname, strerror(-err));
		return 1;
	}
	if (existing_id) {
		fprintf(stderr,
			"refusing to replace existing driver-mode XDP program id %u on %s\n",
			existing_id, options.ifname);
		return 1;
	}

	err = ensure_empty_pin_dir();
	if (err) {
		fprintf(stderr, "pin directory %s is not empty/usable: %s\n",
			ACE_XDP_PIN_DIR, strerror(-err));
		return 1;
	}

	hwtstamp_fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (hwtstamp_fd < 0) {
		err = -errno;
		fprintf(stderr, "cannot create HWTSTAMP control socket: %s\n",
			strerror(-err));
		goto out;
	}
	err = ace_hwtstamp_capture(hwtstamp_fd, options.ifname, &hwtstamp);
	if (err) {
		fprintf(stderr, "cannot capture HWTSTAMP state on %s: %s\n",
			options.ifname, strerror(-err));
		goto out;
	}
	skel = xdp_kern__open();
	if (!skel) {
		fprintf(stderr, "failed to open BPF object\n");
		goto out;
	}
	if (bpf_map__set_max_entries(skel->maps.ingress_records,
				     options.record_capacity) ||
	    bpf_map__set_max_entries(skel->maps.cpumap_records,
				     options.record_capacity)) {
		fprintf(stderr, "failed to set measurement map capacity\n");
		goto out;
	}

	/* RX timestamp kfunc는 포팅한 macb의 xmo_rx_timestamp 구현에 연결된다.
	 * verifier가 어느 장치의 구현인지 알도록 반드시 load 전에 ifindex와
	 * DEV_BOUND_ONLY를 지정한다. attach할 때 장치를 고르는 것만으로는 늦다.
	 * 이것은 NIC 하드웨어 offload가 아니라 CPU에서 실행하는 native XDP다.
	 * CPUMAP의 두 번째 BPF는 전달받은 metadata를 읽으므로 여기에 묶지 않는다. */
	bpf_program__set_ifindex(skel->progs.xdp_dispatch, ifindex);
	err = bpf_program__set_flags(skel->progs.xdp_dispatch,
		bpf_program__flags(skel->progs.xdp_dispatch) |
		BPF_F_XDP_DEV_BOUND_ONLY);
	if (err) {
		fprintf(stderr, "failed to mark XDP program device-bound: %s\n",
			strerror(-err));
		goto out;
	}

	err = xdp_kern__load(skel);
	if (err) {
		fprintf(stderr, "failed to load device-bound BPF object: %s\n",
			strerror(-err));
		goto out;
	}

	err = validate_maps(skel, options.record_capacity);
	if (err) {
		fprintf(stderr, "BPF map validation failed: %s\n", strerror(-err));
		goto out;
	}
	prog_fd = bpf_program__fd(skel->progs.xdp_dispatch);
	if (prog_fd < 0 || (err = get_program_id(prog_fd, &owned_id))) {
		fprintf(stderr, "failed to identify loaded XDP program\n");
		goto out;
	}
	runtime.abi_version = ACE_XDP_RUNTIME_ABI_VERSION;
	runtime.ifindex = (__u32)ifindex;
	runtime.record_capacity = options.record_capacity;
	runtime.xdp_program_id = owned_id;
	runtime.owner_pid = (__u32)getpid();
	err = ace_process_start_ticks(getpid(), &runtime.owner_start_ticks);
	if (err) {
		fprintf(stderr, "cannot identify loader process lifetime: %s\n",
			strerror(-err));
		goto out;
	}
	runtime_set_hwtstamp(&runtime, &hwtstamp, false);
	err = configure_maps(skel, &runtime);
	if (err) {
		fprintf(stderr, "BPF map configuration failed: %s\n", strerror(-err));
		goto out;
	}

	/* map 내용과 실행 주체를 먼저 pin한 다음 패킷 유입을 시작한다.
	 * AF_XDP 수신기는 이 map들을 열어 같은 실험에 연결하고, loader가
	 * 비정상 종료되어도 남은 기록과 프로그램 ID로 --recover가 가능하다. */
	err = bpf_object__pin_maps(skel->obj, ACE_XDP_PIN_DIR);
	if (err) {
		fprintf(stderr, "BPF map pin failed at %s: %s\n",
			ACE_XDP_PIN_DIR, strerror(-err));
		goto out;
	}
	maps_pinned = true;
	if (stop) {
		result = 0;
		goto out;
	}

	err = bpf_xdp_attach(ifindex, prog_fd,
		XDP_FLAGS_DRV_MODE | XDP_FLAGS_UPDATE_IF_NOEXIST, NULL);
	if (err) {
		fprintf(stderr, "native XDP attach failed on %s: %s\n",
			options.ifname, strerror(-err));
		goto out;
	}
	attached = true;

	/* macb의 gem_xdp_setup()은 attach 과정에서 장치를 닫았다 다시 열어
	 * MAC timestamp mode를 초기화한다. 그래서 RX timestamp 활성화는
	 * 반드시 attach 뒤에 한다. attach 전에 켜 두기만 하면 충분하지 않다.
	 * 먼저 저장한 논리 설정과 현재 설정이 같은지도 확인한다. 달라졌다면
	 * 그 사이 들어온 설정을 복원 기준으로 삼고 이번 시작을 중단한다. */
	{
		struct hwtstamp_config current = {};

		err = ace_hwtstamp_query(hwtstamp_fd, options.ifname, &current);
		if (err) {
			fprintf(stderr, "cannot verify HWTSTAMP state on %s: %s\n",
				options.ifname, strerror(-err));
			goto out;
		}
		if (!ace_hwtstamp_config_equal(&current, &hwtstamp.saved)) {
			fprintf(stderr,
				"HWTSTAMP changed after capture; preserving the newer configuration and aborting\n");
			hwtstamp.saved = current;
			hwtstamp.effective = current;
			hwtstamp.changed = false;
			runtime_set_hwtstamp(&runtime, &hwtstamp, false);
			err = update_runtime_config(skel, &runtime);
			if (err)
				fprintf(stderr,
					"cannot publish refreshed HWTSTAMP baseline: %s\n",
					strerror(-err));
			goto out;
		}
	}

	/* Persist the intended state before SIOCSHWTSTAMP.  Recovery treats an
	 * APPLYING state as either pre-ioctl (saved) or post-ioctl (effective). */
	err = ace_hwtstamp_prepare_rx_all(&hwtstamp);
	if (err)
		goto out;
	runtime_set_hwtstamp_applying(&runtime, &hwtstamp);
	err = update_runtime_config(skel, &runtime);
	if (err) {
		fprintf(stderr, "cannot publish applying HWTSTAMP state: %s\n",
			strerror(-err));
		goto out;
	}
	err = ace_hwtstamp_apply_prepared_rx_all(hwtstamp_fd, &hwtstamp);
	if (err) {
		fprintf(stderr, "cannot enable RX hardware timestamps on %s: %s\n",
			options.ifname, strerror(-err));
		goto out;
	}
	runtime_set_hwtstamp(&runtime, &hwtstamp, true);
	err = update_runtime_config(skel, &runtime);
	if (err) {
		fprintf(stderr, "cannot publish applied HWTSTAMP state: %s\n",
			strerror(-err));
		goto out;
	}

	printf("attached native xdp_dispatch id=%u to %s; records=%u; "
	       "RX HWTSTAMP filter=%d; Ctrl-C to stop\n",
	       owned_id, options.ifname, options.record_capacity,
	       hwtstamp.effective.rx_filter);
	/* 이 준비 완료 메시지 뒤에 수신기와 송신기를 시작한다. loader의 대기
	 * 루프는 패킷당 작업을 하지 않으며 기본 quiet 모드로 출력 부하를 줄인다. */
	while (!stop) {
		sleep(1);
		if (!options.quiet)
			print_stats(bpf_map__fd(skel->maps.stats));
	}

	measurement_complete = true;
	result = 0;

out:
	/* 정상 종료는 송신기를 멈추고 수신기를 먼저 끝낸 뒤 loader를 끝낸다.
	 * STOPPING 공개 → 자신이 붙인 XDP detach → CPUMAP drain → CSV 순서다.
	 * 아직 AF_XDP 수신기가 살아 있거나 저장에 실패하면 pin을 남겨 놓는다. */
	if (attached) {
		runtime.flags |= ACE_RUNTIME_F_STOPPING;
		err = update_runtime_config(skel, &runtime);
		if (err) {
			fprintf(stderr, "cannot publish STOPPING state: %s\n",
				strerror(-err));
			result = 1;
			preserve_pins = maps_pinned;
			goto skip_detach;
		}
		err = xsk_owners_have_live_claim(
			bpf_map__fd(skel->maps.xsk_owners), &xsk_active);
		if (err) {
			fprintf(stderr,
				"cannot inspect XSK owner claims before detach: %s\n",
				strerror(-err));
			result = 1;
			preserve_pins = maps_pinned;
		} else if (xsk_active) {
			fprintf(stderr,
				"AF_XDP socket is still registered; stop the receiver before the loader\n");
			result = 1;
			preserve_pins = maps_pinned;
		} else if ((err = detach_if_owned(ifindex, prog_fd, owned_id))) {
			fprintf(stderr, "owned XDP detach failed: %s\n", strerror(-err));
			result = 1;
			preserve_pins = maps_pinned;
		} else {
			attached = false;
		}
	skip_detach:
		;
	}
	if (measurement_complete && !attached) {
		err = wait_for_cpumap_records(bpf_map__fd(skel->maps.stats), &drain);
		if (err) {
			fprintf(stderr,
				"CPUMAP drain did not complete; preserving pinned maps without exporting: %s\n",
				strerror(-err));
			result = 1;
			preserve_pins = maps_pinned;
		} else {
			if (drain.unobserved)
				fprintf(stderr,
					"warning: CPUMAP requested=%" PRIu64
					" terminal=%" PRIu64 " unobserved=%" PRIu64
					" (redirect/enqueue loss)\n",
					(uint64_t)drain.requested,
					(uint64_t)drain.terminal,
					(uint64_t)drain.unobserved);
			print_stats(bpf_map__fd(skel->maps.stats));
			print_record_summary(skel);
			err = export_measurement_files(
				bpf_map__fd(skel->maps.ingress_records),
				bpf_map__fd(skel->maps.cpumap_records),
				bpf_map__fd(skel->maps.stats), &options);
			if (err) {
				fprintf(stderr,
					"measurement export failed: %s; preserving pinned maps\n",
					strerror(-err));
				result = 1;
				preserve_pins = maps_pinned;
			}
		}
	}
	if (hwtstamp_fd >= 0 && !attached) {
		err = ace_hwtstamp_restore_allow_saved(
			hwtstamp_fd, &hwtstamp, true);
		if (err < 0) {
			fprintf(stderr, "cannot restore HWTSTAMP on %s: %s\n",
				options.ifname, strerror(-err));
			result = 1;
			preserve_pins = maps_pinned;
		} else if (err > 0) {
			fprintf(stderr,
				"warning: HWTSTAMP changed externally; not restoring stale settings\n");
			result = 1;
			preserve_pins = maps_pinned;
		} else if (preserve_pins && skel) {
			runtime_set_hwtstamp(&runtime, &hwtstamp, false);
			err = update_runtime_config(skel, &runtime);
			if (err) {
				fprintf(stderr,
					"cannot publish restored HWTSTAMP state: %s\n",
					strerror(-err));
				result = 1;
			}
		}
	} else if (hwtstamp_fd >= 0 && hwtstamp.active) {
		fprintf(stderr,
			"warning: RX HWTSTAMP remains enabled because owned XDP is still attached\n");
	}
	if (maps_pinned && !preserve_pins) {
		err = bpf_object__unpin_maps(skel->obj, ACE_XDP_PIN_DIR);
		if (err) {
			fprintf(stderr, "map unpin failed: %s\n", strerror(-err));
			result = 1;
			preserve_pins = true;
		}
	}
	if (preserve_pins)
		fprintf(stderr, "pinned recovery data remains at %s\n",
			ACE_XDP_PIN_DIR);
	else if (rmdir(ACE_XDP_PIN_DIR) && errno != ENOENT && errno != ENOTEMPTY)
		fprintf(stderr, "warning: cannot remove %s: %s\n",
			ACE_XDP_PIN_DIR, strerror(errno));
	if (hwtstamp_fd >= 0)
		close(hwtstamp_fd);
	xdp_kern__destroy(skel);
	return result;
}
