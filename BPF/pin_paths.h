/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_XDP_PIN_PATHS_H
#define ACE_XDP_PIN_PATHS_H

/* xdp_loader와 userspace consumer가 공유하는 bpffs 경로. */
#define ACE_XDP_PIN_DIR       "/sys/fs/bpf/ace_xdp"
#define ACE_XDP_XSK_MAP_PIN   ACE_XDP_PIN_DIR "/xsk_map"
#define ACE_XDP_STATS_MAP_PIN ACE_XDP_PIN_DIR "/stats"

#endif
