# DGX Spark ネットワーク管理資料

作成日：2026-10-25（日本時間）／管理機能追記：2026-10-26／版：1.1

対象：OpenWrt（保存時ホスト名 R7800）、Raspberry Pi、DGX Spark、管理PC

本資料は `config.json`、`output/` の生成設定、`openwrt/before_work` の保存設定に基づく。生成設定を適用したときの構成・許可条件を記載しており、実機への適用完了や通信試験の合格を示すものではない。

## 1. NW構成図

```text
                         インターネット（公開IPv4）
                                   |
                         WAN：DHCP / NATあり
                                   |
                    +-----------------------------+
                    | OpenWrt（R7800）             |
                    | LAN / DNS：10.0.0.254/24      |
                    | AI側IF：10.2.0.1/30           |
                    +-----------------------------+
                       |                       |
       br-lan：lan1～3・既存Wi-Fi          lan4：ai_transit
                       |                  FWゾーン：ai
              開発LAN 10.0.0.0/24               |
                       |              中継網 10.2.0.0/30
              管理PC 10.0.0.9                   |
                                      eth0：10.2.0.2/30
                                   +--------------------+
                                   | Raspberry Pi       |
                                   | IPv4ルーティング   |
                                   | nftables / NATなし |
                                   +--------------------+
                                      eth1：10.1.0.1/24
                                                |
                                       AI網 10.1.0.0/24
                                                |
                                   +--------------------+
                                   | DGX Spark          |
                                   | 10.1.0.10/24       |
                                   | GW：10.1.0.1       |
                                   | DNS：10.0.0.254    |
                                   +--------------------+
```

- 管理経路：管理PC → OpenWrt → RPi／Spark。SSHとPingを許可する。
- 外部通信：Spark → RPi → OpenWrt → WAN。RPiではNATせず、OpenWrtの既存WAN masqueradeを使用する。
- DNS：SparkとRPiがOpenWrtの `10.0.0.254` に直接問い合わせる。RPiにDNSサーバーは設けない。
- 中継網・AI網にはDHCPサーバーを設けない。RPiとSparkは固定IPv4を使用する。
- `lan4` は開発LANのブリッジから分離する。保存設定では `br-lan` の有線メンバーは `lan1`～`lan3`。
- 既存Wi-Fiの所属はREADME上の設計。無線設定の保存ファイルは今回の資料作成では確認していない。

## 2. IPアドレス台帳

### 2.1 セグメント

| 用途 | ネットワーク | マスク | 利用可能ホスト | ブロードキャスト | アドレス配布 |
|---|---|---|---|---|---|
| 開発LAN | 10.0.0.0/24 | 255.255.255.0 | 10.0.0.1～10.0.0.254 | 10.0.0.255 | 既存OpenWrt DHCP／固定IP |
| OpenWrt–RPi中継 | 10.2.0.0/30 | 255.255.255.252 | 10.2.0.1～10.2.0.2 | 10.2.0.3 | 固定IPのみ |
| AI網 | 10.1.0.0/24 | 255.255.255.0 | 10.1.0.1～10.1.0.254 | 10.1.0.255 | 固定IPのみ |

### 2.2 機器・インターフェース

| 機器 | IF／接続先 | IPv4／プレフィックス | デフォルトGW | DNS | 割当方法・根拠 |
|---|---|---|---|---|---|
| OpenWrt | lan / br-lan | 10.0.0.254/24 | WAN側で取得 | 上流設定に従う | 固定／保存設定で確認 |
| OpenWrt | ai_transit / lan4 | 10.2.0.1/30 | 追加しない | ― | 固定／生成設定 |
| OpenWrt | wan / wan | DHCPで取得・値未記録 | DHCPで取得 | 上流設定に従う | 保存設定ではDHCP |
| 管理PC | 開発LAN接続IF・名称未記録 | 10.0.0.9/24 | 10.0.0.254（設計） | 10.0.0.254（設計） | 固定またはDHCP予約／許可元IPはconfig.json指定 |
| RPi | eth0 / OpenWrt lan4 | 10.2.0.2/30 | 10.2.0.1 | 10.0.0.254 | 固定／taco-uplink |
| RPi | eth1 / Spark側 | 10.1.0.1/24 | このIFには設定しない | このIFには設定しない | 固定／taco-ai |
| DGX Spark | 有線IF・名称未記録 | 10.1.0.10/24 | 10.1.0.1 | 10.0.0.254 | 固定／spark-settings.txt |

