# RadxaのDNSフォワーダーと推定ドメイン表示

Spark → **Radxa 10.1.0.1:53（UDP/TCP）** → **NetScreen 10.0.0.254:53** の順で名前解決する。RadxaのDNS転送専用サービスは `taco-console-dns.service`。DHCPやHTTPS復号は行わない。DNSプロトコルの解析・転送にはDebianパッケージ `python3-dnspython` を使用する。

| 対外接続 | AIからRadxaへのDNS | DNS転送・AIへの応答 | SparkからNetScreen／外部への直接DNS |
|---|---|---|---|
| 開 | 受付 | 許可 | 拒否 |
| 閉 | 遮断 | 処理途中・既存TCPも遮断 | 拒否 |
| 再起動／制御サービス再起動／許可期限切れ | 遮断 | 遮断 | 拒否 |

Radxa自身のOSのDNSは引き続き `10.0.0.254` を使い、閉じても更新・管理用の名前解決は維持する。転送サービスだけは送信元を `10.1.0.1` に固定し、この通信とAI向けDNS応答にも既存の20秒許可期限を適用する。別の開閉状態は持たない。

## 1. 既存Radxaへの導入

管理PCから更新した資材を転送する（既存のSSH接続名が `taco` の場合）。

```sh
rsync -av --exclude '__pycache__' management/ taco:~/network-management/
```

Radxaで以下を実行する。コンソール・mTLS導入済み環境を対象とする。管理SSHは維持されるが、**導入・再実行の際に対外接続を「閉」にする**。

```sh
sudo apt update
sudo apt install python3-dnspython
sudo sh ~/network-management/install-dns.sh
sudo systemctl status taco-console-dns.service --no-pager
sudo journalctl -u taco-console-dns.service -n 30 --no-pager
```

導入スクリプトは、現在のゲートを閉じ、53番ポートの競合を検査してから、専用ユーザー・DNSサービス・新しいゲート・コンソールを更新する。変更前の制御コード・ゲート断片・nftables.confを `/var/backups/taco-console-dns/日時-PID/` に保存する。既存の `/etc/nftables.conf` とCaddy・mTLS設定を維持し、nftables全体の再読み込みは行わない。DNS許可は `/etc/nftables.d/taco-console.nft` に保存する。TCP/UDP 53が他のサービスで使用中なら、上記ポート競合検査で止まる。競合元を確認してから再実行する。導入途中で失敗しても「閉」のままとする。

全体を新規構築する場合は、従来のネットワーク・コンソール・mTLS導入後に本手順を実行する。基本ネットワーク生成物だけではDNS転送サービスは稼働しない。

NetScreen側は従来の `10.1.0.0/24` への戻り経路と `ai_dns`（TCP/UDP53、送信元AI網・Radxa）の許可を使用する。追加の開放は不要。受信サービス独自の制限がある環境では、転送元 `10.1.0.1` のDNS問い合わせも受け付ける必要がある。NetScreenのdnsmasqが `localservice=1` の場合は、FW許可とは別に非直結網の問い合わせを拒否する場合がある。既に行ったDNS設定調整を維持する。

## 2. SparkのDNS設定だけ変更する

Spark上で、有線の接続名・デバイス名と現在のDNSを確認して控える。

```sh
nmcli -f NAME,DEVICE connection show --active
nmcli -f ipv4.dns,ipv4.ignore-auto-dns connection show '実際の接続名'
resolvectl status
```

次の接続名とデバイス名は実機の値へ置き換える（これまでの例は `enP7s7`）。IPアドレスとゲートウェイは変更しない。

```sh
sudo nmcli connection modify '実際の接続名' ipv4.dns '10.1.0.1' ipv4.ignore-auto-dns yes
sudo nmcli device reapply enP7s7
sudo resolvectl flush-caches
resolvectl status enP7s7
```

`/etc/resolv.conf` が `127.0.0.53` を示すのはsystemd-resolved利用時の正常な状態。`resolvectl status` のリンク側DNSが `10.1.0.1` になっていることを確認する。Wi-Fiや別の接続にDNS・デフォルト経路が残っていないことも確認する。

## 3. 開閉と表示の確認

コンソールを再読み込みし、対外接続を「開」にしてからSparkで実行する。

```sh
dig +time=2 +tries=1 @10.1.0.1 example.com
dig +tcp +time=2 +tries=1 @10.1.0.1 example.com
resolvectl query example.com
curl -I --connect-timeout 5 https://example.com
```

