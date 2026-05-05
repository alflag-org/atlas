# Release Checklist

## 自動チェック
- pytest -q
- ruff check .
- ruff format --check .
- mypy atlas
- pyright
- python -m build
- atlas build --sign
- atlas verify-bundle
- atlas update --dry-run
- atlas apply --dry-run
- atlas install-systemd --dry-run

## 手動チェック（tag push 前）
- README 手順確認
- GitHub Release asset（wheel/sdist/signed bundle/checksum）確認
- 別環境で `atlas verify-bundle` 実施
