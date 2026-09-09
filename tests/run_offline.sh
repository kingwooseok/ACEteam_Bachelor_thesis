#!/usr/bin/env bash
# Build and exercise everything that does not require a physical Ethernet peer.
set -Eeuo pipefail

project_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
udp_dir="$project_dir/udp_socket"
test_dir="$project_dir/tests"
sender="$udp_dir/bin/sender"
receiver="$udp_dir/bin/receiver"
calibrator="$udp_dir/bin/phc_calibrate"
loader="$project_dir/BPF/xdp_loader"
afxdp_receiver="$project_dir/BPF/afxdp_recv"

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/ace-offline.XXXXXX")
staging_dir=$(mktemp -d /dev/shm/ace-offline.XXXXXX)
staging_normal="$staging_dir/normal.bin"
staging_flow="$staging_dir/flow.bin"
staging_trunc="$staging_dir/trunc.bin"
staging_hwts="$staging_dir/hwts.bin"
receiver_pid=""

cleanup()
{
	if [[ -n "$receiver_pid" ]] && kill -0 "$receiver_pid" 2>/dev/null; then
		kill "$receiver_pid" 2>/dev/null || true
		wait "$receiver_pid" 2>/dev/null || true
	fi
	rm -f -- "$staging_normal" "$staging_flow" "$staging_trunc" "$staging_hwts"
	rmdir -- "$staging_dir" 2>/dev/null || true
	rm -f -- "$work_dir/abi_wire_test" \
		"$work_dir/runtime_helpers_test" \
		"$work_dir/normal.bin" "$work_dir/normal.receiver.log" "$work_dir/normal.sender.log" \
		"$work_dir/flow.bin" "$work_dir/flow.receiver.log" \
		"$work_dir/trunc.bin" "$work_dir/trunc.receiver.log" \
		"$work_dir/hwts.bin" "$work_dir/hwts.receiver.log" "$work_dir/hwts.sender.log" \
		"$work_dir/cli.log"
	rmdir -- "$work_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

step()
{
	printf '\n[%s] %s\n' "$1" "$2"
}

fail()
{
	printf 'FAIL: %s\n' "$*" >&2
	exit 1
}

expect_status()
{
	local expected=$1
	local label=$2
	shift 2
	local actual

	set +e
	"$@" >"$work_dir/cli.log" 2>&1
	actual=$?
	set -e
	if [[ $actual -ne $expected ]]; then
		printf '%s\n' "--- command output ---" >&2
		sed -n '1,120p' "$work_dir/cli.log" >&2
		fail "$label returned $actual; expected $expected"
	fi
	printf 'ok: %s (exit %d)\n' "$label" "$actual"
}

free_udp_port()
{
	python3 - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

start_receiver()
{
	local log=$1
	shift
	timeout --signal=TERM 8s "$receiver" "$@" >"$log" 2>&1 &
	receiver_pid=$!
	# The setup is small (one to three records). Verify it did not fail before send.
	sleep 0.15
	if ! kill -0 "$receiver_pid" 2>/dev/null; then
		set +e
		wait "$receiver_pid"
		local status=$?
		set -e
		receiver_pid=""
		sed -n '1,160p' "$log" >&2
		fail "receiver exited during setup (exit $status)"
	fi
}

wait_receiver()
{
	local expected=$1
	local log=$2
	local actual

	set +e
	wait "$receiver_pid"
	actual=$?
	set -e
	receiver_pid=""
	if [[ $actual -ne $expected ]]; then
		sed -n '1,200p' "$log" >&2
		fail "receiver returned $actual; expected $expected"
	fi
}

for tool in cc make git python3 timeout mktemp; do
	command -v "$tool" >/dev/null 2>&1 || fail "required tool is missing: $tool"
done
[[ -d /dev/shm && -w /dev/shm ]] || fail "/dev/shm must be writable tmpfs"

step 1 "build UDP and BPF userspace/kernel objects"
make --no-print-directory -C "$udp_dir"
make --no-print-directory -C "$project_dir/BPF"

step 2 "reject whitespace/errors in the working diff"
git -C "$project_dir" diff --check

step 3 "compile and verify shared ABI/runtime helpers"
cc -std=c11 -Wall -Wextra -Wpedantic -Werror \
	-I"$project_dir/include" -I"$udp_dir/include" \
	"$test_dir/abi_wire_test.c" -o "$work_dir/abi_wire_test"
"$work_dir/abi_wire_test"
cc -std=c11 -Wall -Wextra -Wpedantic -Werror \
	-I"$project_dir/include" \
	"$test_dir/runtime_helpers_test.c" -o "$work_dir/runtime_helpers_test"
"$work_dir/runtime_helpers_test"

step 4 "exercise CLI help and pre-setup rejection paths"
expect_status 0 "sender --help" "$sender" --help
expect_status 0 "receiver --help" "$receiver" --help
expect_status 0 "phc_calibrate --help" "$calibrator" --help
expect_status 0 "xdp_loader --help" "$loader" --help
expect_status 0 "afxdp_recv --help" "$afxdp_receiver" --help
expect_status 2 "sender rejects port zero" "$sender" --port 0 127.0.0.1
expect_status 2 "sender requires a flow ID" "$sender" 127.0.0.1
expect_status 2 "sender rejects signed count" \
	"$sender" --flow-id 1 --count -1 127.0.0.1
expect_status 2 "sender rejects fragmented/oversize payload" \
	"$sender" --payload-size 1441 127.0.0.1
expect_status 2 "sender rejects invalid IPv4" "$sender" not-an-ip
expect_status 2 "receiver rejects count zero" "$receiver" --count 0
expect_status 2 "receiver requires a flow ID" "$receiver" --count 1
expect_status 2 "receiver rejects identical staging/output paths" \
	"$receiver" --tmpfs "$staging_normal" --output "$staging_normal" --count 1
expect_status 2 "phc_calibrate rejects zero samples" "$calibrator" --samples 0
expect_status 2 "phc_calibrate rejects signed samples" "$calibrator" --samples +1
expect_status 2 "phc_calibrate rejects device/interface ambiguity" \
	"$calibrator" --device /dev/ptp0 --interface eth0
expect_status 1 "xdp_loader requires an output directory" "$loader"
expect_status 1 "xdp_loader rejects unsafe prefix" \
	"$loader" --output-dir "$work_dir" --prefix ../escape
expect_status 1 "xdp_loader rejects signed record count" \
	"$loader" --output-dir "$work_dir" --records +1
expect_status 1 "xdp_loader rejects cleanup outside recovery" \
	"$loader" --output-dir "$work_dir" --cleanup-pins
expect_status 1 "afxdp_recv requires an output file" "$afxdp_receiver"
expect_status 1 "afxdp_recv rejects signed record count" \
	"$afxdp_receiver" --output "$work_dir/unused.csv" --records +1
expect_status 1 "afxdp_recv rejects nonnumeric RX queue" \
	"$afxdp_receiver" --output "$work_dir/unused.csv" lo abc

step 5 "normal UDP sender/receiver loopback"
normal_port=$(free_udp_port)
start_receiver "$work_dir/normal.receiver.log" \
	--bind 127.0.0.1 --port "$normal_port" --flow-id 42 \
	--payload-size 64 --count 3 --tmpfs "$staging_normal" \
	--output "$work_dir/normal.bin"
if ! "$sender" --port "$normal_port" --flow-id 42 --payload-size 64 \
	--count 3 --period-ns 100000 127.0.0.1 >"$work_dir/normal.sender.log" 2>&1; then
	sed -n '1,160p' "$work_dir/normal.sender.log" >&2
	fail "normal loopback sender failed"
fi
wait_receiver 0 "$work_dir/normal.receiver.log"
grep -q 'completed: samples=3 invalid=0' "$work_dir/normal.receiver.log" || {
	sed -n '1,200p' "$work_dir/normal.receiver.log" >&2
	fail "normal loopback counters differ"
}
python3 "$test_dir/verify_udp_records.py" "$work_dir/normal.bin" \
	--flow-id 42 --sequences 0,1,2 --packet-length 96

step 6 "wrong-flow packet is rejected before the valid packet"
flow_port=$(free_udp_port)
start_receiver "$work_dir/flow.receiver.log" \
	--bind 127.0.0.1 --port "$flow_port" --flow-id 42 \
	--payload-size 64 --count 1 --tmpfs "$staging_flow" \
	--output "$work_dir/flow.bin"
python3 "$test_dir/udp_fixture.py" --port "$flow_port" --flow-id 99 \
	--sequence 111 --payload-size 64
sleep 0.05
python3 "$test_dir/udp_fixture.py" --port "$flow_port" --flow-id 42 \
	--sequence 222 --payload-size 64
wait_receiver 0 "$work_dir/flow.receiver.log"
grep -q 'completed: samples=1 invalid=1' "$work_dir/flow.receiver.log" || {
	sed -n '1,200p' "$work_dir/flow.receiver.log" >&2
	fail "wrong-flow packet was not counted as invalid"
}
python3 "$test_dir/verify_udp_records.py" "$work_dir/flow.bin" \
	--flow-id 42 --sequences 222 --packet-length 96

step 7 "userspace-truncated oversized datagram is rejected"
trunc_port=$(free_udp_port)
start_receiver "$work_dir/trunc.receiver.log" \
	--bind 127.0.0.1 --port "$trunc_port" --flow-id 42 \
	--payload-size 1440 --count 1 --tmpfs "$staging_trunc" \
	--output "$work_dir/trunc.bin"
# This 2000-byte datagram is larger than receiver's 1472-byte buffer. A valid
# prefix must not make MSG_TRUNC input look like a valid single-frame sample.
python3 "$test_dir/udp_fixture.py" --port "$trunc_port" --flow-id 42 \
	--sequence 666 --payload-size 1968
sleep 0.05
python3 "$test_dir/udp_fixture.py" --port "$trunc_port" --flow-id 42 \
	--sequence 777 --payload-size 1440
wait_receiver 0 "$work_dir/trunc.receiver.log"
grep -q 'completed: samples=1 invalid=1' "$work_dir/trunc.receiver.log" || {
	sed -n '1,200p' "$work_dir/trunc.receiver.log" >&2
	fail "truncated datagram was not counted as invalid"
}
python3 "$test_dir/verify_udp_records.py" "$work_dir/trunc.bin" \
	--flow-id 42 --sequences 777 --packet-length 1472

step 8 "strict HW timestamp mode fails cleanly on loopback and preserves data"
hwts_port=$(free_udp_port)
start_receiver "$work_dir/hwts.receiver.log" \
	--bind 127.0.0.1 --port "$hwts_port" --flow-id 7 \
	--payload-size 0 --count 1 --tmpfs "$staging_hwts" \
	--output "$work_dir/hwts.bin" --require-hwts
if ! "$sender" --port "$hwts_port" --flow-id 7 --payload-size 0 \
	--count 1 --period-ns 100000 127.0.0.1 >"$work_dir/hwts.sender.log" 2>&1; then
	sed -n '1,160p' "$work_dir/hwts.sender.log" >&2
	fail "strict-HWTS loopback sender failed"
fi
wait_receiver 1 "$work_dir/hwts.receiver.log"
grep -Eq 'completed: samples=1 invalid=0 .*missing_hwts=1' "$work_dir/hwts.receiver.log" || {
	sed -n '1,200p' "$work_dir/hwts.receiver.log" >&2
	fail "strict HW timestamp loopback behavior differs"
}
python3 "$test_dir/verify_udp_records.py" "$work_dir/hwts.bin" \
	--flow-id 7 --sequences 0 --packet-length 32

step 9 "analysis and provenance unit tests"
python3 -m unittest -v \
	analysis.tests.test_join_results \
	analysis.tests.test_aggregate_runs \
	tests.test_capture_manifest

printf '\nPASS: all offline checks completed without NIC configuration or root privileges\n'
