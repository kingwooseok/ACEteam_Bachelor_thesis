/* SPDX-License-Identifier: GPL-2.0 */
#define _DEFAULT_SOURCE

#include <arpa/inet.h>
#include <endian.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* Include both public views in one translation unit.  They must agree. */
#include "../BPF/metadata.h"
#include "../udp_socket/include/protocol.h"

_Static_assert(ACE_PACKET_HEADER_SIZE == 32, "wire header is not 32 bytes");
_Static_assert(sizeof(struct ace_experiment_header) == 32,
	"wire header layout differs from its declared size");
_Static_assert(offsetof(struct ace_experiment_header, magic) == 0,
	"magic offset changed");
_Static_assert(offsetof(struct ace_experiment_header, version) == 4,
	"version offset changed");
_Static_assert(offsetof(struct ace_experiment_header, size) == 6,
	"size offset changed");
_Static_assert(offsetof(struct ace_experiment_header, flow_id) == 8,
	"flow_id offset changed");
_Static_assert(offsetof(struct ace_experiment_header, reserved) == 12,
	"reserved offset changed");
_Static_assert(offsetof(struct ace_experiment_header, sequence) == 16,
	"sequence offset changed");
_Static_assert(offsetof(struct ace_experiment_header, tx_realtime_ns) == 24,
	"tx timestamp offset changed");

int main(void)
{
	const unsigned char expected[ACE_PACKET_HEADER_SIZE] = {
		0x41, 0x43, 0x45, 0x31,
		0x00, 0x01,
		0x00, 0x20,
		0x01, 0x02, 0x03, 0x04,
		0x00, 0x00, 0x00, 0x00,
		0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
		0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18,
	};
	struct ace_experiment_header header = {
		.magic = htobe32(ACE_PACKET_MAGIC),
		.version = htobe16(ACE_PACKET_VERSION),
		.size = htobe16(ACE_PACKET_HEADER_SIZE),
		.flow_id = htobe32(UINT32_C(0x01020304)),
		.reserved = 0,
		.sequence = htobe64(UINT64_C(0x0102030405060708)),
		.tx_realtime_ns = htobe64(UINT64_C(0x1112131415161718)),
	};

	if (memcmp(&header, expected, sizeof(expected)) != 0) {
		fputs("wire header byte order/layout mismatch\n", stderr);
		return 1;
	}

	puts("wire ABI: 32-byte shared header and network byte order verified");
	return 0;
}
