#!/bin/bash

# ============================================================
# install_irq_service.sh — 수신 Pi에서만 실행하는 명시적 설치 단계
#
#   sudo bash monitor/install_irq_service.sh install eth0 --receiver
#   sudo bash monitor/install_irq_service.sh disable eth0 --receiver
#
# Git pull/build만으로 서비스를 등록하지 않는다. install은 지금 적용하고
# 다음 부팅에도 활성화한다. 기존 /etc/default 설정은 보존한다.
# disable은 해당 instance의 자동 실행만 해제하며 현재 IRQ 값을 임의 복원하지 않는다.
# ============================================================

set -Eeuo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ${1:-} == --help ]]; then
    echo 'Usage: sudo install_irq_service.sh install|disable INTERFACE --receiver'
    exit 0
fi
[[ $# == 3 && ($1 == install || $1 == disable) && $2 =~ ^[A-Za-z0-9_.:-]{1,15}$ && $3 == --receiver ]] || exit 2
((EUID == 0)) || { echo 'Error: root is required for explicit service installation' >&2; exit 1; }
[[ -d /sys/class/net/$2 ]] || { echo "Error: interface not found: $2" >&2; exit 1; }
for tool in install systemctl systemd-analyze systemd-escape; do command -v "$tool" >/dev/null || exit 1; done
unit=$(systemd-escape --template=ace-rx-irq-affinity@.service "$2")
if [[ $1 == disable ]]; then
    systemctl disable --now "$unit"
    exit 0
fi
install -d -m 0755 /usr/local/libexec/ace
install -m 0755 "$SCRIPT_DIR/set_irq_affinity.sh" /usr/local/libexec/ace/set_irq_affinity.sh
install -m 0644 "$SCRIPT_DIR/systemd/ace-rx-irq-affinity@.service" /etc/systemd/system/ace-rx-irq-affinity@.service
if [[ ! -e /etc/default/ace-rx-irq-affinity ]]; then
    install -m 0644 "$SCRIPT_DIR/systemd/ace-rx-irq-affinity.conf.example" /etc/default/ace-rx-irq-affinity
fi
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/ace-rx-irq-affinity@.service
systemctl enable "$unit"
systemctl restart "$unit"
systemctl --no-pager status "$unit"
