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
        (payload_dir / "command-index.json").write_text(json.dumps({"hello": {"path": "packs/base/bin/hello", "pack": "base", "roles": ["dns"], "destructive": False}}))
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
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"payload": "release.tar", "checksum": h}))

    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.json")
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
    (etc / "secrets.yml").write_text('secrets:\n  - target: "/tmp/atlas-secret.txt"\n    env: "ATLAS_SECRET_TOKEN"\n    mode: "0600"\n')
    monkeypatch.setenv("ATLAS_SECRET_TOKEN", "super-secret")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"]); assert main() == 0
    assert Path('/tmp/atlas-secret.txt').read_text() == 'super-secret'
