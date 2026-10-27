# DGX Spark 用ネットワーク設定

通信状況のWeb表示と、OpenWrt／TACOのバージョン収集は [開発NW管理ツール](management/README.md) を参照。管理PC限定のHTTPS・mTLS接続と証明書登録は [mTLS導入手順](management/MTLS.md) を参照。

デプロイは行わない。`example-output/` は **管理PC=10.0.0.10、OpenWrtポート=lan4、RPi上流=eth0、AI側=eth1を仮定したサンプル**。
実機の値に合わせて生成し直すこと。Raspberry Pi OS Lite 64bit + NetworkManager、OpenWrt fw4を対象とする。
OpenWrtは既存設定への追加断片であり、設定ファイル全体の置換用ではない。

DNS転送・推定ドメイン表示を追加する場合は [Radxa DNS導入・Spark設定手順](management/DNS.md) を参照。導入後はSparkのDNSを `10.1.0.1` に変更する。以下はDNS転送追加前の基本ネットワーク構成。

## 構成

```text
開発LAN 10.0.0.0/24（既存Wi-Fi・残りのLANポート）
  管理PC: config.jsonで指定、DHCP予約または固定IP
  OpenWrt LAN/DNS/DGW: 10.0.0.254
       |
  専用ポート: 10.2.0.1/30、aiゾーン
       |
  RPi上流: 10.2.0.2/30
  RPi下流: 10.1.0.1/24
       |
  Spark: 10.1.0.10/24、DGW 10.1.0.1、DNS 10.0.0.254
```

10.2.0.0/30は今回追加した中継区間。既存ネットワーク・VPNと重複しないことを確認。
RPiはNATしない。OpenWrtにAIセグメントへの静的ルートを追加し、既存WANのmasqueradeでインターネットへ出す。
OpenWrtの既存ゾーン名は`lan`と`wan`を仮定。WANのmasqueradeが有効で、送信元制限`masq_src`がある場合は
`10.1.0.0/24`と`10.2.0.2/32`も対象に含める。ポリシールーティング/VPNは別途監査する。

## 許可ポリシー

| 送信元 | 宛先 | 許可 |
|---|---|---|
| 管理PC 1台 | RPi 10.2.0.2 / Spark 10.1.0.10 | TCP22、ping |
| AI側 / RPi | OpenWrt 10.0.0.254 | TCP/UDP53のみ |
| AI側 / RPi | 公開IPv4 | TCP80/443、UDP123 |
| Spark | RPi 10.1.0.1 | ping。SMB TCP445はコメント解除時のみ |
| RPi | OpenWrt 10.2.0.1 | ping |
| 全区間 | 許可された接続への応答、関連ICMPエラー | 状態追跡で許可 |

それ以外の新規接続は拒否。IPv6の入力・転送を許可しない。下流DHCPは用意せずSparkを固定IPにする。
AI側の端末同士は同一L2上にあるとRPiを通らないため、このFWでは相互分離できない。今回はSpark単体を想定。
NASストレージ構成・ユーザー作成・Samba設定は含まない。NAS許可はRPiのINPUTでありFORWARDではない。

**これは「インターネット接続あり」のプロファイル**。80/443は全公開宛先に許可されるので、HTTPS経由の外部送信やDoHも可能。
DNSはLANへの唯一の例外であり、DNS経由の送信まで防ぐ構成ではない。
完全ローカル運用に切り替える場合はOpenWrtの`ai_web`/`ai_ntp`を無効化し、RPiのforwardの80/443・123許可を除く。
RPi自身の更新通信はoutputに分けてある。外部LLM APIの宛先制限には別途プロキシが必要。

## 1. オフライン生成

このフォルダで実行する。

```sh
cp config.example.json config.json
# config.jsonの4項目を実機に合わせて編集
python3 render.py config.json output
```

- `admin_ipv4`: 管理PCの固定IPv4。10.0.0.254はルーターなので指定しない。
- `openwrt_ai_device`: 開発LANから分離したデバイス。DSAなら例`lan4`、VLAN方式なら例`br-lan.20`。
- `rpi_uplink_if` / `rpi_ai_if`: `ip -br link`と抜き差しで確認。eth番号と物理速度の対応は決め打ちしない。

## 2. OpenWrt

### バックアップと専用ポートの準備

