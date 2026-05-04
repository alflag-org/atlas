# Getting Started

この手順は **CI でドライ実行しやすい最小構成** です。実環境の `/etc` `/opt` `/var/lib` は使わず、作業ディレクトリ配下に閉じます。

## 1. 事前準備

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 2. 最小リリースを作る

```bash
mkdir -p .tmp/release/packs/base/bin
cat > .tmp/release/packs/base/bin/hello <<'SH'
#!/usr/bin/env bash
echo "hello atlas"
SH
chmod +x .tmp/release/packs/base/bin/hello
```

## 3. ローカル node 設定を作る

```bash
mkdir -p .tmp/etc
cat > .tmp/etc/node.yml <<'YAML'
name: ci-node
role: ci
packs:
  - base
YAML
```

## 4. bundle を build / pull / apply する

```bash
export ATLAS_ETC_DIR="$PWD/.tmp/etc"
export ATLAS_OPT_DIR="$PWD/.tmp/opt"
export ATLAS_VAR_DIR="$PWD/.tmp/var"

atlas build .tmp/release .tmp/release.tar
atlas pull .tmp/release.tar --version 2026.05.04-ci
atlas apply --version 2026.05.04-ci
```

## 5. コマンド実行

```bash
atlas run hello
atlas status
```

## 6. クリーンアップ（任意）

```bash
rm -rf .tmp .venv
```
