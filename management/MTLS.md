# 管理PC限定のHTTPS・mTLS接続

ブラウザーから **https://radxa-console.home.arpa/** を開く。SSH転送の起動や、Web画面への操作キー入力は不要。

```text
管理PC 10.0.0.9
  └─ HTTPS / TCP 443 / クライアント証明書
      └─ OpenWrt：管理PC → 10.2.0.2:443 のみ許可
          └─ Radxa eth0：同じ送信元・宛先・ポートのみ許可
              └─ Caddy：接続元IP + CA検証 + 許可済み証明書の一致
                  └─ HTTP localhost:8765：内部キーを付与
                      └─ 既存のコンソール・制御サービス
```

例の管理PCは、これまでのSSH元 `10.0.0.9`。実際の固定IPが異なる場合は `--admin-ip` を変更する。Radxaの上流IFが異なる場合は `--uplink-if` も変更する。`config.example.json` の例は `.10` なので、そのまま実機値と思い込まないこと。

443はIPv4の管理側IP `10.2.0.2` だけで待ち受ける。HTTP/80・HTTP/3のUDP/443・Caddy管理APIは開かない。AI側IPの `10.1.0.1` には公開しない。管理PC以外は、正しいクライアント証明書があっても拒否する。

専用名を使うのは、mTLS時のTLS SNI名とHTTP Hostの厳密な一致を維持するため。IPアドレス直打ちでは利用しない。FWやOSのホスト名をこの名前に変更する必要はない。

## 1. 管理PCで証明書を作成する

リポジトリのルートで実行する。Python 3.9以上と `openssl` が必要。macOS標準のLibreSSLでも実行可能。WindowsではWSLなど、PythonとOpenSSLが使える環境で作成し、最後にCA公開証明書と `.p12` をWindowsへコピーする。

```sh
python3 management/mtls.py init
python3 management/mtls.py issue-server radxa-server-20261027
python3 management/mtls.py issue-client admin-pc-20261027
```

- `init`：専用CAを作成。CA秘密鍵を暗号化するパスフレーズを入力する。
- `issue-server`：CAのパスフレーズを入力してサーバー証明書を発行する。
- `issue-client`：CAのパスフレーズを入力し、続いて `.p12` の保護パスワードを設定する。十分長く、使い回していないものを使う。

CAは10年、サーバー・クライアント証明書は既定365日。サーバー証明書には専用DNS名と `10.2.0.2` のSANを設定する。既存のディレクトリや証明書は上書きしない。失敗途中のディレクトリがある場合も、新しい名前でやり直す。

出力はGit管理対象外の `output/mtls-pki/`。CA秘密鍵 `ca.key` は暗号化済み。クライアントの `key.pem` は0600・格納ディレクトリは0700で、`.p12` は別途パスワード保護する。CA秘密鍵・クライアント秘密鍵は管理PCで保管し、Radxaへ転送しない。

## 2. Radxaへ送る資材を作る

```sh
python3 management/mtls.py bundle \
  --server radxa-server-20261027 \
  --client admin-pc-20261027 \
  --admin-ip 10.0.0.9 --uplink-if eth0 \
  --output output/mtls-deploy-20261027
```

このディレクトリにはサーバー証明書・サーバー秘密鍵、CA公開証明書、許可するクライアントの**公開証明書だけ**、Caddy・FW設定を含める。CA秘密鍵・管理PCの秘密鍵・`.p12` は含まない。

管理PCが複数なら証明書を個別に発行できるが、現在のFWは指定した**1つの管理PC IPのみ**を許可する。`--client` を複数指定しても接続元IPの範囲は広がらない。

## 3. RadxaにCaddyを用意する

Radxa上のDebian / Raspberry Pi OSで実行する。**Caddy 2.11.7以上**が必要。古いOS標準パッケージではなく、[Caddy公式のstableパッケージ手順](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)を利用する。

```sh
sudo apt install debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o /tmp/caddy-stable.gpg.key
sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg /tmp/caddy-stable.gpg.key
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /tmp/caddy-stable.list
sudo install -m 0644 /tmp/caddy-stable.list /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo apt update
sudo apt install caddy
caddy version
```

公式パッケージは既定の `caddy.service` を自動起動する。このRadxaで他用途にCaddyを使っていない場合、既定サービスを止める。今回の専用サービスは `taco-console-caddy.service`。既存のCaddy設定は上書きしない。既に443を他用途で利用中なら、その設定との統合が必要。

```sh
sudo systemctl disable --now caddy.service
```

## 4. 資材転送とRadxaへの導入

管理PCで実行する。`YOUR_USER` はRadxaのSSHユーザー、`openwrt` は既存のSSH接続名へ読み替える。

```sh
scp -r management/. YOUR_USER@10.2.0.2:~/network-management/
scp -r output/mtls-deploy-20261027 YOUR_USER@10.2.0.2:~/console-mtls-20261027
```

Radxaで実行する。既存コンソールが導入済みであることが前提。

