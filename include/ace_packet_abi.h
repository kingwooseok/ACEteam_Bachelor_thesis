/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ACE_PACKET_ABI_H
#define ACE_PACKET_ABI_H

/*
 * Wire ABI shared by the BPF parser and UDP sender/receivers.  vmlinux.h
 * already supplies these types to the BPF translation unit; ordinary
 * userspace gets the UAPI definitions from linux/types.h.
 */
#ifndef __VMLINUX_H__
#include <linux/types.h>
#endif

#define ACE_PACKET_MAGIC          0x41434531U /* "ACE1" after BE conversion */
#define ACE_PACKET_VERSION        1U
#define ACE_PACKET_HEADER_SIZE    32U

/* Every member is encoded in network (big-endian) byte order. */
struct ace_experiment_header {
	__be32 magic;
	__be16 version;
	__be16 size;
	__be32 flow_id;
	__be32 reserved;
	__be64 sequence;
	__be64 tx_realtime_ns;
};

_Static_assert(sizeof(struct ace_experiment_header) == ACE_PACKET_HEADER_SIZE,
	"experiment packet header ABI changed");

#endif /* ACE_PACKET_ABI_H */
