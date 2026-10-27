#!/bin/sh
# Run on Radxa after installing the console. Deliberately closes the AI gate.
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'Radxaでsudo sh management/install-dns.shを実行してください。' >&2; exit 1; }
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -c 'import dns.message, dns.query' || { echo '先に sudo apt install python3-dnspython を実行してください。' >&2; exit 1; }
test -f /opt/taco-console/control.py
test -f /etc/systemd/system/taco-console-control.service
# Close using the installed controller before replacing any code.
python3 /opt/taco-console/control.py off
systemctl stop taco-console-dns.service 2>/dev/null || true
# Fail early if a different DNS service already owns this exact address/wildcard.
python3 - <<'PY'
import errno
import socket
import sys
for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
    try:
        with socket.socket(socket.AF_INET, kind) as sock:
            sock.bind(('10.1.0.1', 53))
    except OSError as error:
        if error.errno == errno.EADDRNOTAVAIL:
            sys.exit('DNS導入中止: このホストにAI側IP 10.1.0.1がありません。'
                     'ip -br -4 addr と nmcli connection show --active で確認してください。'
                     '対外接続は閉じたままです。')
        if error.errno == errno.EADDRINUSE:
            sys.exit('DNS導入中止: 10.1.0.1:53が使用中です。'
                     'sudo ss -luntp で競合サービスを確認してください。'
                     '対外接続は閉じたままです。')
        raise
PY
backup=/var/backups/taco-console-dns/$(date -u +%Y%m%dT%H%M%S)-$$
install -d -m 0700 "$backup"
cp -p /opt/taco-console/control.py "$backup/control.py"
if [ -f /etc/nftables.d/taco-console.nft ]; then
    cp -p /etc/nftables.d/taco-console.nft "$backup/taco-console.nft"
fi
cp -p /etc/nftables.conf "$backup/nftables.conf"
getent group taco-console-dns >/dev/null || groupadd --system taco-console-dns
getent passwd taco-console-dns >/dev/null || useradd --system --gid taco-console-dns --no-create-home --shell /usr/sbin/nologin taco-console-dns
install -m 0644 "$base/taco-console/control.py" /opt/taco-console/control.py
install -m 0644 "$base/taco-console/dns_forwarder.py" /opt/taco-console/dns_forwarder.py
install -m 0644 "$base/taco-console/dns_observations.py" /opt/taco-console/dns_observations.py
# Validate and persist the new gate, preserving existing mTLS firewall includes.
python3 /opt/taco-console/control.py install
install -m 0644 "$base/systemd/taco-console-dns.service" /etc/systemd/system/taco-console-dns.service
install -d -m 0755 /etc/systemd/system/taco-console-collector.service.d
install -m 0644 "$base/systemd/taco-console-collector.service.d/dns.conf" /etc/systemd/system/taco-console-collector.service.d/dns.conf
systemctl daemon-reload
systemctl restart taco-console-control.service
# Runtime rules are installed atomically, initially closed, by the controller.
sh "$base/install-console.sh" --update-console
systemctl enable taco-console-dns.service
systemctl restart taco-console-dns.service
systemctl is-active --quiet taco-console-dns.service
printf '%s\n' "変更前のバックアップ: $backup"
printf '%s\n' 'DNS導入完了。対外接続は「閉」です。SparkのDNSを10.1.0.1に変更し、コンソールから「開」にして確認してください。'
