/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_PROCESS_H
#define ACE_PROCESS_H

#include <errno.h>
#include <fcntl.h>
#include <linux/types.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

/* Linux /proc/PID/stat field 22.  PID plus start ticks detects PID reuse. */
static inline int ace_process_start_ticks(pid_t pid, __u64 *start_ticks)
{
	char buffer[4096];
	char path[64];
	char *cursor;
	ssize_t length;
	int saved_errno;
	int fd;

	if (pid <= 0 || !start_ticks)
		return -EINVAL;
	if (snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid) >=
	    (int)sizeof(path))
		return -ENAMETOOLONG;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -errno;
	do {
		length = read(fd, buffer, sizeof(buffer) - 1);
	} while (length < 0 && errno == EINTR);
	saved_errno = errno;
	if (close(fd) && length >= 0)
		return -errno;
	if (length < 0)
		return -saved_errno;
	if (length == 0 || (size_t)length == sizeof(buffer) - 1)
		return -EOVERFLOW;
	buffer[length] = '\0';

	/* comm (field 2) may contain spaces and parentheses; its final ')' is
	 * the reliable delimiter before the remaining fields. */
	cursor = strrchr(buffer, ')');
	if (!cursor)
		return -EINVAL;
	cursor++;
	for (unsigned int field = 3; field <= 22; field++) {
		char *end;

		while (*cursor == ' ')
			cursor++;
		if (!*cursor)
			return -EINVAL;
		end = cursor;
		while (*end && *end != ' ' && *end != '\n')
			end++;
		if (field == 22) {
			char *parsed_end;
			unsigned long long parsed;

			errno = 0;
			parsed = strtoull(cursor, &parsed_end, 10);
			if (errno || parsed_end != end)
				return -EINVAL;
			*start_ticks = (__u64)parsed;
			return 0;
		}
		cursor = end;
	}
	return -EINVAL;
}

static inline int ace_process_identity_is_alive(pid_t pid,
		__u64 expected_start_ticks, bool *alive)
{
	__u64 actual_start_ticks;
	int err;

	if (!alive || pid <= 0 || !expected_start_ticks)
		return -EINVAL;
	*alive = false;
	err = ace_process_start_ticks(pid, &actual_start_ticks);
	if (err == -ENOENT || err == -ESRCH)
		return 0;
	if (err)
		return err;
	*alive = actual_start_ticks == expected_start_ticks;
	return 0;
}

#endif /* ACE_PROCESS_H */
