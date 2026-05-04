from pathlib import Path
import json
import tarfile
import pytest

from atlas.cli import main
import atlas.release as release_mod
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

    sig = tmp_path / "manifest.yml.minisig"
    sig.write_text("dummy-signature")
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(sig, arcname="manifest.yml.minisig")
        tf.add(payload_tgz, arcname="release.tar")
    return bundle


def setup_node(monkeypatch, tmp_path, role="dns", packs="  - base\n"):
    opt = tmp_path / "opt"
    var = tmp_path / "var"
    etc = tmp_path / "etc"
    etc.mkdir(parents=True)
    (etc / "node.yml").write_text(f"name: n1\nrole: {role}\npacks:\n{packs}")
    monkeypatch.setenv("ATLAS_OPT_DIR", str(opt))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    return opt, var, etc




@pytest.fixture(autouse=True)
def _mock_signature_verification(monkeypatch):
    monkeypatch.setattr(release_mod, "verify_manifest_signature", lambda staged_dir: None)

def test_pull_apply_run_and_status(monkeypatch, tmp_path, capsys):
    opt, var, _ = setup_node(monkeypatch, tmp_path)
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
    shim = opt / "shims/hello"
    assert shim.is_symlink()
    assert shim.resolve() == (opt / "libexec/atlas-shim")


def test_rollback(monkeypatch, tmp_path):
    _, _, _ = setup_node(monkeypatch, tmp_path)
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
    _, _, etc = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    target = etc / "secrets" / "atlas-secret.txt"
    (etc / "secrets.yml").write_text(f'secrets:\n  - target: "{target}"\n    env: "ATLAS_SECRET_TOKEN"\n    mode: "0600"\n')
    monkeypatch.setenv("ATLAS_SECRET_TOKEN", "super-secret")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"]); assert main() == 0
    assert target.read_text() == 'super-secret'


def test_secret_path_traversal_blocked(monkeypatch, tmp_path):
    _, _, etc = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    (etc / "secrets.yml").write_text('secrets:\n  - target: "/tmp/outside-secret.txt"\n    env: "ATLAS_SECRET_TOKEN"\n')
    monkeypatch.setenv("ATLAS_SECRET_TOKEN", "super-secret")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"])
    with pytest.raises(ValueError, match=r"outside allowed root"):
        main()


def test_secret_redaction(monkeypatch, tmp_path):
    _, _, etc = setup_node(monkeypatch, tmp_path)
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
    sig = tmp_path / "manifest.yml.minisig"
    sig.write_text("dummy-signature")
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(sig, arcname="manifest.yml.minisig")
        tf.add(payload_tgz, arcname="release.tar")

    target = etc / "secrets" / "atlas-secret.txt"
    (etc / "secrets.yml").write_text(f'secrets:\n  - target: "{target}"\n    env: "ATLAS_SECRET_TOKEN"\n')
    monkeypatch.setenv("ATLAS_SECRET_TOKEN", "super-secret")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "--materialize-secrets", "hello"]); assert main() == 0
    out = (tmp_path / "var/logs/hello.log").read_text()
    assert "super-secret" not in out
    assert "***REDACTED***" in out
    run_logs = (tmp_path / "var/logs/runs.jsonl").read_text()
    assert "super-secret" not in run_logs


def test_run_jsonl_has_required_fields_even_on_failure(monkeypatch, tmp_path):
    _, _, _etc = setup_node(monkeypatch, tmp_path)
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
    sig = tmp_path / "manifest.yml.minisig"
    sig.write_text("dummy-signature")
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(sig, arcname="manifest.yml.minisig")
        tf.add(payload_tgz, arcname="release.tar")

    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "boom"])
    assert main() == 7

    line = (tmp_path / "var/logs/runs.jsonl").read_text().strip().splitlines()[-1]
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
    sig = tmp_path / "manifest.yml.minisig"
    sig.write_text("dummy-signature")
    bundle = tmp_path / "bundle.tar"
    with tarfile.open(bundle, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(sig, arcname="manifest.yml.minisig")
        tf.add(payload_tgz, arcname="release.tar")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"])
    with pytest.raises(ValueError, match=r"timeout header out of range"):
        main()


def test_apply_failure_rolls_back_active_state_and_shims(monkeypatch, tmp_path):
    opt, var, _ = setup_node(monkeypatch, tmp_path)
    b1 = make_bundle(tmp_path / "a")
    b2 = make_bundle(tmp_path / "b")
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(b1), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0

    before_state = json.loads((var / "state.yml").read_text())
    before_active = (opt / "current").resolve()
    before_shim_target = (opt / "shims/hello").resolve()

    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(b2), "--version", "v2"]); assert main() == 0
    import atlas.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "generate_shims", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shim failure")))

    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v2"])
    with pytest.raises(RuntimeError):
        main()

    after_state = json.loads((var / "state.yml").read_text())
    assert after_state["current_version"] == before_state["current_version"]
    assert (opt / "current").resolve() == before_active
    assert (opt / "shims/hello").resolve() == before_shim_target


