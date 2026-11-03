/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_AFXDP_RUN_CONTROL_H
#define ACE_AFXDP_RUN_CONTROL_H
#include <stdbool.h>
#include <stdint.h>

/* One fixed deadline, not an inactivity timer: continuous incoming traffic
 * cannot postpone shutdown. A second signal does not reset the deadline. */
struct ace_xsk_drain {
	bool active;
	int64_t deadline_ns;
};

static inline void ace_xsk_drain_begin(struct ace_xsk_drain *drain,
		int64_t now_ns, uint32_t duration_ms)
{
	if (!drain->active) {
		drain->active = true;
		drain->deadline_ns = now_ns + (int64_t)duration_ms * 1000000;
	}
}

/* -1 means the total drain window ended; otherwise return a bounded poll. */
static inline int ace_xsk_drain_poll_ms(const struct ace_xsk_drain *drain,
		int64_t now_ns)
{
	int64_t remaining;
	if (!drain->active)
		return 1000;
	remaining = drain->deadline_ns - now_ns;
	if (remaining <= 0)
		return -1;
	if (remaining >= 1000000000)
		return 1000;
	return (int)((remaining + 999999) / 1000000);
}
#endif