管理PCの実際のIF名、マスク、GW、DNS、MACアドレス、およびSparkのIF名・MACアドレスは実機確認後に追記する。未記載のIPは、他機器が使っていないことを確認するまで空きアドレスと扱わない。

保存設定の開発LAN DHCP範囲は `start=1, limit=200`。管理PCの `10.0.0.9` はこの範囲内なので、DHCP予約または配布範囲の調整によって重複を防ぐ。今回の生成設定には管理PCのDHCP予約は含まれない。

### 2.3 ルーティング・NAT

| 機器 | 宛先 | ネクストホップ | 出力IF | 種別 |
|---|---|---|---|---|
| OpenWrt | 10.0.0.0/24 | 直接接続 | br-lan | 既存 |
| OpenWrt | 10.2.0.0/30 | 直接接続 | ai_transit / lan4 | 追加 |
| OpenWrt | 10.1.0.0/24 | 10.2.0.2 | ai_transit / lan4 | 追加静的ルート ai_subnet |
| OpenWrt | デフォルト | WAN DHCPで取得 | wan | 既存・実値未記録 |
| RPi | 10.2.0.0/30 | 直接接続 | eth0 | 固定IPに伴う経路 |
| RPi | 10.1.0.0/24 | 直接接続 | eth1 | 固定IPに伴う経路 |
| RPi | デフォルト（開発LAN向けを含む） | 10.2.0.1 | eth0 | taco-uplink |
| Spark | 10.1.0.0/24 | 直接接続 | 有線IF | 固定IPに伴う経路 |
| Spark | デフォルト | 10.1.0.1 | 有線IF | 固定設定 |

OpenWrtのWANゾーンは保存設定で `masq=1`。保存設定に `masq_src` 制限はない。AIゾーンのmasqueradeは無効、RPiにはNATルールを設けない。

## 3. Firewallポリシー表

### 3.1 基本方針と適用範囲

以下はAI構成に関する新規通信のポリシー。既存LAN/WANの全ポリシーを置き換えるものではない。

| 適用箇所 | 既定動作 | 補足 |
|---|---|---|
| OpenWrt aiゾーン INPUT | REJECT | ルーター自身宛て。DNSとRPiからのPingを個別許可 |
| OpenWrt aiゾーン FORWARD | REJECT | 個別ルールで許可。ai→wan／lan→aiの全面許可は追加しない |
| OpenWrt aiゾーン OUTPUT | ACCEPT | OpenWrt自身からai側への送信 |
| RPi INPUT / FORWARD / OUTPUT | DROP | 各チェーンで必要な通信のみ許可 |
| RPi 管理コンソールの追加ゲート（導入時） | AI側の上流通信は起動時OFF | forward priority -10。操作キーでON/OFF。既存接続の往復も対象 |
| 応答通信 | 許可 | established / related。RPiではIPv6・送信元不整合・invalidの拒否が先行 |
| RPi IPv6 | 拒否・無効化 | nftablesとsysctl、接続プロファイルで制御。loopbackは例外 |
| Spark IPv6・Wi-Fi | 無効化する設計 | ホスト側での実設定確認が必要 |

OpenWrtの既存LAN/WAN IPv6設定は維持される。保存設定にはWANからのICMPv6転送許可もあるため、「OpenWrt全体でIPv6を全面拒否」とは扱わない。AI側のIPv6入力・転送制限は実機ルールを確認し、RPi側の無効化・拒否も必ず適用する。

