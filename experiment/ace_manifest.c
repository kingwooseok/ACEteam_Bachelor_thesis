/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include "ace_manifest.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CAPTURE_LIMIT (2U * 1024U * 1024U)
#define COMMAND_TIMEOUT_MS 10000

struct artifact {
	char *path;
	char digest[65];
	int64_t bytes;
	bool missing;
};

static int64_t monotonic_ms(void)
{
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now))
		return -1;
	return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

/* No shell or command concatenation. A bounded capture prevents a stalled
 * external utility from leaving preparation hung indefinitely. */
static int capture(char *const argv[], int input_fd, char **text)
{
	int pipes[2], status = 0, saved = 0;
	pid_t child, waited;
	char *buffer = NULL;
	size_t used = 0, capacity = 4096;
	int64_t deadline = monotonic_ms();
	bool eof = false;

	*text = NULL;
	if (deadline < 0)
		return -1;
	deadline += COMMAND_TIMEOUT_MS;
	if (pipe2(pipes, O_CLOEXEC))
		return -1;
	child = fork();
	if (child < 0) {
		saved = errno;
		close(pipes[0]);
		close(pipes[1]);
		errno = saved;
		return -1;
	}
	if (!child) {
		int null_fd = open("/dev/null", O_RDWR);
		if (null_fd < 0 || dup2(pipes[1], STDOUT_FILENO) < 0 ||
		    dup2(input_fd >= 0 ? input_fd : null_fd, STDIN_FILENO) < 0 ||
		    dup2(null_fd, STDERR_FILENO) < 0)
			_exit(126);
		close(pipes[0]);
		close(pipes[1]);
		if (null_fd > STDERR_FILENO)
			close(null_fd);
		if (input_fd > STDERR_FILENO)
			close(input_fd);
		setenv("LC_ALL", "C", 1);
		execvp(argv[0], argv);
		_exit(127);
	}
	close(pipes[1]);
	buffer = malloc(capacity);
	if (!buffer) {
		saved = errno;
		goto fail;
	}
	while (!eof) {
		struct pollfd pfd = { .fd = pipes[0], .events = POLLIN };
		int64_t now = monotonic_ms();
		ssize_t count;
		int result;
		if (now < 0 || now >= deadline) {
			saved = now < 0 ? errno : ETIMEDOUT;
			goto fail;
		}
		result = poll(&pfd, 1, (int)(deadline - now));
		if (result < 0 && errno == EINTR)
			continue;
		if (result <= 0) {
			saved = result == 0 ? ETIMEDOUT : errno;
			goto fail;
		}
		if (used + 1 == capacity) {
			char *larger;
			if (capacity >= CAPTURE_LIMIT) {
				saved = EFBIG;
				goto fail;
			}
			capacity *= 2;
			larger = realloc(buffer, capacity);
			if (!larger) {
				saved = errno;
				goto fail;
			}
			buffer = larger;
		}
		count = read(pipes[0], buffer + used, capacity - used - 1);
		if (count < 0 && errno == EINTR)
			continue;
		if (count < 0) {
			saved = errno;
			goto fail;
		}
		if (!count)
			eof = true;
		used += (size_t)count;
	}
	for (;;) {
		int64_t now;
		waited = waitpid(child, &status, WNOHANG);
		if (waited == child)
			break;
		if (waited < 0 && errno == EINTR)
			continue;
		if (waited < 0) {
			saved = errno;
			goto fail;
		}
		now = monotonic_ms();
		if (now < 0 || now >= deadline) {
			saved = now < 0 ? errno : ETIMEDOUT;
			goto fail;
		}
		(void)poll(NULL, 0, 10);
	}
	close(pipes[0]);
	if (!WIFEXITED(status) || WEXITSTATUS(status)) {
		free(buffer);
		errno = WIFEXITED(status) && WEXITSTATUS(status) == 127 ? ENOENT : EIO;
		return -1;
	}
	buffer[used] = '\0';
	*text = buffer;
	return 0;
fail:
	close(pipes[0]);
	(void)kill(child, SIGKILL);
	while (waitpid(child, &status, 0) < 0 && errno == EINTR)
		;
	free(buffer);
	errno = saved ? saved : EIO;
	return -1;
}

static char *read_optional_text(const char *path)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	char buffer[65536];
	size_t used = 0;
	if (fd < 0)
		return NULL;
	for (;;) {
		ssize_t count = read(fd, buffer + used, sizeof(buffer) - used - 1);
		if (count < 0 && errno == EINTR)
			continue;
		if (count < 0 || used + (size_t)count >= sizeof(buffer) - 1) {
			close(fd);
			return NULL;
		}
		if (!count)
			break;
		used += (size_t)count;
	}
	close(fd);
	while (used && (buffer[used - 1] == '\n' || buffer[used - 1] == '\r'))
		used--;
	buffer[used] = '\0';
	return strdup(buffer);
}

