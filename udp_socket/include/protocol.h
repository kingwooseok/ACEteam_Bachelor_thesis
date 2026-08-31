#ifndef ACE_UDP_PROTOCOL_H
#define ACE_UDP_PROTOCOL_H

#include <stdint.h>

/* Multi-byte packet fields are transmitted in network byte order. */
struct packet_header {
	uint64_t seq;
	int64_t tx_ns;
};

/* samples.bin stores consecutive records in the receiver's native byte order. */
struct sample_record {
	uint64_t seq;
	int64_t tx_ns;
	int64_t rx_ns;
	uint32_t len;
	uint16_t flags;
	uint16_t reserved;
};

_Static_assert(sizeof(struct packet_header) == 16,
	"packet_header layout changed");
_Static_assert(sizeof(struct sample_record) == 32,
	"sample_record layout changed");

#endif