```sh
sudo sh ~/network-management/install-console.sh --update-console
sudo python3 ~/network-management/install_mtls.py ~/console-mtls-20261027
sudo systemctl status taco-console-caddy taco-console-web --no-pager
```

インストーラーは次を実行する。

- 既存の操作キーをCaddy専用のroot読取用EnvironmentFileにコピーする。ブラウザーへキーは返さない。
- Caddyを構文・証明書検証する。証明書とサーバー秘密鍵はCaddyグループのみ読める権限にする。
- `/etc/nftables.conf` をバックアップし、末尾へHTTPS許可用のincludeを追加して構文確認する。
- 動作中のFWには `taco_filter` のINPUTに専用ルールを1つだけ挿入する。更新時は同じコメントのルールを置き換え、重複を増やさない。
- 制御サービスを再起動せず、既存の対外接続の開閉状態を維持する。nftables全体のreloadも行わない。
- 途中失敗時は変更した設定と専用ルールを復元し、以前のCaddyが稼働していた場合はその設定で再起動する。エラー時は出力とサービスログも確認する。

rootのバックアップ先は `/etc/taco-console/backups/mtls-<時刻>/`。OS再起動や制御サービス再起動時の「閉」初期化は変更しない。通常のコンソール更新ではmTLS資材を変更しない。

## 5. OpenWrtのFWへ許可を追加する

管理PCで生成済みスクリプトを送る。

```sh
scp output/mtls-deploy-20261027/openwrt-apply.sh openwrt:/tmp/console-mtls-apply.sh
ssh openwrt 'sh /tmp/console-mtls-apply.sh'
```

スクリプトは未確定のUCI変更があれば停止する。既存FWをバックアップし、名前付きの `ai_admin_console` ルールだけを設定、`fw4 check`、`uci commit firewall`、FW reloadの順で適用する。reload失敗時は保存済み設定へ戻す。内容を手で確認・統合する場合は同梱の `openwrt.fragment` を使う。

ルールは「LAN側の管理PC `10.0.0.9` → AI側のRadxa `10.2.0.2`、TCP 443」のみ。LAN全体・AIセグメント・WAN全体の許可は追加しない。OpenWrt→Radxaで管理PCの送信元IPがNAT変換される構成には対応しない。既存のAI側zoneはmasqueradeなしが前提。

## 6. 管理PCに証明書と接続名を登録する

登録するものは次の2つ。

1. `output/mtls-pki/ca.crt`：サーバー証明書を信頼するためのCA公開証明書。
2. `output/mtls-pki/admin-pc-20261027/identity.p12`：クライアント証明書と秘密鍵。作成時の `.p12` パスワードが必要。

CA登録前に、作成元PCの `openssl x509 -in output/mtls-pki/ca.crt -noout -fingerprint -sha256` で表示した指紋と、登録するCAの指紋が同じことを確認する。

### macOS：Safari / Chrome / Edge

- 「キーチェーンアクセス」の「システム」に `ca.crt` を読み込む。証明書を開き、「信頼」のSSL設定を信頼するよう変更する。
- 「ログイン」キーチェーンに `identity.p12` を読み込む。作成時のパスワードを入力し、「自分の証明書」で証明書の下に秘密鍵があることを確認する。
- `/etc/hosts` に次の1行を追加する。既に同じ名前がある場合はその行を修正し、重複させない。

```text
10.2.0.2 radxa-console.home.arpa
```

hostsの編集は `sudo nano /etc/hosts`。必要なら `sudo dscacheutil -flushcache`。ブラウザーを終了・再起動して新しいURLを開く。証明書選択画面では `admin-pc-20261027` を選ぶ。キーチェーンの秘密鍵使用確認が出た場合は利用するブラウザーに許可する。

