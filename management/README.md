# 開発NW 管理ツール

TACO上の通信・死活監視Webコンソール、AI側インターネットON/OFF操作、管理PCで実行するバージョン収集ツール。
既存の `config.json` に合わせ、AI網は `10.1.0.0/24`、TACO上流IPは `10.2.0.2` を対象にする。
通信ONでも既存FWの許可範囲は広げない。死活監視用のTACO→DGX Pingだけを追加許可する。GPIO物理スイッチの制御はまだ含まない。

## 1. TACO通信コンソール

### 確認できるもの

- 現在のIPv4追跡エントリ：送信元／宛先IP・ポート、プロトコル、TCP状態、接続単位の送受信バイト数。
- AI網から開始した通信を既定表示。TACO自身の通信もフィルターで選択可能。
- 観測履歴：5秒間隔で記録し、画面には直近500件。終了済みの保持は24時間、現在分を含む件数の目安は2万件。現在追跡中の通信は削除対象外。
- 拒否ログ：直近5分、抽出前最大1000ログ・画面最大200件。`taco-input-drop` / `taco-forward-drop` / `taco-output-drop` を対象とする。
- IP・ポート・状態で検索、プロトコル別表示、収集スナップショットのJSON保存。
- OpenWrt・TACO・DGXの死活状態、最終正常確認時刻、Pingの応答時間。
- 操作キーでAI側のインターネット通信をON/OFF。実際のカーネル状態を確認して表示する。

収集元は `conntrack -L -f ipv4 -o extended,id` と `journalctl -k`。既存nftablesのログは拒否通信が中心なので、それだけから許可セッションを再構成しない。

**観測の制約**：5秒間隔のスナップショットなので、その間に作成・消滅した通信は見逃す場合がある。履歴の初回／最終観測は接続開始／終了の正確な時刻ではない。追跡エントリがあることだけでは到達成功やアプリケーション成功を保証しない。TCP終了待ち・UDPの無応答エントリも含む。完全な監査ログ用途ではない。

拒否ログは既存FWのレート制限を受け、ログ出力より前にdropする送信元不整合・invalidなどのルールは表示されない。ログ件数は拒否パケット総数ではない。サービス開始前のセッション履歴は復元できない。再起動前の拒否ログは今回の画面対象外。IPv6・URL・通信内容・DNS問い合わせ名は収集しない。サービス名はポート番号からの推定で、アプリの識別結果ではない。

### 接続方法

```text
管理PCのブラウザー → localhost:8765 → SSHトンネル → TACO localhost:8765
                                                     |
                               非特権Webサーバー ← JSON収集データ
                                                     ↑
                               特権collector → conntrack／kernel log
                               非特権Web → 専用Unix socket → 特権control
                                                                  ├ nftゲート
                                                                  └ 固定ノードのPing
```

Webサーバーは `127.0.0.1` にだけ待ち受ける。既存の管理PC→TACO TCP 22許可を利用し、新しいWebポートのFW許可は不要。
閲覧はSSH認証とループバック限定で制限する。通信の変更には追加の操作キーが必要。TACOや管理PC内の他ユーザーも閲覧可能なので、共有端末の利用には注意する。
別ホスト名によるアクセスはHost検証で拒否する。ブラウザーは下記の `127.0.0.1:8765` または `localhost:8765` を使用する。

### TACOへの導入

以下は**TACO上**で行う。Python 3.9以降、systemd、既存の `taco_filter` FWを前提とする。

```sh
sudo apt update
sudo apt install python3 conntrack nftables iputils-ping
```

管理PCから、このリポジトリの `management` フォルダを転送する。
`YOUR_USER` はTACOにSSHできるユーザー名へ変更する。

```sh
ssh YOUR_USER@10.2.0.2 'mkdir -p ~/network-management'
scp -r management/. YOUR_USER@10.2.0.2:~/network-management/
```