/* UTF-8 input -> ASCII JSON, including surrogate pairs for supplementary
 * codepoints. Reject invalid UTF-8 instead of silently changing a path. */
static int json_string(FILE *file, const char *value)
{
	const unsigned char *p = (const unsigned char *)value;
	if (!value)
		return fputs("null", file) == EOF ? -1 : 0;
	if (fputc('"', file) == EOF)
		return -1;
	while (*p) {
		uint32_t cp;
		unsigned continuation;
		if (*p < 0x80) {
			cp = *p++;
			if (cp == '"' || cp == '\\') {
				if (fputc('\\', file) == EOF || fputc((int)cp, file) == EOF)
					return -1;
			} else if (cp < 0x20) {
				if (fprintf(file, "\\u%04x", cp) < 0)
					return -1;
			} else if (fputc((int)cp, file) == EOF) {
				return -1;
			}
			continue;
		}
		if (*p >= 0xc2 && *p <= 0xdf) {
			cp = *p++ & 0x1f;
			continuation = 1;
		} else if (*p >= 0xe0 && *p <= 0xef) {
			cp = *p++ & 0x0f;
			continuation = 2;
		} else if (*p >= 0xf0 && *p <= 0xf4) {
			cp = *p++ & 0x07;
			continuation = 3;
		} else {
			errno = EILSEQ;
			return -1;
		}
		for (unsigned i = 0; i < continuation; i++) {
			if ((*p & 0xc0) != 0x80) {
				errno = EILSEQ;
				return -1;
			}
			cp = (cp << 6) | (*p++ & 0x3f);
		}
		if ((continuation == 1 && cp < 0x80) ||
		    (continuation == 2 && cp < 0x800) ||
		    (continuation == 3 && cp < 0x10000) ||
		    cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) {
			errno = EILSEQ;
			return -1;
		}
		if (cp <= 0xffff) {
			if (fprintf(file, "\\u%04x", cp) < 0)
				return -1;
		} else {
			cp -= 0x10000;
			if (fprintf(file, "\\u%04x\\u%04x", 0xd800 + (cp >> 10),
				    0xdc00 + (cp & 0x3ff)) < 0)
				return -1;
		}
	}
	return fputc('"', file) == EOF ? -1 : 0;
}

static int artifact_digest(struct artifact *artifact)
{
	struct stat before, after;
	char *result = NULL;
	char *const argv[] = { "sha256sum", NULL };
	int fd = open(artifact->path, O_RDONLY | O_CLOEXEC), saved;
	if (fd < 0 && errno == ENOENT) {
		/* Role-specific executable requirements are checked by the runner. */
		artifact->missing = true;
		return 0;
	}
	if (fd < 0)
		return -1;
	if (fstat(fd, &before)) {
		saved = errno;
		close(fd);
		errno = saved;
		return -1;
	}
	if (!S_ISREG(before.st_mode)) {
		close(fd);
		errno = EINVAL;
		return -1;
	}
	if (capture(argv, fd, &result) || fstat(fd, &after)) {
		saved = errno;
		close(fd);
		free(result);
		errno = saved;
		return -1;
	}
	close(fd);
	if (before.st_size != after.st_size ||
	    before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
	    before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
	    before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
	    before.st_ctim.tv_nsec != after.st_ctim.tv_nsec) {
		free(result);
		errno = ESTALE;
		return -1;
	}
	if (strlen(result) < 65 || result[64] != ' ') {
		free(result);
		errno = EIO;
		return -1;
	}
	for (size_t i = 0; i < 64; i++) {
		if (!((result[i] >= '0' && result[i] <= '9') ||
		      (result[i] >= 'a' && result[i] <= 'f'))) {
			free(result);
			errno = EIO;
			return -1;
		}
	}
	memcpy(artifact->digest, result, 64);
	artifact->digest[64] = '\0';
	artifact->bytes = before.st_size;
	free(result);
	return 0;
}