管理PCは分離対象以外のポートまたは既存Wi-Fiから接続。LuCIでバックアップをダウンロードしておく。
`uci export network`、`uci export firewall`、`uci export dhcp`も保存する。

**DSAでポートが個別表示される場合**: LuCIのデバイス設定で、例`lan4`を`br-lan`のポート一覧から外す。
残りのLAN1–3と無線LANは既存LANに残す。既にbridge VLAN filteringを使っている場合は、その構成に合わせる。
**swconfig方式**: 対象ポートを既存LAN VLANから外し、CPU tagged + 対象ポートuntaggedの専用VLANを作る。
CPUポート番号は機種依存。DSA用の手順をそのまま使わない。生成時のdeviceにはそのVLANデバイスを指定。

既存設定がないため、この物理スイッチ操作は自動化していない。同じL2に残したままゾーンだけ追加しない。

### 断片を追加

`output/openwrt/network.fragment` → `/etc/config/network`、`firewall.fragment` → `/etc/config/firewall`、
`dhcp.fragment` → `/etc/config/dhcp`に、それぞれのセクションを**一度だけ**追加する。
同名セクションがある場合は追記せず内容を統合する。再適用時は既存の該当セクションを更新する。

既存の`lan`/`wan`や無線設定を置換しない。`lan→ai`や`ai→wan`の全面許可forwardingは追加しない。
生成したfirewall.fragment内のルール順は維持する。既存の全ゾーン対象ACCEPT、転送・DNATルールがaiを許可しないか確認する。

```sh
fw4 check
```

構文チェック後、LuCIの確認付き「保存して適用」で反映する。CLIの一括再起動は接続断に備えられる場合のみ行う。
OpenWrtのdnsmasqで`localservice`が有効な場合、AIセグメントは直接接続ではないためDNSが拒否されることがある。
その場合は`/etc/config/dhcp`の既存`config dnsmasq`に`option localservice '0'`を設定し、dnsmasqを再起動する。
その前に、**WANや他の未信頼ゾーンからルーターへのTCP/UDP53が拒否されていることを確認**する。
dnsmasqのlisten/interface制限を独自設定している場合も、ai_transitから10.0.0.254への問い合わせを受けられるよう確認する。

## 3. RPi

ローカルコンソールで作業するのが確実。NetworkManagerを使っていることを`nmcli general status`で確認。
固定IPの競合がなく、Wi-Fi・USB NIC・VPNなどの迂回経路がないことを確認する。
必要パッケージ`nftables`を事前に導入。NetworkManager以外のネットワーク管理との併用は避ける。
Docker/UFW/firewalldはこのRPiでは使わない。本設定は`flush ruleset`により既存ルール全体を置換する。

### バックアップ

`/etc/nftables.conf`、`/etc/NetworkManager/system-connections/`、既存sysctl設定を退避。
現在のルールは`sudo nft list ruleset`で別ファイルに保存する。
既存接続の名前・UUID・自動接続設定は`nmcli connection show`で記録する。

### ファイアーウォールを先に適用

以下は、生成した`output/rpi/`のファイルをRPiのカレントディレクトリに転送した後のコマンド。
実行するのはユーザー自身。構文検証成功後に実際のルールを入れる。

```sh
sudo nft -c -f nftables.conf
sudo install -m 0644 nftables.conf /etc/nftables.conf
sudo systemctl enable --now nftables
sudo nft -f /etc/nftables.conf
```

サービスが既に起動済みでも最後のコマンドで新ルールを反映する。
この段階で現在の管理接続が許可条件に合わなければ切断されるため、コンソールまたは復旧手段を確保する。

### NetworkManagerの接続プロファイル

```sh
sudo install -m 0600 taco-uplink.nmconnection /etc/NetworkManager/system-connections/taco-uplink.nmconnection
sudo install -m 0600 taco-ai.nmconnection /etc/NetworkManager/system-connections/taco-ai.nmconnection
sudo nmcli connection reload
```

`nmcli connection show`を確認し、**対象の2枚のNICに適用される古い接続プロファイルだけ**自動接続を無効化する。
例: `sudo nmcli connection modify uuid <旧UUID> connection.autoconnect no`。
Wi-Fiも無効化する。名前が違う既存プロファイルを一律に削除しない。

```sh
sudo nmcli radio wifi off
sudo nmcli connection up taco-uplink
sudo nmcli connection up taco-ai
sudo install -m 0644 90-taco-router.conf /etc/sysctl.d/90-taco-router.conf
sudo sysctl --system
```

