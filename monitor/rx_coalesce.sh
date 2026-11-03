#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# == 4 && $2 =~ ^[a-zA-Z0-9_.:-]+$ && -d $3 && $4 =~ ^[0-9]+$ ]] || exit 2
action=$1 iface=$2 directory=$3 requested=$4
read_usecs() { ethtool -c "$iface" | awk '$1=="rx-usecs:" {print $2}'; }
if [[ $action == save ]]; then
    old=$(read_usecs)
    [[ $old =~ ^[0-9]+$ ]] || exit 1
    (set -o noclobber; printf '%s\n' "$old" > "$directory/rx-coalesce-before.txt")
elif [[ $action == apply ]]; then
    [[ -f $directory/rx-coalesce-before.txt ]] || exit 1
    ethtool -C "$iface" rx-usecs "$requested"
    actual=$(read_usecs)
    [[ $actual == "$requested" ]] || { echo "RX coalescing readback: requested=$requested actual=$actual" >&2; exit 1; }
    ethtool -c "$iface" > "$directory/coalesce-active.txt"
    printf '{"rx_usecs":%s,"verified":true,"applied_after":"xdp_and_receiver_ready"}\n' "$actual" > "$directory/coalesce-active.json"
elif [[ $action == restore ]]; then
    [[ -f $directory/rx-coalesce-before.txt ]] || exit 0
    old=$(cat "$directory/rx-coalesce-before.txt")
    [[ $old =~ ^[0-9]+$ ]] || exit 1
    if [[ $(read_usecs) != "$old" ]]; then
        ethtool -C "$iface" rx-usecs "$old"
        # macb's sub-microsecond timer quantizes an integer readback downwards.
        if [[ $(read_usecs) != "$old" ]]; then ethtool -C "$iface" rx-usecs "$((old+1))"; fi
    fi
    [[ $(read_usecs) == "$old" ]] || exit 1
    printf '{"rx_usecs":%s,"restored":true}\n' "$old" > "$directory/coalesce-restored.json"
else
    exit 2
fi
