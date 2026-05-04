# Security Policy（実装済み範囲）

- bundle 読み込み時に tar パストラバーサル・不正 uid/gid/mode を拒否。
- `manifest.yml.minisig` と trusted pubkey で manifest 署名を検証。
- payload checksum（SHA-256）を検証。
- command 実行時に pack/role/destructive/lock を強制。
- `packs/*/files` 展開先は許可プレフィックス（`/etc`, `/opt`, `/usr/local`, `/var/lib/atlas`）に制限。
- secrets 実体化先を `$ATLAS_ETC_DIR/secrets` 配下に制限し、値はログでマスク。
