# Top-level Makefile — delegates to BPF/ sub-directory.

.PHONY: all clean

all:
	$(MAKE) -C BPF

clean:
	$(MAKE) -C BPF clean