TACOで導入する。既存サービス定義とアプリファイルがある場合は更新する。**初回・再導入ともAI側インターネットはOFFになる。**
既存 `/etc/nftables.conf` は構文検証・バックアップ後、末尾に専用設定のincludeを追加する。既存ルール本体・NetworkManager設定は置換せず、conntrackテーブルも消去しない。
`flush ruleset` と `table inet taco_filter` を含む既存構成が前提。異なる構成や構文チェック失敗時は自動統合せず停止する。

```sh
sudo sh ~/network-management/install-console.sh
sudo systemctl status taco-console-collector taco-console-control taco-console-web --no-pager
```

`collector` はrootで接続情報を読む。`control` はrootで固定対象のPingと固定ゲートのみを操作する。WebサーバーはsystemdのDynamicUserで動作し、専用グループのUnix socketを通じてstatus/enable/disableのみを要求する。HTTPから任意のコマンド・IP・ルールは受け付けない。3サービスとも通常は `active (running)`。

バイト数が `—` になる場合、接続追跡のアカウンティングが無効、または無効時に作られた接続の可能性がある。必要ならTACOで次を実行する。これはFWの許可範囲を変更しない。

```sh
sudo modprobe nf_conntrack
sudo sysctl -w net.netfilter.nf_conntrack_acct=1
```

永続化する場合は `/etc/sysctl.d/91-taco-monitor.conf` を新規作成し、次の1行を保存する。

```ini
net.netfilter.nf_conntrack_acct = 1
```

再起動時に該当モジュールがまだロードされていなければsysctlの適用に失敗することがある。その場合は `/etc/modules-load.d/taco-monitor.conf` に `nf_conntrack` を指定する。カウンターは主に設定後に作成された新しい通信から確認する。既存接続を切断する必要はない。

