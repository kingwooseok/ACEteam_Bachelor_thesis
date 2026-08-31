# SPDX-License-Identifier: GPL-2.0
# BPF/Makefile — BTF 생성부터 userspace loader 빌드까지의 파이프라인
#
# make가 수행하는 단계:
#   vmlinux.h → xdp_kern.o → xdp_kern.skel.h → xdp_loader

# ===== 빌드 도구와 컴파일 옵션 =====
# 필요하면 명령행에서 재정의할 수 있다(예: make CLANG=clang-19).
CLANG      ?= clang
CC         ?= cc
BPFTOOL    ?= bpftool
PKG_CONFIG ?= pkg-config

CFLAGS ?= -O2 -g
WARNINGS := -Wall -Wextra
BPF_CFLAGS ?= -O2 -g

LIBBPF_CFLAGS := $(shell $(PKG_CONFIG) --cflags libbpf 2>/dev/null)
LIBBPF_LIBS   := $(shell $(PKG_CONFIG) --libs libbpf 2>/dev/null)
LIBXDP_CFLAGS := $(shell $(PKG_CONFIG) --cflags libxdp 2>/dev/null)
LIBXDP_LIBS   := $(shell $(PKG_CONFIG) --libs libxdp 2>/dev/null)

# ===== BPF 대상 아키텍처 =====
# CO-RE가 사용할 대상 아키텍처를 빌드 머신에 맞춰 지정한다.
ARCH ?= $(shell uname -m)
ifeq ($(ARCH),aarch64)
  BPF_TARGET_ARCH ?= -D__TARGET_ARCH_arm64
else ifeq ($(ARCH),x86_64)
  BPF_TARGET_ARCH ?= -D__TARGET_ARCH_x86
else
endif

all: xdp_loader

check-build-tools:
	@command -v $(CC) >/dev/null 2>&1 || { echo "missing C compiler: $(CC)" >&2; exit 1; }
	@command -v $(PKG_CONFIG) >/dev/null 2>&1 || { echo "missing pkg-config" >&2; exit 1; }

check-bpf-tools:
	@command -v $(CLANG) >/dev/null 2>&1 || { echo "missing BPF compiler: $(CLANG)" >&2; exit 1; }
	@command -v $(BPFTOOL) >/dev/null 2>&1 || { echo "missing bpftool: $(BPFTOOL)" >&2; exit 1; }
	@test -r /sys/kernel/btf/vmlinux || { echo "kernel BTF is unavailable: /sys/kernel/btf/vmlinux" >&2; exit 1; }
	@test -n "$(BPF_TARGET_ARCH)" || { echo "unsupported architecture: $(ARCH) (set BPF_TARGET_ARCH explicitly)" >&2; exit 1; }

check-libbpf: check-build-tools
	@$(PKG_CONFIG) --exists libbpf || { echo "missing libbpf development package" >&2; exit 1; }

check-libxdp: check-build-tools
	@$(PKG_CONFIG) --exists libxdp || { echo "missing libxdp development package" >&2; exit 1; }

vmlinux.h: | check-bpf-tools
	@set -e; $(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@.tmp; mv $@.tmp $@

xdp_kern.o: xdp_kern.c config.h vmlinux.h
	$(CLANG) -target bpf $(BPF_TARGET_ARCH) $(BPF_CFLAGS) $(WARNINGS) -c $< -o $@

xdp_kern.skel.h: xdp_kern.o | check-bpf-tools
	@set -e; $(BPFTOOL) gen skeleton $< > $@.tmp; mv $@.tmp $@

xdp_loader: xdp_loader.c xdp_kern.skel.h config.h | check-libbpf
	$(CC) $(CFLAGS) $(WARNINGS) $(LIBBPF_CFLAGS) $< -o $@ $(LIBBPF_LIBS)

afxdp_recv: afxdp_recv.c config.h | check-libbpf check-libxdp
	$(CC) $(CFLAGS) $(WARNINGS) $(LIBBPF_CFLAGS) $(LIBXDP_CFLAGS) $< -o $@ \
		$(LIBXDP_LIBS) $(LIBBPF_LIBS)

afxdp: xdp_loader afxdp_recv

clean:
	rm -f xdp_kern.o xdp_kern.skel.h xdp_kern.skel.h.tmp \
		xdp_loader afxdp_recv vmlinux.h vmlinux.h.tmp

.PHONY: all clean afxdp check-build-tools check-bpf-tools check-libbpf check-libxdp