[Apple公式の読み込み手順](https://support.apple.com/guide/keychain-access/import-and-export-keychain-items-kyca35961/mac)

### Windows：Chrome / Edge

- `certmgr.msc` を開く（現在のユーザー）。「信頼されたルート証明機関」→「証明書」に `ca.crt` をインポートする。
- 「個人」→「証明書」に `identity.p12` をインポートする。ファイル種別をPFX/P12へ変更し、パスワードを入力する。秘密鍵をエクスポート可能にするチェックは不要。
- 管理者権限のエディターで `C:\Windows\System32\drivers\etc\hosts` に上記の1行を追加する。必要なら `ipconfig /flushdns`。
- ブラウザーを終了・再起動し、証明書選択画面で自分の証明書を選ぶ。

[Microsoft公式のPFX登録説明](https://learn.microsoft.com/en-us/powershell/module/pki/import-pfxcertificate)

### Firefox / Linux

Firefoxの設定→プライバシーとセキュリティ→証明書→証明書を表示で、「認証局証明書」に `ca.crt` を登録してWebサイトの識別を信頼し、「あなたの証明書」に `identity.p12` を登録する。hostsには同じ1行を追加する。OSストアとの共有動作はブラウザー・OSの設定で異なるため、ここではブラウザーへの明示登録を使う。

### 接続確認

**https://radxa-console.home.arpa/** をブックマークする。「設」の接続認証が「証」になり、操作キーの入力なしで開閉できることを確認する。証明書の選択が毎回表示されるかはブラウザーの設定による。

証明書なしではTLS接続に失敗する。許可外IPはFWで拒否されるため、タイムアウトになる場合もある。許可外IPに対するCaddyの追加判定は403。`https://10.2.0.2/` ではなく専用名を使う。証明書警告を無視して接続しない。

コマンドでの確認（管理PC）：

```sh
curl --cacert output/mtls-pki/ca.crt \
  --cert output/mtls-pki/admin-pc-20261027/cert.pem \
  --key output/mtls-pki/admin-pc-20261027/key.pem \
  https://radxa-console.home.arpa/api/control
```

`auth.mode` が `mtls`、`auth.can_control` が `true`。応答に内部キーは含まれない。

## 7. 証明書の更新・無効化

期限確認は管理PCで行う。手動発行した証明書をCaddyが自動更新することはない。

```sh
openssl x509 -in output/mtls-pki/admin-pc-20261027/cert.pem -noout -dates
openssl x509 -in output/mtls-pki/radxa-server-20261027/cert.pem -checkend 2592000 -noout
```

期限前に、新しい名前で `issue-server` / `issue-client` を実行し、新しいディレクトリへ `bundle` を作成して、手順4・5と同じ方法で適用する。クライアント更新時は管理PCにも新しい `.p12` を登録する。同じCAの間はCAの再登録不要。

移行期間は `--client 旧名 --client 新名` の両方を指定可能。更新確認後は、新名だけのbundleを作ってRadxaに再適用する。許可リストは指定した証明書の集合で**置換**されるため、旧証明書では接続できなくなる。CAで署名しただけの未登録証明書も許可しない。

紛失・漏えい時も、当該証明書を除いたbundleへ置き換えて専用Caddyを再起動する。最後の1枚を無効化して全員を止める場合は、RadxaへSSHして次を実行する。

```sh
sudo systemctl disable --now taco-console-caddy
```

現在の仕組みはCRL/OCSPを自動取得するものではなく、Caddy標準のleaf許可リストによる明示管理。証明書変更時はreloadではなくサービスを再起動し、既存TLS接続も閉じる。Caddyの再起動は対外接続ゲートの状態を変えない。

CA秘密鍵の紛失・漏えいはCAの作り直しと全証明書の再発行が必要。CA鍵とパスフレーズを別々にバックアップする。発行済み証明書・秘密鍵・配布資材はGitへ追加しない。

内部操作キーを別途ローテーションしたときは、Radxaの `install_mtls.py` を再実行してCaddy側へ反映する。操作キーを管理PC・ブラウザーへ配布する必要はない。以前localhostのコンソールへ保存したキーがあれば、旧URLのブラウザーサイトデータから削除する。

## 8. 復旧と既存構成との関係

- Web更新だけなら `install-console.sh --update-console`。専用Caddy・FW・証明書は維持される。
- `render.py` で生成したRadxaのnftables.confを丸ごと再配置した場合は、mTLSのincludeが外れる。`install_mtls.py` を再実行する。
- コンソールの初回・完全再導入やOS再起動でゲートが「閉」になる性質は維持する。
- SSH転送によるlocalhost表示は閲覧用に残る。ブラウザーの手入力キー機能は廃止しているため、開閉はmTLSのURLから行う。
- 切り分けは `sudo journalctl -u taco-console-caddy -u taco-console-web -n 80 --no-pager`、`sudo nft -a list chain inet taco_filter input`、`uci show firewall.ai_admin_console` を利用する。CaddyのEnvironmentFileや内部キーの内容をログ・問い合わせへ貼らない。

新しいHTTPS入口を撤去する場合は専用Caddyを停止し、OpenWrtの `ai_admin_console`、Radxaの専用ルール・includeを削除する。削除対象のルールは `comment "taco-console-mtls"` のものだけ。既存のSSH・Ping許可や `inet taco_console` ゲートを削除しない。

## 検証

```sh
python3 -B -m unittest discover -s management/tests
node --check management/taco-console/static/app.js
node management/tests/test_atlas.cjs
python3 -B management/tests/mtls_smoke.py --caddy /path/to/caddy
```

最後のテストは一時CA・証明書・ローカルの模擬制御サービスを使い、FWには触れない。有効な証明書・内部キー付与・不正Origin拒否・証明書なし／未登録証明書拒否・旧証明書の無効化・接続元IP制限を実際のCaddyで確認する。

参考：[Caddyのクライアント証明書検証](https://caddyserver.com/docs/caddyfile/directives/tls#client_auth)、[Host/SNIの厳密照合](https://caddyserver.com/docs/caddyfile/options#strict_sni_host)。