### 管理PCから開く

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 YOUR_USER@10.2.0.2
```

この端末を開いたまま、ブラウザーで [通信コンソール](http://127.0.0.1:8765) を開く。SSHトンネルの終了は `Ctrl+C`。
SSH側でポート転送を禁止している場合は、sshdの `AllowTcpForwarding` / `PermitOpen` と鍵の制限を確認する。Webポートを外部公開して回避しない。

### 死活監視

| ノード | 確認方法 | 周期 |
|---|---|---|
| OpenWrt | TACOのeth0から10.2.0.1にICMP Ping | 約10秒 |
| TACO | 制御サービス自身が応答すること | 状態取得時／監視周期 |
| DGX Spark | TACOのeth1から10.1.0.10にICMP Ping | 約10秒 |

TACOの既存OUTPUTは非公開宛先を拒否するので、専用設定にTACO 10.1.0.1 → DGX 10.1.0.10のICMP echo-request許可を1つ追加する。これ以外のLAN宛て許可は追加しない。AI通信OFF中もこの監視は継続する。
DGX自身のホストFWがPingを拒否している場合は「応答なし」になる。電源断と断定しない。TACOの表示は制御サービスの稼働確認で、NIC・ディスク・インターネット疎通の総合診断ではない。
Webとの接続断や制御サービス停止時は、状態不明・監視データが古いことを表示する。最終正常時刻は制御サービスのメモリー内に保持し、サービス再起動でリセットする。

### AI側インターネットON/OFF

TACOで操作キーを確認し、Web画面の「操作キー」に入力する。

```sh
sudo cat /etc/taco-console/control.token
```

キーはブラウザーの永続ストレージに保存しない。画面再読み込み後は再入力する。状態の閲覧には不要。
HTTP操作は同一Origin、固定JSON形式、Bearerキーを検証する。キーをURLやレポートに含めない。

| 通信 | OFF | ON |
|---|---|---|
| AI網が開始した上流向け通信・その戻り | 遮断。DNS・NTPと既存TCPも含む | 元のFW許可に従う（主にDNS、80/443、123） |
| 管理PCが開始したDGX／TACOへのSSH・Ping | 維持 | 維持 |
| DGX→TACO下流IPへのPing | 維持 | 維持 |
| TACO自身のDNS・更新・ノード監視 | 維持 | 維持 |
| AI網から開発LANへの任意接続 | 元のFWで拒否 | 元のFWで拒否 |

専用 `inet taco_console` テーブルは、既存FWのestablished/related許可より先のforward priority -10で制御する。AI側から開始した通信の往復を確認するため、OFFにした後も確立済み接続だけが通り続ける状態を避ける。ONは既存FWで許可された通信だけを通し、許可先は増やさない。IPv6の上流転送はこのテーブルでも拒否する。
管理PCから開始したSSHの応答は維持するので、物理的な完全分離ではない。同じAI網の端末間通信は対象外。TACO自身からの外部通信もこのスイッチの対象外。

起動時・control再起動時はOFF。ONの許可要素にはカーネル側で20秒の有効期限を設定し、正常稼働中は4秒ごとに更新する。control停止・ハング時は、通常停止なら即時、強制停止等でも最後の更新から最大20秒でOFFになる。Web画面を閉じてもcontrolが稼働していればONを維持する。
許可期限が一度切れた場合や、通常のnftables設定再読み込みでテーブルが作り直された場合はONを自動復元しない。画面から改めてONにする。
rootによるルール削除・別FW・flowtableなどの迂回はこの仕組みでは防げない。RPiでのflowtable offloadは非対応で、検出した場合controlは起動しない。

現在の状態・操作履歴：

```sh
sudo nft list table inet taco_console
sudo journalctl -u taco-console-control -n 80 --no-pager
```

ゲートで拒否した新規のAI通信も、レート制限付きで既存の拒否ログ表示に含める。既存接続のconntrackエントリはOFF後も残ることがあるが、通信継続を意味しない。

### 障害確認・停止

```sh
sudo journalctl -u taco-console-collector -u taco-console-control -u taco-console-web -n 80 --no-pager
sudo conntrack -L -f ipv4 -o extended,id
sudo journalctl -k --since '5 minutes ago' --grep 'taco-(input|forward|output)-drop'
```

収集エラー・更新停止は画面に表示する。エラー時の「0件」を通信なしと解釈しない。

```sh
sudo systemctl disable --now taco-console-web taco-console-collector taco-console-control
```

停止してもFWは維持され、AI側インターネットはOFFになる。nftables自体は停止しない。データは `/var/lib/taco-console/history.sqlite3` と `state.json`。
DBはrootのみ読み書き可、Web向けJSONはローカルユーザーから読み取り可能。
削除した履歴のSQLite内の空き領域は再利用するがファイル自体は自動縮小しない。

緊急にWeb以外からOFFにする場合：

```sh
sudo python3 /opt/taco-console/control.py off
```

controlが応答すればdisableを要求し、応答しなければ許可setを直接空にする。ハング中で直接操作した場合も、controlは期限切れ／空のsetを検出するとONを自動復元しない。

元の常時許可構成へ戻す場合は、3サービスを停止した後、`/etc/nftables.conf` の `include "/etc/nftables.d/taco-console.nft"` を削除するか、保存された `/etc/nftables.conf.taco-console-*.bak` から適切なバックアップを選んで復元する。構文を `sudo nft -c -f /etc/nftables.conf` で確認し、SSH接続への影響を確認して `sudo nft -f /etc/nftables.conf` で再読み込みする。ゲートテーブルを削除するだけでは次の再起動で復元されるので、永続設定も合わせて変更する。

### 管理PCでデモを確認

Python以外の追加ライブラリは不要。実機への接続・FW変更は行わない。

```sh
python3 management/taco-console/console.py demo
```

[デモ画面](http://127.0.0.1:8765) にはDEMOラベルを表示する。例示用宛先IPを使用し、実際の通信結果と区別する。

## 2. バージョン収集・更新候補確認

### 前提

管理PCにPython 3.9以降とOpenSSHが必要。macOS／Linuxを想定する。
管理PCから両機器へのSSH鍵認証、ホスト鍵の登録を先に完了する。
スクリプトは `BatchMode=yes`、`StrictHostKeyChecking=yes` を使用し、パスワード入力や未知のホスト鍵の自動承認はしない。
SSHポートや鍵は `~/.ssh/config` で設定できる。SSH別名も引数に使用可能。

```sh
ssh root@10.0.0.254
ssh YOUR_USER@10.2.0.2
```

初回のホスト鍵を正しい機器のものと確認して登録し、各接続を終了してから収集する。
OpenWrtのSSHユーザーやIPを変更済みの場合は実値を指定する。

### 実行

リポジトリのルートで実行する。

```sh
python3 management/check_versions.py \
  --openwrt root@10.0.0.254 \
  --taco YOUR_USER@10.2.0.2 \
  --output management/version-reports