管理コンソール最新版の導入時は、以下の許可表に加えて `inet taco_console` のゲートが適用される。
OFFはAI網から開始した上流向け通信とその戻りを遮断し、指定DNS・NTP・既存HTTPSも止める。
ONは以下の既存許可範囲に従う。管理PCが開始したSSH/PingとTACO自身の通信は維持する。
監視のためTACO 10.1.0.1 → Spark 10.1.0.10のICMP echo-requestのみ追加許可する。
Webボタン、操作キー、20秒の許可期限、再起動時OFFの詳細は `management/README.md` を参照。導入済みかどうかは実機確認が必要。

### 3.2 許可する新規通信

| ID | 送信元 | 宛先 | プロトコル／宛先ポート | 動作 | 適用箇所・ルール |
|---|---|---|---|---|---|
| P01 | 管理PC 10.0.0.9 | RPi 10.2.0.2 | TCP 22 | 許可 | OpenWrt ai_admin_ssh → RPi INPUT |
| P02 | 管理PC 10.0.0.9 | RPi 10.2.0.2 | ICMP echo-request | 許可 | OpenWrt ai_admin_ping → RPi INPUT |
| P03 | 管理PC 10.0.0.9 | Spark 10.1.0.10 | TCP 22 | 許可 | OpenWrt ai_admin_ssh → RPi FORWARD。Spark側SSH/FW設定も必要 |
| P04 | 管理PC 10.0.0.9 | Spark 10.1.0.10 | ICMP echo-request | 許可 | OpenWrt ai_admin_ping → RPi FORWARD。Spark側FWも確認 |
| P05 | AI網 10.1.0.0/24 | OpenWrt 10.0.0.254 | TCP / UDP 53 | 許可 | RPi FORWARD → OpenWrt ai_dns |
| P06 | RPi 10.2.0.2 | OpenWrt 10.0.0.254 | TCP / UDP 53 | 許可 | RPi OUTPUT → OpenWrt ai_dns |
| P07 | AI網 10.1.0.0/24 | 公開IPv4・WAN経由 | TCP 80 / 443 | 許可 | RPi FORWARD → OpenWrt ai_web |
| P08 | RPi 10.2.0.2 | 公開IPv4・WAN経由 | TCP 80 / 443 | 許可 | RPi OUTPUT → OpenWrt ai_web |
| P09 | AI網 10.1.0.0/24 | 公開IPv4・WAN経由 | UDP 123 | 許可 | RPi FORWARD → OpenWrt ai_ntp |
| P10 | RPi 10.2.0.2 | 公開IPv4・WAN経由 | UDP 123 | 許可 | RPi OUTPUT → OpenWrt ai_ntp |
| P11 | AI網 10.1.0.0/24・eth1受信 | RPi 10.1.0.1 | ICMP echo-request | 許可 | RPi INPUT。現構成はSpark単体だが、ルールはAI網全体が対象 |
| P12 | RPi 10.2.0.2 | OpenWrt 10.2.0.1 | ICMP echo-request | 許可 | RPi OUTPUT → OpenWrt ai_transit_ping |
| P13 | RPi自身 | 必要な宛先 | ICMP destination-unreachable / time-exceeded / parameter-problem | 許可 | RPi OUTPUT。経路・通信エラー通知 |
| P14 | 許可通信の相手 | 元の通信元 | 応答・関連通信 | 許可 | 状態追跡。戻り方向の全面許可ルールは不要 |

「公開IPv4」は、3.4の除外先を除いた宛先を表す。DNSサービス側の受信制限がある場合、FW許可だけでは名前解決は成功しない。

### 3.3 拒否・任意許可