def test_generate_shims_only_enabled_and_cleans_old_files(tmp_path):
    from atlas.shims import generate_shims

    active = tmp_path / "active"
    shims = tmp_path / "shims"
    libexec = tmp_path / "libexec"
    active.mkdir()
    shims.mkdir()
    (shims / "old-shim").write_text("legacy")
    (active / "command-index.yml").write_text(
        "commands:\n"
        "  hello:\n"
        "    path: packs/base/bin/hello\n"
        "    enabled: true\n"
        "  disabled:\n"
        "    path: packs/base/bin/disabled\n"
        "    enabled: false\n"
    )

    generated = generate_shims(active, shims, libexec)

    assert generated == 1
    assert not (shims / "old-shim").exists()
    assert (shims / "hello").is_symlink()
    assert not (shims / "disabled").exists()


def test_generate_shims_fails_on_reserved_or_system_name_collisions(tmp_path):
    from atlas.shims import generate_shims

    active = tmp_path / "active"
    shims = tmp_path / "shims"
    libexec = tmp_path / "libexec"
    active.mkdir()

    (active / "command-index.yml").write_text("commands:\n  atlas:\n    path: packs/base/bin/hello\n")
    with pytest.raises(ValueError, match=r"reserved command name"):
        generate_shims(active, shims, libexec)

    (active / "command-index.yml").write_text("commands:\n  ls:\n    path: packs/base/bin/hello\n")
    with pytest.raises(ValueError, match=r"conflicts with system binary"):
        generate_shims(active, shims, libexec)


def test_node_schema_missing_required_type_and_unknown_key(monkeypatch, tmp_path):
    _, _, etc = setup_node(monkeypatch, tmp_path)

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
    _, _, etc = setup_node(monkeypatch, tmp_path)
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


def test_node_json_fallback_when_yaml_missing(monkeypatch, tmp_path):
    opt = tmp_path / "opt"
    var = tmp_path / "var"
    etc = tmp_path / "etc"
    etc.mkdir(parents=True)
    (etc / "node.json").write_text(json.dumps({"name": "n1", "role": "dns", "packs": ["base"]}))
    monkeypatch.setenv("ATLAS_OPT_DIR", str(opt))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    with pytest.raises(ValueError, match=r"pack not enabled for this host: base"):
        main()


def test_invalid_yaml_has_file_and_location(monkeypatch, tmp_path):
    _, _, etc = setup_node(monkeypatch, tmp_path)
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle), "--version", "v1"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v1"]); assert main() == 0
    (etc / "node.yml").write_text("name: n1\nrole: dns\npacks:\n  - base\n  - [bad\n")
    monkeypatch.setattr("sys.argv", ["atlas", "run", "hello"])
    with pytest.raises(ValueError, match=r"invalid YAML in .*node\\.yml line \\d+, column \\d+"):
        main()

def test_build_inspect_verify_bundle(monkeypatch, tmp_path, capsys):
    release_dir = tmp_path / "release"
    cmd = release_dir / "packs/base/bin"
    cmd.mkdir(parents=True)
    hello = cmd / "hello"
    hello.write_text("#!/usr/bin/env bash\n# atlas: allowed_roles=dns\necho hello\n")
    hello.chmod(0o755)

    bundle = tmp_path / "bundle.tar"
    monkeypatch.setattr("sys.argv", ["atlas", "build", str(release_dir), str(bundle)])
    assert main() == 0

    monkeypatch.setattr("sys.argv", ["atlas", "inspect-bundle", str(bundle)])
    assert main() == 0
    out = capsys.readouterr().out
    assert "manifest" in out and "base" in out and "payload" in out

    monkeypatch.setattr("sys.argv", ["atlas", "verify-bundle", str(bundle)])
    assert main() == 0