```

- **OpenWrt**：OSリリース、カーネル、アーキテクチャ、ubusの機種・target情報、インストール済みパッケージ。`opkg` と `apk` の双方に対応。
- **TACO**：OS、稼働中カーネル、アーキテクチャ、dpkgパッケージ一覧、APTインデックスファイルの時刻、`apt-get -s upgrade` の更新候補。
- **公式最新版**：管理PCからOpenWrt公式の [リリース一覧JSON](https://downloads.openwrt.org/.versions.json) を取得し、安定版および同じ系列の最新リリースと比較する。機種対応は自動判定しない。

標準実行は機器の設定やパッケージ一覧を更新せずに収集する。TACOのAPT候補は既存キャッシュに基づくため、「候補なし」を最新と断定しない。
インターネットが不要な収集モードは `--offline`。SSH収集は行い、公式リリース取得のみ省略する。

### TACOの最新パッケージ候補も確認する場合

一番簡単なのはTACOで次を実行してから、管理PCでスクリプトを再実行する方法。

```sh
sudo apt-get -o APT::Update::Error-Mode=any update
```

これが更新するのは候補情報だけで、パッケージのインストールはしない。
標準実行のレポート上は、実施済みでも「キャッシュ参照・最新性未確認」と表示する。スクリプト外での更新実施は自動確認できないため、実施日時を合わせて記録する。

すでにTACOのSSHユーザーが非対話sudoを利用できる場合は、管理PCから次のように一括実行できる。

```sh
python3 management/check_versions.py \
  --openwrt root@10.0.0.254 \
  --taco YOUR_USER@10.2.0.2 \
  --output management/version-reports \
  --refresh-apt --timeout 300