| ID | 送信元 | 宛先・通信 | 動作 | 制御箇所・理由 |
|---|---|---|---|---|
| D01 | 管理PC以外の開発LAN端末 | RPi／Sparkへの新規通信 | 拒否 | OpenWrtの個別許可対象外。RPiも個別許可対象外 |
| D02 | AI網／RPi | 開発LAN・その他の非公開宛先 | 拒否 | 指定DNSと中継GWへのPingを除く。OpenWrt ai_block_special、RPi non_public_v4 |
| D03 | AI網／RPi | OpenWrtのSSH / Web管理など | 拒否 | ai INPUTの既定REJECT。TCP / UDP 53、RPi→10.2.0.1 Pingのみ例外 |
| D04 | AI網／RPi | 外部DNS TCP / UDP 53 | 拒否 | 許可先は10.0.0.254のみ |
| D05 | AI網／RPi | 公開IPv4へのPing・上記以外の新規通信 | 拒否 | 公開宛先にはTCP 80/443・UDP 123のみ許可 |
| D06 | AI網 | RPiへのTCP 22 / 445 | 拒否 | RPi INPUTの許可対象外。445は任意設定あり |
| D07 | eth1受信で送信元が10.1.0.0/24以外 | RPi自身／転送先 | DROP | RPi INPUT / FORWARDの送信元検査 |
| D08 | eth0受信で送信元が10.1.0.0/24 | RPi自身／転送先 | DROP | RPi INPUT / FORWARDの送信元検査 |
| D09 | RPiを通るIPv6・RPi自身のIPv6 | INPUT / FORWARD / OUTPUT | DROP | RPi nftables。loopbackは例外。IPv6自体も無効化 |
| O01 | Spark 10.1.0.10 | RPi 10.1.0.1 TCP 445 | 現在は無効 | Samba認証・共有を設定後、RPi INPUTのコメントを解除した場合のみ許可 |

RPiの拒否はDROP、OpenWrtのAI側の拒否は原則REJECT。表示上の拒否結果やタイムアウトの違いは制御箇所による。

### 3.4 非公開・特殊宛先の拒否リスト

OpenWrt `ai_block_special` とRPi `non_public_v4` の対象：

```text
0.0.0.0/8       10.0.0.0/8       100.64.0.0/10    127.0.0.0/8
169.254.0.0/16  172.16.0.0/12   192.0.0.0/24     192.0.2.0/24
192.168.0.0/16  198.18.0.0/15   198.51.100.0/24  203.0.113.0/24
224.0.0.0/4     240.0.0.0/4
```

RPiでは指定DNSへの例外をこの拒否より先に評価する。OpenWrtではDNSはルーター自身宛てのINPUT、上記拒否は転送通信に適用する。生成したFirewallルールの順序を維持する。

### 3.5 運用上の制約

- HTTP/HTTPSは宛先ドメインを制限していない。HTTPS経由の外部送信・DoHも可能。
- 同一AI網内の端末間通信はRPiを通らない場合があり、このFWでは相互分離できない。現構成はSpark単体を想定。
- 保存済みOpenWrtのLAN INPUTはACCEPT。開発LANからOpenWrt自身へのPingはAI向け管理PC限定ルールとは別の既存ポリシーで許可される。
- 既存の広いACCEPT、DNAT、VPN、ポリシールーティングがある場合は追加ポリシーへの影響を確認する。
- RPiのnftables設定は `flush ruleset` で既存ルール全体を置換する。Docker/UFW/firewalldとの併用は想定しない。

## 4. 適用・確認記録

以下は本資料作成時点では未確認。実施日と結果を追記する。

| 確認項目 | 期待値・条件 | 状態／実施日 |
|---|---|---|
| 管理PCのアドレス固定 | 10.0.0.9を予約または配布範囲から除外 | 未確認 |
| OpenWrt lan4の分離 | br-lan外。保存設定では分離済み | 現在の実機は未確認 |
| OpenWrt設定の反映 | ai_transit、静的ルート、DHCP無効、FWルールが存在 | 未確認 |
| OpenWrt FW構文 | fw4 checkが成功 | 未確認 |
| WAN NAT | masquerade有効、AI／中継送信元を除外しない | 保存設定では条件を満たす。現在値は未確認 |
| DNSサービスの受信制限 | SparkからTCP/UDP両方で10.0.0.254に問い合わせ可能 | 保存設定はlocalservice=1。下記参照 |
| RPiのNIC対応 | eth0がOpenWrt側、eth1がSpark側 | 未確認 |
| RPiのFW・経路 | nftablesと2つの固定IP、IPv4転送が有効 | 設定作業中。完了未確認 |
| Spark固定IP | 10.1.0.10/24、GW 10.1.0.1、DNS 10.0.0.254 | 未確認 |
| IPv6・迂回経路 | RPi/SparkのIPv6、Wi-Fi、追加上流を無効化 | 未確認 |
| 再起動後の維持 | RPiのnftables、固定IP、経路が維持される | 未確認 |

