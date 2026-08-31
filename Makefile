# Top-level Makefile — delegates to the independent experiment sub-directories.

.PHONY: all clean udp afxdp

all:
	$(MAKE) -C BPF

clean:
	$(MAKE) -C BPF clean
	$(MAKE) -C udp_socket clean

udp:
	$(MAKE) -C udp_socket

# Build the pinned-map XDP loader and the AF_XDP receiver.
afxdp:
	$(MAKE) -C BPF afxdp