```

`--refresh-apt` を指定したときだけTACO上で `sudo -n apt-get -o APT::Update::Error-Mode=any update` を実行する。
sudo認証が必要なら失敗を記録し、既存キャッシュの候補として表示する。この機能のために広いNOPASSWD権限を追加する必要はない。
外向き通信を物理スイッチ等で制限している場合は、その許可が必要。

### 保存されるファイル

標準出力は、OpenWrt／TACOそれぞれの判定・バージョン・更新件数と保存先だけを表示する。進捗・パッケージ一覧・但し書きは表示しない。
詳細なパッケージ一覧、取得エラー、注意事項は `report.md` とJSON／生ログに保存する。`report.md` の冒頭にも簡易サマリーを表示する。
**緑文字・🟢は確認範囲で最新、赤文字・🔴は更新候補あり／要確認／判定不能**。
OpenWrtは今回取得した公式安定版と一致した場合、TACOは `--refresh-apt` による情報更新が成功し、upgrade候補・保留がない場合に緑になる。
キャッシュ参照だけの場合は、候補が0件でも最新性を確認できないため赤で表示する。
ホスト名・パッケージ一覧など付随情報の取得失敗は、版の比較結果とは分けて赤い「収集上の注意」に表示する。OpenWrtの版と公式最新版の比較に成功していれば、付随情報に失敗しても版の判定は緑になる。
APTのパッケージ一覧もバージョンを色分けする。現在の旧バージョンは赤、候補バージョンは今回のAPT情報更新に成功した場合に緑。キャッシュ参照などで候補の最新性を確認できない場合は候補も赤にする。

端末ではANSI色を使用する。通常は端末への出力時だけ有効で、リダイレクト時は丸印のみ残す。`--color always` で強制有効、`--color never` でANSI色を無効化でき、`NO_COLOR` 環境変数もauto時に尊重する。
`report.md` はHTMLの文字色指定を含む。ビューアが文字色を無効化する場合でも、🟢／🔴と判定文で見分けられる。ANSI制御文字はMarkdownファイルに保存しない。

```text
management/version-reports/20261026T....Z/
├── report.md          人が読む判定・更新候補
├── versions.json      全機器の収集結果・比較結果・公式取得データ
├── openwrt.raw.txt    SSHコマンドの生出力
└── taco.raw.txt       SSHコマンドの生出力
```

時刻はUTC。実行ごとに別ディレクトリに保存し、既存レポートを上書きしない。
ディレクトリは原則0700、生成ファイルは0600。パッケージ一覧・ホスト名などを含むので公開リポジトリには追加しない。
終了コードは正常時0、SSH・個別コマンド・公式情報取得の失敗があれば2。失敗時も取得できた結果は保存する。

### 判定の範囲

- 更新可能な版の存在と、更新を今すぐ実施すべきかは別。リリースノート、脆弱性、機種対応を確認する。
- OpenWrtの世代が違えば移行計画を確認する。同じ系列の新しい版も併記する。
- OpenWrtのSNAPSHOT／独自版は安定版との単純比較を行わず、要確認とする。
- TACOのAPT比較は設定済みリポジトリ・pin・holdの範囲に従う。依存関係変更が必要な更新や保留は生ログも確認する。
- TACOのOS世代更新、Pi EEPROM・ブートローダー、独自ビルド、手動導入ソフト、Dockerイメージ等は最新性を自動判定しない。
- 稼働中カーネルとインストール済みカーネルパッケージの差から再起動が必要な場合もある。自動更新・再起動は行わない。
- 公式情報取得不能や候補取得失敗は「判定不能」。アップデート不要とは表示しない。

Codexに依頼するときは、生成フォルダを指定し「versions.jsonと生ログを読み、機種・OS系列に対応する最新公式情報から更新要否を確認して」と伝える。

## 3. ローカル検証

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s management/tests -v
node --check management/taco-console/static/app.js
sh -n management/install-console.sh
```

実機のsystemd、接続追跡、journald権限、SSH認証、ネットワーク経路は導入先での確認が必要。

ゲートのLinuxカーネル試験は、本番機器ではなく使い捨てDocker環境だけで実行する。

```sh
docker build -t taco-console-test -f management/tests/Dockerfile .
docker run --rm --network none --cap-add NET_ADMIN --cap-add SYS_ADMIN \
  --security-opt apparmor=unconfined --security-opt seccomp=unconfined \
  --sysctl net.ipv4.ip_forward=1 -e TACO_GATE_DISPOSABLE_TEST=1 \
  --mount type=bind,source="$PWD",target=/work,readonly \
  taco-console-test python3 -B /work/management/tests/linux_gate_smoke.py
```

試験は仮想AI／上流を作り、実際のパケットでON/OFF・既存接続遮断・管理SSH維持・DNS・Ping・許可期限切れを確認する。

## 参考

- [conntrackコマンドの仕様](https://manpages.debian.org/bookworm/conntrack/conntrack.8.en.html)
- [APT updateとシミュレーションの仕様](https://manpages.debian.org/bookworm/apt/apt-get.8.en.html)
- [OpenWrt公式リリース一覧](https://downloads.openwrt.org/.versions.json)
