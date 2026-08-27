# Top-level Makefile — delegates to BPF/ sub-directory.

.PHONY: all clean afxdp

all:
	$(MAKE) -C BPF

clean:
	$(MAKE) -C BPF clean

# Build the pinned-map XDP loader and the AF_XDP receiver.
afxdp:
	$(MAKE) -C BPF afxdp
