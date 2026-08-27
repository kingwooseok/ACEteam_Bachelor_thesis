# SPDX-License-Identifier: GPL-2.0
# BPF/Makefile — BTF 생성부터 userspace loader 빌드까지의 파이프라인
#
# make가 수행하는 단계:
#   vmlinux.h → xdp_kern.o → xdp_kern.skel.h → xdp_loader

# ===== 도구 =====
# 필요하면 명령행에서 경로를 재정의할 수 있다(예: make CLANG=clang-19).
CLANG   ?= clang          # BPF bytecode 컴파일러
CC      ?= cc             # userspace loader 컴파일러
BPFTOOL ?= bpftool        # BTF dump 및 skeleton 생성 도구
CFLAGS  ?= -O2 -g -Wall -Wextra

# AF_XDP ring/socket API는 libbpf 1.0부터 libxdp로 분리되었다.
# libxdp가 설치된 경우에만 afxdp_recv 타깃을 활성화한다.
LIBXDP_CFLAGS := $(shell pkg-config --cflags libxdp 2>/dev/null)
LIBXDP_LIBS   := $(shell pkg-config --libs libxdp 2>/dev/null)

# ===== BPF target architecture =====
# CO-RE가 사용할 대상 아키텍처를 빌드 머신에 맞춰 지정한다.
ARCH := $(shell uname -m)
ifeq ($(ARCH),aarch64)
  BPF_TARGET_ARCH := -D__TARGET_ARCH_arm64    # Raspberry Pi 5 등 ARM64
else ifeq ($(ARCH),x86_64)
  BPF_TARGET_ARCH := -D__TARGET_ARCH_x86      # x86_64 개발 머신
else
endif

all: xdp_loader

vmlinux.h:
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

xdp_kern.o: xdp_kern.c vmlinux.h
	$(CLANG) -target bpf $(BPF_TARGET_ARCH) -O2 -g -Wall -Wextra -c $< -o $@

xdp_kern.skel.h: xdp_kern.o
	$(BPFTOOL) gen skeleton $< > $@

xdp_loader: xdp_loader.c xdp_kern.skel.h pin_paths.h
	$(CC) $(CFLAGS) $< -o $@ $$(pkg-config --cflags --libs libbpf)

ifeq ($(strip $(LIBXDP_LIBS)),)
afxdp_recv:
	@echo "libxdp-dev is required to build afxdp_recv (AF_XDP socket API)" >&2
	@exit 1
else
afxdp_recv: afxdp_recv.c pin_paths.h
	$(CC) $(CFLAGS) $(LIBXDP_CFLAGS) $< -o $@ $(LIBXDP_LIBS) $$(pkg-config --cflags --libs libbpf)
endif

afxdp: xdp_loader afxdp_recv

clean:
	rm -f xdp_kern.o xdp_kern.skel.h xdp_loader afxdp_recv vmlinux.h

.PHONY: all clean afxdp
