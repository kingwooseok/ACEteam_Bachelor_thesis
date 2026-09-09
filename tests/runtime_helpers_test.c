/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE

#include <linux/net_tstamp.h>
#include <stdbool.h>
#include <stdio.h>
#include <unistd.h>

#include "../include/ace_hwtstamp.h"
#include "../include/ace_process.h"

int main(void)
{
	struct ace_hwtstamp_state state = {
		.saved = {
			.flags = 7,
			.tx_type = HWTSTAMP_TX_ON,
			.rx_filter = HWTSTAMP_FILTER_PTP_V2_EVENT,
		},
		.active = true,
	};
	__u64 start_ticks;
	bool alive;
	int err;

	state.effective = state.saved;
	err = ace_hwtstamp_prepare_rx_all(&state);
	if (err || state.effective.flags != 0 ||
	    state.effective.tx_type != state.saved.tx_type ||
	    state.effective.rx_filter != HWTSTAMP_FILTER_ALL || !state.changed) {
		fputs("HWTSTAMP prepare did not preserve TX/set RX ALL\n", stderr);
		return 1;
	}

	err = ace_process_start_ticks(getpid(), &start_ticks);
	if (err || !start_ticks) {
		fputs("cannot parse this process start ticks\n", stderr);
		return 1;
	}
	err = ace_process_identity_is_alive(getpid(), start_ticks, &alive);
	if (err || !alive) {
		fputs("live process identity was not recognized\n", stderr);
		return 1;
	}
	err = ace_process_identity_is_alive(getpid(), start_ticks + 1, &alive);
	if (err || alive) {
		fputs("mismatched process generation was accepted\n", stderr);
		return 1;
	}

	puts("runtime helpers: HWTSTAMP intent and PID generation verified");
	return 0;
}
