# Release Operations

## bundle 作成

```bash
atlas build <release_dir> <bundle_path>
```

## bundle 検査

```bash
atlas inspect-bundle <bundle_path>
atlas verify-bundle <bundle_path>
```

## 配布・反映

```bash
atlas pull <bundle_path> --version <version>
atlas apply --version <version>
```

## ロールバック

```bash
atlas rollback
```