この時点でIPv4転送が有効になる。RPiの`ip route`はデフォルトが10.2.0.1、10.0.0.0/24への経路もこのデフォルト経由。
DHCPサーバー・DNSフォワーダーは不要。SparkのDNSは10.0.0.254を直接指定する。
`systemctl is-enabled nftables`と再起動後のルール・経路を確認する。nftablesを停止すると転送制限が失われるので、
保守で停止する前にはIPv4転送を無効化するか、Spark側ケーブルを外す。

### Spark

既存の有線接続プロファイルを、IPv4 `10.1.0.10/24`、DGW `10.1.0.1`、DNS `10.0.0.254`に変更する。
IPv6とWi-Fi・追加の上流接続を無効化。SSHを有効化し、ホスト側FWでも管理PCからのTCP22を許可する。
モデルAPIやJupyterのポートはLANへ公開せず、SSHトンネルで利用する。

## 4. 確認項目

| 実行元 | 確認 | 期待 |
|---|---|---|
| OpenWrt | `ip route get 10.1.0.10` | 10.2.0.2経由 |
| RPi | `ip route` / `sudo nft list ruleset` | 指定の2区間とFWが存在 |
| 管理PC | `ssh <user>@10.2.0.2`、`ssh <user>@10.1.0.10` | 接続成功 |
| 管理PC以外のLAN端末 | RPi/Sparkへの新規SSH | 拒否 |
| Spark | `ping 10.1.0.1` | 成功 |
| Spark | `dig @10.0.0.254 example.com` と `dig +tcp @10.0.0.254 example.com` | 両方成功 |
| Spark | `curl -I --connect-timeout 5 https://example.com` | 成功 |
| Spark | `nc -vz -w 3 10.0.0.254 22`、同443 | 拒否 |
| Spark | 既知の開発LAN端末の開いているポートへ接続 | 拒否 |
| Spark | `dig +time=2 +tries=1 @1.1.1.1 example.com` | 拒否 |
| Spark | `curl -6 --connect-timeout 5 https://example.com` | 失敗 |
| Spark | RPi 10.1.0.1へのTCP22/445 | 拒否（445はNAS許可後のみ成功） |

公開IPへのpingは初期設定では許可していないため、その失敗をインターネット障害と判断しない。
拒否対象は必ず稼働中・ポート開放済みのテスト用端末で確認し、RPiのdropカウンター増加も確認する。
RPiの拒否ログは`journalctl -k`、カウンターは`sudo nft list table inet taco_filter`で確認。
上流OpenWrt単独の防御も確認する場合、RPi自身から開発LAN端末への接続を試し、OpenWrtで拒否されることを確認する。
RPiのoutputにも同様の制限があるため、切り分けはコンソールでルールのカウンター/キャプチャを使う。
既存接続は状態追跡で残るため、ポリシー変更の試験は新規接続で行う。

## 5. 復旧

問題時はSpark側を抜き、コンソールで`sudo sysctl -w net.ipv4.ip_forward=0`を実行してから復旧する。
RPiは保存したnftables.conf・NetworkManagerプロファイル・sysctl設定を戻し、旧プロファイルの自動接続を元に戻す。
本設定の90-taco-router.confを残したまま再起動すると転送が再び有効になる点に注意。
OpenWrtは追加したセクションを取り除き、ポートのブリッジ/VLAN所属を元に戻すか、保存したバックアップから復旧する。
開発LANの既存アドレスやWi-Fiは変更不要。

## 根拠資料

- OpenWrt fw4: https://openwrt.org/docs/guide-user/firewall/firewall_configuration
- DSA: https://openwrt.org/docs/guide-user/network/dsa/converting-to-dsa
- NetworkManager keyfile: https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nm-settings-keyfile.html
- nftables: https://www.netfilter.org/projects/nftables/manpage.html

実機の既存設定は未取得。生成処理・不正な入力の拒否をローカルで確認済み。
サンプルの`nftables.conf`はLinuxコンテナ（Debian bookworm、NET_ADMIN付き）で`nft -c -f`のチェック成功を確認済み。
NICドライバー、実ルーティング、OpenWrtの`fw4 check`、NetworkManagerの実機での適用、
通信の許可・拒否は未検証。上記手順で確認する。