static int valid_run_id(const char *value)
{
	size_t length;
	if (!value || !(length = strlen(value)) || length > 128)
		return 0;
	for (size_t i = 0; i < length; i++) {
		unsigned char c = (unsigned char)value[i];
		bool alnum = (c >= 'A' && c <= 'Z') ||
			(c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
		if (!alnum && (!i || (c != '.' && c != '_' && c != '-')))
			return 0;
	}
	return 1;
}

int ace_write_manifest(const char *output, const char *run_id,
		const char *condition_id, const char *interface,
		const char *repo, const char *condition_json)
{
	static const char *const binaries[] = {
		"BPF/xdp_loader", "BPF/afxdp_recv", "udp_socket/bin/sender",
		"udp_socket/bin/receiver", "udp_socket/bin/phc_calibrate",
		"udp_socket/bin/ace_clock", "experiment/ace-run",
	};
	struct artifact artifacts[sizeof(binaries) / sizeof(binaries[0])] = {0};
	struct utsname system_info;
	struct stat existing;
	char *repository = NULL, *head = NULL, *status = NULL, *temporary = NULL;
	char *cmdline = NULL, *realtime = NULL, *online = NULL, *isolated = NULL;
	FILE *file = NULL;
	int fd = -1, result = -1, saved = EIO;

	if (!output || !*output || !valid_run_id(run_id) || !condition_id ||
	    !interface || !*interface || !repo || !*repo || !condition_json ||
	    !*condition_json) {
		errno = EINVAL;
		return -1;
	}
	if (!lstat(output, &existing)) {
		errno = EEXIST;
		return -1;
	}
	if (errno != ENOENT)
		return -1;
	repository = realpath(repo, NULL);
	if (!repository || uname(&system_info))
		goto out;
	{
		char *const argv_head[] = { "git", "--no-optional-locks", "--no-pager",
			"-C", repository, "rev-parse", "--verify", "HEAD", NULL };
		char *const argv_status[] = { "git", "--no-optional-locks", "--no-pager",
			"-C", repository, "-c", "core.fsmonitor=false", "status",
			"--porcelain=v1", "--untracked-files=normal", NULL };
		if (capture(argv_head, -1, &head) || capture(argv_status, -1, &status))
			goto out;
		for (size_t n = strlen(head); n && (head[n - 1] == '\n' || head[n - 1] == '\r'); n--)
			head[n - 1] = '\0';
	}
	for (size_t i = 0; i < sizeof(artifacts) / sizeof(artifacts[0]); i++) {
		if (asprintf(&artifacts[i].path, "%s/%s", repository, binaries[i]) < 0)
			goto out;
		if (artifact_digest(&artifacts[i]))
			goto out;
	}
	cmdline = read_optional_text("/proc/cmdline");
	realtime = read_optional_text("/sys/kernel/realtime");
	online = read_optional_text("/sys/devices/system/cpu/online");
	isolated = read_optional_text("/sys/devices/system/cpu/isolated");
	if (asprintf(&temporary, "%s.tmp.XXXXXX", output) < 0)
		goto out;
	fd = mkstemp(temporary);
	if (fd < 0 || fchmod(fd, 0644))
		goto out;
	file = fdopen(fd, "w");
	if (!file)
		goto out;
	fd = -1;
	/* Only condition_json is caller-supplied validated JSON. Every string,
	 * including native command output, passes through the JSON encoder. */
#define FIELD(name, value) do { \
	if (fputs("\"" name "\":", file) == EOF || json_string(file, value)) goto out; \
} while (0)
	if (fputs("{\n\"manifest_version\":1,", file) == EOF)
		goto out;
	FIELD("run_id", run_id);
	fputc(',', file);
	FIELD("condition_id", condition_id);
	if (fprintf(file, ",\n\"parameters\":%s,\n\"interface\":{", condition_json) < 0)
		goto out;
	FIELD("name", interface);
	fputs("},\n\"system\":{", file);
	FIELD("kernel_release", system_info.release);
	fputc(',', file);
	FIELD("architecture", system_info.machine);
	fputc(',', file);
	FIELD("cmdline", cmdline);
	fputc(',', file);
	FIELD("realtime", realtime);
	fputc(',', file);
	FIELD("cpu_online", online);
	fputc(',', file);
	FIELD("cpu_isolated", isolated);
	fputs("},\n\"project_repository\":{", file);
	FIELD("path", repository);
	fputc(',', file);
	FIELD("head", head);
	fputc(',', file);
	FIELD("status", status);
	fputs("},\n\"artifact_hashes\":[", file);
	for (size_t i = 0; i < sizeof(artifacts) / sizeof(artifacts[0]); i++) {
		if (i)
			fputc(',', file);
		fputc('{', file);
		FIELD("path", artifacts[i].path);
		if (artifacts[i].missing) {
			fputs(",\"missing\":true}", file);
			continue;
		}
		if (fprintf(file, ",\"bytes\":%" PRId64 ",", artifacts[i].bytes) < 0)
			goto out;
		FIELD("sha256", artifacts[i].digest);
		fputc('}', file);
	}
	if (fputs("]\n}\n", file) == EOF || fflush(file) || ferror(file) || fsync(fileno(file)))
		goto out;
	if (fclose(file)) {
		file = NULL;
		goto out;
	}
	file = NULL;
	/* link is atomic and fails with EEXIST; rename would replace old data. */
	if (link(temporary, output))
		goto out;
	result = 0;
out:
	saved = errno ? errno : EIO;
	if (file)
		fclose(file);
	if (fd >= 0)
		close(fd);
	if (temporary)
		unlink(temporary);
	free(temporary);
	free(repository);
	free(head);
	free(status);
	free(cmdline);
	free(realtime);
	free(online);
	free(isolated);
	for (size_t i = 0; i < sizeof(artifacts) / sizeof(artifacts[0]); i++)
		free(artifacts[i].path);
	if (result)
		errno = saved;
	return result;
#undef FIELD
}
