# SPDX-License-Identifier: GPL-2.0
#
# BPF/Makefile — XDP 패킷 분류기 빌드 시스템
#
# 빌드 흐름 (make 실행 시 자동으로 수행됨):
#
#   1) vmlinux.h 생성
#      /sys/kernel/btf/vmlinux (커널 BTF)
#        → bpftool btf dump
#        → vmlinux.h (커널 전체 타입 헤더)
#
#   2) BPF object 컴파일
#      xdp_kern.c + vmlinux.h
#        → clang -target bpf
#        → xdp_kern.o (BPF ELF 오브젝트)
#
#   3) skeleton 헤더 생성
#      xdp_kern.o
#        → bpftool gen skeleton
#        → xdp_kern.skel.h (C 구조체 형태의 BPF 로딩 코드)
#
#   4) userspace 로더 컴파일
#      xdp_loader.c + xdp_kern.skel.h
#        → gcc + libbpf
#        → xdp_loader (실행 파일)

# --------------------------------------------------------------------------
# 도구 경로 (환경에 맞게 재정의 가능, 예: make CLANG=/usr/bin/clang-19)
# --------------------------------------------------------------------------
CLANG   ?= clang          # BPF 프로그램 컴파일러 (LLVM 기반, BPF 백엔드 지원 필요)
CC      ?= cc             # userspace 로더 컴파일러 (gcc 또는 cc)
BPFTOOL ?= bpftool        # BTF 덤프 및 skeleton 생성 도구
CFLAGS  ?= -O2 -g -Wall -Wextra   # userspace 컴파일 플래그

# --------------------------------------------------------------------------
# 타깃 아키텍처 자동 감지
#
# BPF 프로그램은 아키텍처 독립적인 바이트코드로 컴파일되지만,
# vmlinux.h의 구조체 레이아웃이 아키텍처마다 다르므로
# __TARGET_ARCH_xxx 매크로를 지정해야 올바른 CO-RE relocation이 된다.
# --------------------------------------------------------------------------
ARCH := $(shell uname -m)
ifeq ($(ARCH),aarch64)
  BPF_TARGET_ARCH := -D__TARGET_ARCH_arm64    # Raspberry Pi 5 등 ARM64 보드
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

xdp_loader: xdp_loader.c xdp_kern.skel.h
	$(CC) $(CFLAGS) $< -o $@ $$(pkg-config --cflags --libs libbpf)

clean:
	rm -f xdp_kern.o xdp_kern.skel.h xdp_loader vmlinux.h

.PHONY: all clean