DNSの応答後、約2秒で候補データを公開し、通常の約5秒の収集・画面更新で反映する。「観」の経路一覧・選択詳細、「覧」の宛先IPの下に **推定ドメイン** を表示する。検索欄でもドメインを検索できる。短時間で終了する通信は既存の収集間隔のため見逃すことがある。

対外接続を「閉」にし、次のUDP/TCP問い合わせがタイムアウトすることを確認する。閉じた時点で処理中だった転送・応答、既存のDNS TCP接続にも遮断を適用する。

```sh
dig +time=2 +tries=1 @10.1.0.1 example.com
dig +tcp +time=2 +tries=1 @10.1.0.1 example.com
```

「開」でも、Sparkから `@10.0.0.254` や `@1.1.1.1` への直接DNSは通らない。AI側DNSをRadxaへ統一するための制限である。

Radxaでの切り分け：

```sh
sudo systemctl status taco-console-dns.service taco-console-control.service --no-pager
sudo ss -luntp 'sport = :53'
sudo nft list table inet taco_console
sudo journalctl -u taco-console-dns.service -n 50 --no-pager
sudo tcpdump -ni eth0 'host 10.0.0.254 and port 53'
```

開いているのにSERVFAILになる場合は、NetScreenへの経路・DNS受信制限を確認する。タイムアウトなら、コンソールの開閉・ゲート・DNSサービス・SparkのDNS設定を確認する。

## 推定の範囲と保存

- 同じ問い合わせ元IPが受け取ったA/AAAA応答と宛先IPを照合する。CNAMEは問い合わせた元の名前を表示し、途中のCNAMEとアドレス応答の短い方のTTLを使う。TTLの上限は1時間。無関係な追加レコードは照合に使わない。
- 共有IPに複数の名前が対応する場合は複数候補を表示する。実際のHTTPSホスト名・URLの確定結果ではない。DNSが発生しないキャッシュ利用、直接IP指定、DoH、観測開始前のDNSでは表示できない。
- DNS観測情報は最大1万件、`/run/taco-console-dns/observations.json`（tmpfs）に置く。問い合わせごとのディスクログは作らない。再起動で候補は消える。フォワーダー自身は応答をキャッシュせず、上流とクライアント側のキャッシュを利用する。
- セッションに付いた候補名は既存のSQLite履歴へ一緒に保存し、そのセッションの観測が続いている間はTTL後も維持する。観測が終了し新しい通信として観測された場合は改めて照合する。既存の履歴保持期間（終了済み24時間・全体目安2万件）に従う。
- 公開IPv4へのTCP443は従来どおり許可されるため、DoHなど暗号化DNSを網羅して遮断・推定するものではない。

## 一時的に旧DNS構成へ戻す場合

新しいゲートは直接DNSを遮断するため、SparkのDNSだけを `10.0.0.254` に戻しても名前解決は復旧しない。転送サービスの不調はまず「閉」のまま修復する。旧構成へ戻す場合は、最初の導入時のバックアップを選び、Radxa上で以下を実行する。バックアップが今回の機能追加前であることを確認する。

```sh
sudo python3 /opt/taco-console/control.py off
sudo systemctl disable --now taco-console-dns.service
sudo install -m 0644 /var/backups/taco-console-dns/実際の日時-PID/control.py /opt/taco-console/control.py
sudo install -m 0644 /var/backups/taco-console-dns/実際の日時-PID/taco-console.nft /etc/nftables.d/taco-console.nft
sudo nft -c -f /etc/nftables.conf
sudo systemctl restart taco-console-control.service
```

その後SparkのDNSを控えた値へ戻し、コンソールで「開」にして確認する。FW全体のバックアップを単純に読み戻すとmTLSルールも変わるため、今回追加したDNS・ゲート部分を対象にする。

## 開発時の試験

```sh
python3 -m unittest discover -s management/tests -p 'test_*.py'
node management/tests/test_atlas.cjs
docker build -t taco-gate-test -f management/tests/Dockerfile .
docker run --rm --privileged --network none -e TACO_GATE_DISPOSABLE_TEST=1 -v "$PWD:/workspace:ro" -w /workspace taco-gate-test /usr/bin/python3 -B management/tests/linux_gate_smoke.py
```

Python試験にはdnspythonと、ローカル試験用UDP/TCPポートを開ける環境が必要。カーネル試験は使い捨てコンテナ専用で、実機のFirewallを変更して実行しない。
