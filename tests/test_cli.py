from pathlib import Path
import json
import tarfile
import pytest

from atlas.cli import main
from atlas.release import pull_bundle


def make_bundle(tmp_path: Path, include_index: bool = True):
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir(parents=True)
    if include_index:
        (payload_dir / "command-index.yml").write_text("hello:\n  path: packs/base/bin/hello\n  pack: base\n  roles:\n    - dns\n  destructive: false\n")
    cmd = payload_dir / "packs/base/bin"
    cmd.mkdir(parents=True)
    hello = cmd / "hello"
    hello.write_text("#!/usr/bin/env bash\necho hello\n")
    hello.chmod(0o755)

    payload_tgz = tmp_path / "release.tar"
    with tarfile.open(payload_tgz, "w") as tf:
        tf.add(payload_dir, arcname=".")

    import hashlib
    h = hashlib.sha256(payload_tgz.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(f"payload: release.tar\nchecksum: {h}\n")

    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(payload_tgz, arcname="release.tar")
    return bundle


def setup_node(monkeypatch, tmp_path, role="dns", packs="  - base\n"):
    root = tmp_path / "opt"
    etc = tmp_path / "etc"
    etc.mkdir(parents=True)
    (etc / "node.yml").write_text(f"name: n1\nrole: {role}\npacks:\n{packs}")
    monkeypatch.setenv("ATLAS_ROOT", str(root))
    monkeypatch.setenv("ATLAS_ETC", str(etc))
    return root, etc


def test_pull_apply_run_and_status(monkeypatch, tmp_path, capsys):
    root, _ = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"])
    assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"])
    assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "status"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "hello" in out and "current_version" in out
    assert (root / "shims/hello").exists()


def test_rollback(monkeypatch, tmp_path):
    setup_node(monkeypatch, tmp_path)
    b1 = make_bundle(tmp_path / "a")
    b2 = make_bundle(tmp_path / "b")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(b1), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(b2), "--version", "v2"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v2"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "rollback"]); assert main() == 0


def test_role_or_pack_denied(monkeypatch, tmp_path):
    setup_node(monkeypatch, tmp_path, role="web", packs="  - monitoring\n")
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    with pytest.raises(ValueError):
        main()


def test_tar_path_traversal_blocked(tmp_path):
    staged = tmp_path / "staged"
    bundle = tmp_path / "evil.tar"
    with tarfile.open(bundle, "w") as tf:
        evil = tmp_path / "evil.txt"; evil.write_text("x")
        tf.add(evil, arcname="../evil.txt")
    with pytest.raises(ValueError):
        pull_bundle(bundle, staged)


def test_materialize_secrets(monkeypatch, tmp_path):
    _, etc = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    target = etc / "secrets" / "atlas-secret.txt"
    (etc / "secrets.yml").write_text(f'secrets:\n  - target: "{target}"\n    env: "ATLAS_SECRET_TOKEN"\n    mode: "0600"\n')
    monkeypatch.setenv("ATLAS_SECRET_TOKEN", "super-secret")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"]); assert main() == 0
    assert target.read_text() == 'super-secret'


def test_secret_path_traversal_blocked(monkeypatch, tmp_path):
    _, etc = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    (etc / "secrets.yml").write_text('secrets:\n  - target: "/tmp/outside-secret.txt"\n    env: "ATLAS_SECRET_TOKEN"\n')
    monkeypatch.setenv("ATLAS_SECRET_TOKEN", "super-secret")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"])
    with pytest.raises(ValueError, match=r"outside allowed root"):
        main()


def test_secret_redaction(monkeypatch, tmp_path):
    _, etc = setup_node(monkeypatch, tmp_path)
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir(parents=True)
    cmd = payload_dir / "packs/base/bin"
    cmd.mkdir(parents=True)
    hello = cmd / "hello"
    hello.write_text("#!/usr/bin/env bash\necho super-secret\n")
    hello.chmod(0o755)

    payload_tgz = tmp_path / "release.tar"
    with tarfile.open(payload_tgz, "w") as tf:
        tf.add(payload_dir, arcname=".")
    import hashlib
    h = hashlib.sha256(payload_tgz.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(f"payload: release.tar\nchecksum: {h}\n")
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(payload_tgz, arcname="release.tar")

    target = etc / "secrets" / "atlas-secret.txt"
    (etc / "secrets.yml").write_text(f'secrets:\n  - target: "{target}"\n    env: "ATLAS_SECRET_TOKEN"\n')
    monkeypatch.setenv("ATLAS_SECRET_TOKEN", "super-secret")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"]); assert main() == 0
    out = (tmp_path / "opt/logs/hello.log").read_text()
    assert "super-secret" not in out
    assert "***REDACTED***" in out
    run_logs = (tmp_path / "opt/logs/runs.jsonl").read_text()
    assert "super-secret" not in run_logs


def test_run_jsonl_has_required_fields_even_on_failure(monkeypatch, tmp_path):
    _, _etc = setup_node(monkeypatch, tmp_path)
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir(parents=True)
    (payload_dir / "command-index.yml").write_text("boom:\n  path: packs/base/bin/boom\n  pack: base\n  roles:\n    - dns\n  destructive: false\n")
    cmd = payload_dir / "packs/base/bin"
    cmd.mkdir(parents=True)
    boom = cmd / "boom"
    boom.write_text("#!/usr/bin/env bash\necho err 1>&2\nexit 7\n")
    boom.chmod(0o755)

    payload_tgz = tmp_path / "release.tar"
    with tarfile.open(payload_tgz, "w") as tf:
        tf.add(payload_dir, arcname=".")
    import hashlib
    h = hashlib.sha256(payload_tgz.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(f"payload: release.tar\nchecksum: {h}\n")
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(payload_tgz, arcname="release.tar")

    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "boom"])
    assert main() == 7

    line = (tmp_path / "opt/logs/runs.jsonl").read_text().strip().splitlines()[-1]
    record = json.loads(line)
    for key in ["timestamp", "command", "args", "caller", "exit_code", "duration_ms", "release_version", "node_role"]:
        assert key in record
        assert record[key] is not None


def test_metadata_invalid_header(monkeypatch, tmp_path):
    setup_node(monkeypatch, tmp_path)
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir(parents=True)
    cmd = payload_dir / "packs/base/bin"
    cmd.mkdir(parents=True)
    hello = cmd / "hello"
    hello.write_text("#!/usr/bin/env bash\n# atlas: timeout=999999\necho hello\n")
    hello.chmod(0o755)
    payload_tgz = tmp_path / "release.tar"
    with tarfile.open(payload_tgz, "w") as tf:
        tf.add(payload_dir, arcname=".")
    import hashlib
    h = hashlib.sha256(payload_tgz.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(f"payload: release.tar\nchecksum: {h}\n")
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(payload_tgz, arcname="release.tar")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"])
    with pytest.raises(ValueError, match=r"timeout header out of range"):
        main()


def test_apply_failure_rolls_back_active_state_and_shims(monkeypatch, tmp_path):
    root, _ = setup_node(monkeypatch, tmp_path)
    b1 = make_bundle(tmp_path / "a")
    b2 = make_bundle(tmp_path / "b")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(b1), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0

    before_state = json.loads((root / "state/runtime.yml").read_text())
    before_active = (root / "active").resolve()
    before_shim = (root / "shims/hello").read_text()

    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(b2), "--version", "v2"]); assert main() == 0
    import atlas.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "generate_shims", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shim failure")))

    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v2"])
    with pytest.raises(RuntimeError):
        main()

    after_state = json.loads((root / "state/runtime.yml").read_text())
    assert after_state["current_version"] == before_state["current_version"]
    assert (root / "active").resolve() == before_active
    assert (root / "shims/hello").read_text() == before_shim


