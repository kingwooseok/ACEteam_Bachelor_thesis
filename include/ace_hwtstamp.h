/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_HWTSTAMP_H
#define ACE_HWTSTAMP_H

#include <errno.h>
#include <linux/net_tstamp.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <stdbool.h>
#include <string.h>
#include <sys/ioctl.h>

/* State needed to restore a netdevice-wide timestamp configuration. */
struct ace_hwtstamp_state {
	struct hwtstamp_config saved;
	struct hwtstamp_config effective;
	char interface_name[IFNAMSIZ];
	bool active;
	bool changed;
};

static inline bool ace_hwtstamp_config_equal(
		const struct hwtstamp_config *left,
		const struct hwtstamp_config *right)
{
	return left->flags == right->flags &&
		left->tx_type == right->tx_type &&
		left->rx_filter == right->rx_filter;
}

static inline int ace_hwtstamp_query(int socket_fd,
		const char *interface_name, struct hwtstamp_config *config)
{
	struct ifreq request = {0};
	size_t length;

	if (!interface_name || !interface_name[0])
		return -EINVAL;
	length = strlen(interface_name);
	if (length >= sizeof(request.ifr_name))
		return -ENAMETOOLONG;
	memcpy(request.ifr_name, interface_name, length + 1);
	request.ifr_data = (void *)config;
	if (ioctl(socket_fd, SIOCGHWTSTAMP, &request))
		return -errno;
	return 0;
}

/* Capture the netdevice-wide state before an operation such as XDP attach can
 * reset the MAC.  A captured state can always be passed to restore(). */
static inline int ace_hwtstamp_capture(int socket_fd,
		const char *interface_name, struct ace_hwtstamp_state *state)
{
	size_t length;
	int err;

	if (!interface_name || !interface_name[0])
		return -EINVAL;
	memset(state, 0, sizeof(*state));
	length = strlen(interface_name);
	if (length >= sizeof(state->interface_name))
		return -ENAMETOOLONG;
	memcpy(state->interface_name, interface_name, length + 1);
	err = ace_hwtstamp_query(socket_fd, interface_name, &state->saved);
	if (err)
		return err;

	state->effective = state->saved;
	state->active = true;
	return 0;
}

/* Prepare the exact configuration that will be persisted as APPLYING before
 * the ioctl.  This closes the crash window between changing the device and
 * publishing enough state to restore it. */
static inline int ace_hwtstamp_prepare_rx_all(
		struct ace_hwtstamp_state *state)
{
	if (!state->active)
		return -EINVAL;
	state->effective = state->saved;
	state->effective.flags = 0;
	state->effective.rx_filter = HWTSTAMP_FILTER_ALL;
	state->changed = !ace_hwtstamp_config_equal(&state->saved,
						    &state->effective);
	return 0;
}

/* Apply a prepared FILTER_ALL configuration.  Re-read immediately before
 * SIOCSHWTSTAMP so a concurrent ptp4l/admin change is never knowingly
 * overwritten using the stale captured TX mode. */
static inline int ace_hwtstamp_apply_prepared_rx_all(int socket_fd,
		struct ace_hwtstamp_state *state)
{
	struct hwtstamp_config current = {0};
	struct hwtstamp_config desired;
	struct ifreq request = {0};
	size_t length;
	int err;

	if (!state->active ||
	    state->effective.rx_filter != HWTSTAMP_FILTER_ALL)
		return -EINVAL;
	err = ace_hwtstamp_query(socket_fd, state->interface_name, &current);
	if (err)
		return err;
	if (!ace_hwtstamp_config_equal(&current, &state->saved))
		return -ESTALE;

	desired = state->effective;
	length = strlen(state->interface_name);
	memcpy(request.ifr_name, state->interface_name, length + 1);
	request.ifr_data = (void *)&desired;
	if (ioctl(socket_fd, SIOCSHWTSTAMP, &request))
		return -errno;

	/* SIOCSHWTSTAMP may normalize the requested filter in-place. */
	state->effective = desired;
	state->changed = !ace_hwtstamp_config_equal(&state->saved, &desired);
	if (desired.rx_filter != HWTSTAMP_FILTER_ALL) {
		request.ifr_data = (void *)&state->saved;
		if (ioctl(socket_fd, SIOCSHWTSTAMP, &request))
			return -errno;
		state->changed = false;
		state->effective = state->saved;
		return -EOPNOTSUPP;
	}
	return 0;
}

/* Apply RX FILTER_ALL using an already captured state.  Keeping capture and
 * apply separate lets the XDP loader save the pre-attach configuration. */
static inline int ace_hwtstamp_apply_rx_all(int socket_fd,
		struct ace_hwtstamp_state *state)
{
	int err = ace_hwtstamp_prepare_rx_all(state);

	return err ? err : ace_hwtstamp_apply_prepared_rx_all(socket_fd, state);
}

/* Enable RX hardware timestamps while preserving the existing TX mode (for
 * example one selected by ptp4l).  Returns zero or a negative errno value. */
static inline int ace_hwtstamp_enable(int socket_fd, const char *interface_name,
		struct ace_hwtstamp_state *state)
{
	int err;

	err = ace_hwtstamp_capture(socket_fd, interface_name, state);
	return err ? err : ace_hwtstamp_apply_rx_all(socket_fd, state);
}

/*
 * Restore only if no other process changed the netdevice-wide setting since
 * we installed it.  Returns 1 when restoration is intentionally skipped, 0
 * on success/no-op, or a negative errno value on ioctl failure.
 */
static inline int ace_hwtstamp_restore_allow_saved(int socket_fd,
		struct ace_hwtstamp_state *state, bool allow_saved)
{
	struct hwtstamp_config current = {0};
	struct ifreq request = {0};
	size_t length;

	if (!state->active)
		return 0;
	length = strlen(state->interface_name);
	memcpy(request.ifr_name, state->interface_name, length + 1);
	request.ifr_data = (void *)&current;
	if (ioctl(socket_fd, SIOCGHWTSTAMP, &request))
		return -errno;
	if (!ace_hwtstamp_config_equal(&current, &state->effective) &&
	    !(allow_saved &&
	      ace_hwtstamp_config_equal(&current, &state->saved)))
		return 1;
	/* Reapply even when the logical config was unchanged: an XDP attach or
	 * detach may have reset the MAC while leaving SIOCGHWTSTAMP state intact. */
	request.ifr_data = (void *)&state->saved;
	if (ioctl(socket_fd, SIOCSHWTSTAMP, &request))
		return -errno;
	state->effective = state->saved;
	state->changed = false;
	state->active = false;
	return 0;
}

static inline int ace_hwtstamp_restore(int socket_fd,
		struct ace_hwtstamp_state *state)
{
	return ace_hwtstamp_restore_allow_saved(socket_fd, state, false);
}

#endif /* ACE_HWTSTAMP_H */