def test_verify_bundle_nonzero_on_checksum_mismatch(monkeypatch, tmp_path):
    bundle = make_bundle(tmp_path)
    staged = tmp_path / "staged"
    staged.mkdir()
    with tarfile.open(bundle, "r") as tf:
        tf.extractall(staged)
    (staged / "manifest.yml").write_text("payload: release.tar\nchecksum: deadbeef\n")
    bad_bundle = tmp_path / "bad-bundle.tar"
    with tarfile.open(bad_bundle, "w") as tf:
        (staged / "manifest.yml.minisig").write_text("dummy-signature")
        tf.add(staged / "manifest.yml", arcname="manifest.yml")
        tf.add(staged / "manifest.yml.minisig", arcname="manifest.yml.minisig")
        tf.add(staged / "release.tar", arcname="release.tar")

    monkeypatch.setattr("sys.argv", ["atlas", "verify-bundle", str(bad_bundle)])
    with pytest.raises(SystemExit):
        main()


def test_apply_copies_only_active_pack_files(monkeypatch, tmp_path):
    import atlas.runtime as runtime_mod
    runtime_mod._ALLOWED_FILE_PREFIXES = (tmp_path,)
    opt, var, etc = setup_node(monkeypatch, tmp_path, packs="  - base\n")
    bundle = make_bundle(tmp_path)

    payload_dir = tmp_path / "payload2"
    payload_dir.mkdir(parents=True)
    cmd = payload_dir / "packs/base/bin"
    cmd.mkdir(parents=True)
    hello = cmd / "hello"
    hello.write_text("#!/usr/bin/env bash\necho hello\n")
    hello.chmod(0o755)
    (payload_dir / "packs/base/files" / tmp_path.relative_to('/') / "managed.txt").parent.mkdir(parents=True)
    (payload_dir / "packs/base/files" / tmp_path.relative_to('/') / "managed.txt").write_text("ok")
    (payload_dir / "packs/other/files" / tmp_path.relative_to('/') / "other.txt").parent.mkdir(parents=True)
    (payload_dir / "packs/other/files" / tmp_path.relative_to('/') / "other.txt").write_text("ng")

    payload_tgz = tmp_path / "release2.tar"
    with tarfile.open(payload_tgz, "w") as tf:
        tf.add(payload_dir, arcname=".")
    import hashlib
    h = hashlib.sha256(payload_tgz.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest2.yml"
    manifest.write_text(f"payload: release2.tar\nchecksum: {h}\n")
    bundle2 = tmp_path / "bundle2.tar"
    sig = tmp_path / "manifest2.yml.minisig"
    sig.write_text("dummy-signature")
    with tarfile.open(bundle2, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(sig, arcname="manifest.yml.minisig")
        tf.add(payload_tgz, arcname="release2.tar")

    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle2), "--version", "v2"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v2"]); assert main() == 0

    assert (tmp_path / "managed.txt").read_text() == "ok"
    assert not (tmp_path / "other.txt").exists()


def test_apply_rejects_files_symlink_traversal(monkeypatch, tmp_path):
    import atlas.runtime as runtime_mod
    runtime_mod._ALLOWED_FILE_PREFIXES = (tmp_path,)
    setup_node(monkeypatch, tmp_path, packs="  - base\n")

    payload_dir = tmp_path / "payload3"
    payload_dir.mkdir(parents=True)
    cmd = payload_dir / "packs/base/bin"
    cmd.mkdir(parents=True)
    hello = cmd / "hello"
    hello.write_text("#!/usr/bin/env bash\necho hello\n")
    hello.chmod(0o755)
    files_dir = payload_dir / "packs/base/files"
    files_dir.mkdir(parents=True)
    (files_dir / "bad").symlink_to("/etc/passwd")

    payload_tgz = tmp_path / "release3.tar"
    with tarfile.open(payload_tgz, "w") as tf:
        tf.add(payload_dir, arcname=".")
    import hashlib
    h = hashlib.sha256(payload_tgz.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest3.yml"
    manifest.write_text(f"payload: release3.tar\nchecksum: {h}\n")
    bundle3 = tmp_path / "bundle3.tar"
    sig = tmp_path / "manifest3.yml.minisig"
    sig.write_text("dummy-signature")
    with tarfile.open(bundle3, "w") as tf:
        tf.add(manifest, arcname="manifest.yml")
        tf.add(sig, arcname="manifest.yml.minisig")
        tf.add(payload_tgz, arcname="release3.tar")

    monkeypatch.setattr("sys.argv", ["atlas", "pull", str(bundle3), "--version", "v3"]); assert main() == 0
    monkeypatch.setattr("sys.argv", ["atlas", "apply", "--version", "v3"])
    with pytest.raises(ValueError, match=r"symlink traversal"):
        main()
