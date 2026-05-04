# systemd

`atlas install-systemd` / `atlas uninstall-systemd` を利用します。

## 有効化（dry-run）

```bash
atlas install-systemd --dry-run --unit-dir .tmp/systemd
```

## 有効化（実行）

```bash
sudo atlas install-systemd
```

## 無効化

```bash
sudo atlas uninstall-systemd
```

`systemctl` がない環境ではスキップされます（`--strict` 指定時は失敗）。
