#!/bin/sh
# Full install adds the gate; --update-console updates UI/collection only.
set -eu
mode=${1:-install}
case "$mode" in
    install|--update-console) ;;
    *) echo 'usage: install-console.sh [--update-console]' >&2; exit 1 ;;
esac
if [ "$(id -u)" -ne 0 ]; then
    echo 'TACO上で sudo sh management/install-console.sh を実行してください。' >&2
    exit 1
fi
command -v python3 >/dev/null
command -v conntrack >/dev/null
command -v journalctl >/dev/null
if [ "$mode" = '--update-console' ]; then
    test -f /opt/taco-console/control.py
    test -f /etc/systemd/system/taco-console-web.service
    getent group taco-console-control >/dev/null
else
    command -v nft >/dev/null
    command -v ping >/dev/null
    getent group taco-console-control >/dev/null || groupadd --system taco-console-control
fi
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d -m 0755 /opt/taco-console/static /var/lib/taco-console
install -m 0644 "$base/taco-console/console.py" /opt/taco-console/console.py
if [ "$mode" != '--update-console' ]; then
    install -m 0644 "$base/taco-console/control.py" /opt/taco-console/control.py
fi
install -m 0644 "$base/taco-console/geoip.py" /opt/taco-console/geoip.py
install -m 0644 "$base/update_geoip.py" /opt/taco-console/update_geoip.py
for asset in index.html app.js style.css; do
    install -m 0644 "$base/taco-console/static/$asset" "/opt/taco-console/static/$asset"
done
if [ "$mode" = '--update-console' ]; then
    systemctl restart taco-console-collector.service taco-console-web.service
    echo '監視コンソールを更新しました。ブラウザーを再読み込みしてください。'
    exit 0
fi
install -m 0644 "$base/systemd/taco-console-collector.service" /etc/systemd/system/taco-console-collector.service
install -m 0644 "$base/systemd/taco-console-web.service" /etc/systemd/system/taco-console-web.service
install -m 0644 "$base/systemd/taco-console-control.service" /etc/systemd/system/taco-console-control.service
python3 /opt/taco-console/control.py install
systemctl daemon-reload
systemctl enable taco-console-collector.service taco-console-control.service taco-console-web.service
systemctl restart taco-console-control.service
systemctl restart taco-console-collector.service taco-console-web.service
echo '導入完了。AI側の外向き通信はOFFです。SSHトンネルで http://127.0.0.1:8765 を開いてください。'
echo '操作キーは sudo cat /etc/taco-console/control.token で確認できます。'