def test_node_schema_missing_required_type_and_unknown_key(monkeypatch, tmp_path):
    _, etc = setup_node(monkeypatch, tmp_path)

    # type mismatch: packs should be a list
    (etc / "node.yml").write_text("name: n1\nrole: dns\npacks: base\n")
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    with pytest.raises(ValueError, match=r"node\.yml: invalid key 'packs': expected type list"):
        main()

    # unknown key
    (etc / "node.yml").write_text("name: n1\nrole: dns\npacks:\n  - base\nextra: x\n")
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    with pytest.raises(ValueError, match=r"node\.yml: invalid key 'extra': unknown key"):
        main()


def test_secrets_schema_missing_type_and_unknown_key(monkeypatch, tmp_path):
    _, etc = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0

    # unknown top-level key
    (etc / "secrets.yml").write_text("oops: 1\n")
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"])
    with pytest.raises(ValueError, match=r"secrets\.yml: invalid key 'oops': unknown key"):
        main()

    # type mismatch
    (etc / "secrets.yml").write_text("secrets: nope\n")
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"])
    with pytest.raises(ValueError, match=r"secrets\.yml: invalid key 'secrets': expected type list"):
        main()


def test_node_json_fallback_removed_when_yaml_missing(monkeypatch, tmp_path):
    root = tmp_path / "opt"
    etc = tmp_path / "etc"
    etc.mkdir(parents=True)
    (etc / "node.json").write_text(json.dumps({"name": "n1", "role": "dns", "packs": ["base"]}))
    monkeypatch.setenv("ATLAS_ROOT", str(root))
    monkeypatch.setenv("ATLAS_ETC", str(etc))
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    with pytest.raises(ValueError, match=r"pack not enabled for this host: base"):
        main()


def test_invalid_yaml_has_file_and_location(monkeypatch, tmp_path):
    _, etc = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    (etc / "node.yml").write_text("name: n1\nrole: dns\npacks:\n  - base\n  - [bad\n")
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    with pytest.raises(ValueError, match=r"invalid YAML in .*node\\.yml line \\d+, column \\d+"):
        main()


def test_migrate_layout_dry_run_and_execute(monkeypatch, tmp_path, capsys):
    new_root = tmp_path / "varlib"
    etc = tmp_path / "etc"
    legacy_root = Path("/opt/atlas")
    (legacy_root / "state").mkdir(parents=True, exist_ok=True)
    (legacy_root / "logs").mkdir(parents=True, exist_ok=True)
    (legacy_root / "locks").mkdir(parents=True, exist_ok=True)
    (legacy_root / "state/runtime.yml").write_text("{}")

    monkeypatch.setenv("ATLAS_ROOT", str(new_root))
    monkeypatch.setenv("ATLAS_ETC", str(etc))

    monkeypatch.setattr("sys.argv", ["atlas", "migrate-layout", "--dry-run"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "layout migration plan" in out
    assert "dry-run only" in out
    assert (legacy_root / "state/runtime.yml").exists()

    monkeypatch.setattr("sys.argv", ["atlas", "migrate-layout", "--execute"])
    assert main() == 0
    assert (new_root / "state/runtime.yml").exists()
