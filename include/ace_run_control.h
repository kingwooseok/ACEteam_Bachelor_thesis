/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_RUN_CONTROL_H
#define ACE_RUN_CONTROL_H

/* Userspace run-control helpers. Call file helpers outside measurement loops. */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static inline int ace_parse_u64(const char *text, uint64_t minimum,
		uint64_t maximum, uint64_t *value)
{
	char *end;
	unsigned long long parsed;
	if (!text || !*text)
		return -1;
	for (const unsigned char *p = (const unsigned char *)text; *p; p++)
		if (*p < '0' || *p > '9')
			return -1;
	errno = 0;
	parsed = strtoull(text, &end, 10);
	if (errno || *end || parsed < minimum || parsed > maximum)
		return -1;
	*value = (uint64_t)parsed;
	return 0;
}

static inline int64_t ace_mono_ns(void)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts))
		return -1;
	return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static inline FILE *ace_open_exclusive_stream(const char *path)
{
	int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
	FILE *stream;
	if (fd < 0)
		return NULL;
	stream = fdopen(fd, "w");
	if (!stream) {
		int saved = errno;
		close(fd);
		unlink(path);
		errno = saved;
	}
	return stream;
}

static inline int ace_finish_stream(FILE *stream)
{
	int error = 0;
	if (fflush(stream) || ferror(stream))
		error = errno ? errno : EIO;
	if (!error && fsync(fileno(stream)))
		error = errno;
	if (fclose(stream) && !error)
		error = errno;
	if (error) {
		errno = error;
		return -1;
	}
	return 0;
}

/* Publish complete JSON atomically without overwriting a previous run's marker.
 * role must be a fixed application identifier, not user-provided JSON text.
 * Markers remain as run artifacts; readers must also check process liveness. */
static inline int ace_publish_ready(const char *path, const char *role)
{
	char temporary[PATH_MAX];
	FILE *stream;
	int fd, error = 0;
	int64_t now;
	if (fflush(stdout))
		return -1;
	if (!path)
		return 0;
	if (snprintf(temporary, sizeof(temporary), "%s.tmp.XXXXXX", path) >=
	    (int)sizeof(temporary)) {
		errno = ENAMETOOLONG;
		return -1;
	}
	fd = mkstemp(temporary);
	if (fd < 0)
		return -1;
	/* A privileged receiver may be supervised by the run-directory owner. */
	if (fchmod(fd, 0644)) {
		error = errno;
		close(fd);
		goto out;
	}
	stream = fdopen(fd, "w");
	if (!stream) {
		error = errno;
		close(fd);
		goto out;
	}
	now = ace_mono_ns();
	if (now < 0) {
		error = errno;
		fclose(stream);
		goto out;
	}
	if (fprintf(stream, "{\"schema_version\":1,\"role\":\"%s\","
		"\"pid\":%ld,\"ready_mono_ns\":%lld}\n", role,
		(long)getpid(), (long long)now) < 0) {
		error = errno ? errno : EIO;
		fclose(stream);
		goto out;
	}
	if (ace_finish_stream(stream)) {
		error = errno;
		goto out;
	}
	if (link(temporary, path))
		error = errno;
out:
	unlink(temporary);
	if (error) {
		errno = error;
		return -1;
	}
	return 0;
}
#endif