保存設定ではdnsmasqの `localservice=1`。AI網はOpenWrtへの直接接続網ではないため、SparkからのDNS問い合わせが拒否される可能性がある。TCP/UDP 53の両方を試験する。必要に応じて既存dnsmasqセクションを `localservice=0` に変更する際は、WANや他の未信頼ゾーンからDNSへのアクセスが拒否されていることを先に確認する。独自のlisten/interface制限がある場合も確認する。

### 通信試験表

| 実行元 | 試験 | 期待値 | 結果／実施日 |
|---|---|---|---|
| OpenWrt | ip route get 10.1.0.10 | 10.2.0.2経由 | 未実施 |
| 管理PC | ping 10.2.0.1 | 成功。RPi未接続でもOpenWrt側IFを確認可能 | 未実施 |
| 管理PC | RPi 10.2.0.2／Spark 10.1.0.10へSSH・Ping | 成功 | 未実施 |
| 管理PC以外のLAN端末 | RPi／Sparkへ新規SSH | 拒否 | 未実施 |
| RPi | ping 10.2.0.1 | 成功 | 未実施 |
| Spark | ping 10.1.0.1 | 成功 | 未実施 |
| Spark | dig @10.0.0.254 example.com | UDP DNS成功 | 未実施 |
| Spark | dig +tcp @10.0.0.254 example.com | TCP DNS成功 | 未実施 |
| Spark | curl -I --connect-timeout 5 https://example.com | 成功 | 未実施 |
| Spark | OpenWrt 10.0.0.254のTCP 22/443へ接続 | 拒否 | 未実施 |
| Spark | 外部DNS 1.1.1.1へUDP 53で問い合わせ | 拒否 | 未実施 |
| Spark | 稼働中の開発LANテスト端末の開放ポートへ接続 | 拒否 | 未実施 |
| Spark | curl -6 --connect-timeout 5 https://example.com | 失敗 | 未実施 |
| Spark | RPi 10.1.0.1のTCP 22/445へ接続 | 拒否。445は任意許可後のみ成功 | 未実施 |

拒否試験は新規接続で行い、宛先サービスが稼働していることと、FWの拒否カウンター増加を確認する。公開IPへのPing失敗は今回の設計どおりであり、インターネット障害の判定には使用しない。

## 5. 管理対象ファイル

| 資料・設定 | リポジトリ内の保存先 | 用途 |
|---|---|---|
| 本資料 | docs/network-management.md | 構成・台帳・ポリシー・確認記録 |
| 管理ツール手順 | management/README.md | TACO通信コンソール・SSHバージョン収集の導入と運用 |
| 手順書 | README.md | 適用・検証・復旧手順 |
| 入力設定 | config.json | 管理PC IP・機器IFの指定 |
| 生成時入力の記録 | output/resolved-config.json | 生成設定との対応確認 |
| OpenWrt保存設定 | openwrt/before_work | 作業前に保存したnetwork/dhcp/firewallの参照 |
| OpenWrt追加設定 | output/openwrt/*.fragment | 既存設定に一度だけ追加する断片 |
| RPi設定 | output/rpi/ | nftables、NetworkManager、sysctl |
| Spark設定値 | output/spark-settings.txt | 固定IP・GW・DNS |

構成変更時は `config.json` と生成設定を更新し、本資料のIP台帳・ポリシー表・確認記録も更新する。実機のバックアップは別途取得し、保存日を記録する。
