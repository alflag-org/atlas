| 出典 | 目的 | 具体対象 | 役割 | 前後関係 | 候補語 | 初出定義 |
| --- | --- | --- | --- | --- | --- | --- |
| `SPEC.md` 7 | 操作入力を検証可能にする | managed host の識別子、Registry profile、provider 入力、configuration project、readiness 条件を記述する YAML | 値 | plan より前 | `HostSpec` | |
| `SPEC.md` 8, 16 | 確認対象と再実行条件を固定する | 正規化済み source path と digest、Git 状態、adapter、実行順、idempotency key を含む JSON artifact | 記録 | HostSpec の検証後、apply より前 | `HostOperationPlan` | |
| `SPEC.md` 12, 15 | provider mutation の権限を証明する | Global Registry の Operation ID、resource lock scope、fencing token、各 revision の組 | 値 | reserve 後、allocate より前 | `RegistryAuthority` | |
| `SPEC.md` 11, 12 | phase の結果を Registry に残す | phase、状態、開始・終了日時、試行回数、message、details を持つ一件の JSON record | 記録 | 各 phase の実行後 | `HostOperationEvidence` | |
| `SPEC.md` 13 | provider 固有処理を child executable に閉じ込める | validate、allocate、observe、verify、rollback を提供し、Proxmox 固有型を公開しない interface | 手段 | RegistryAuthority 取得後、configuration より前 | `HostProvider` | |
| `SPEC.md` 14 | configuration 固有処理を child executable に閉じ込める | validate、bootstrap、converge、verify を提供し、Ansible 内部 module を公開しない interface | 手段 | Binding と readiness の後 | `HostConfigurator` | |
