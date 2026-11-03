# 최상위 Makefile — 서로 독립적인 UDP와 BPF 하위 빌드를 연결함.

.PHONY: all clean bpf udp afxdp experiment test

all: bpf udp experiment

bpf:
	$(MAKE) -C BPF

clean:
	$(MAKE) -C BPF clean
	$(MAKE) -C udp_socket clean
	$(MAKE) -C experiment clean

udp:
	$(MAKE) -C udp_socket

experiment: udp
	$(MAKE) -C experiment

# pin된 map을 사용하는 XDP loader와 AF_XDP receiver를 함께 빌드함.
afxdp:
	$(MAKE) -C BPF afxdp

test:
	./tests/run_offline.sh
