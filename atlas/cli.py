from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from .config import ensure_dirs, resolve_paths
from .models import RuntimeState, utcnow
from .release import (
    build_bundle,
    inspect_bundle,
    pull_bundle,
    rollback_to,
    sign_bundle,
    verify_bundle,
)
from .runtime import apply_release_with_phases, run_command
from .secrets import materialize_secrets

SYSTEMD_UNIT_FILES = ("atlas-pull.service", "atlas-pull.timer")


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _run_or_print(cmd: list[str], dry_run: bool) -> None:
    print("$ " + " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=True)


def _systemd_template_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "packaging" / "systemd"


def cmd_install_systemd(args: argparse.Namespace) -> int:
    if not _systemctl_available():
        message = "systemctl not found; skipping systemd install"
        if args.strict:
            raise SystemExit(message)
        print(message)
        return 0

    template_dir = _systemd_template_dir()
    unit_dir = Path(args.unit_dir)
    unit_dir.mkdir(parents=True, exist_ok=True)

    for unit_name in SYSTEMD_UNIT_FILES:
        src = template_dir / unit_name
        if not src.exists():
            raise SystemExit(f"missing systemd template: {src}")
        dst = unit_dir / unit_name
        print(f"install {src} -> {dst}")
        if not args.dry_run:
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    _run_or_print(["systemctl", "daemon-reload"], args.dry_run)
    _run_or_print(["systemctl", "enable", "atlas-pull.timer"], args.dry_run)
    _run_or_print(["systemctl", "start", "atlas-pull.timer"], args.dry_run)
    return 0


def cmd_uninstall_systemd(args: argparse.Namespace) -> int:
    if not _systemctl_available():
        message = "systemctl not found; skipping systemd uninstall"
        if args.strict:
            raise SystemExit(message)
        print(message)
        return 0

    unit_dir = Path(args.unit_dir)
    _run_or_print(["systemctl", "stop", "atlas-pull.timer"], args.dry_run)
    _run_or_print(["systemctl", "disable", "atlas-pull.timer"], args.dry_run)

    for unit_name in SYSTEMD_UNIT_FILES:
        target = unit_dir / unit_name
        print(f"remove {target}")
        if not args.dry_run and target.exists():
            target.unlink()

    _run_or_print(["systemctl", "daemon-reload"], args.dry_run)
    return 0


def _last_run_summary(logs_dir: Path) -> dict[str, object] | None:
    runs = logs_dir / "runs.jsonl"
    if not runs.exists():
        return None
    lines = [ln for ln in runs.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    last = json.loads(lines[-1])
    return {
        "timestamp": last.get("timestamp"),
        "command": last.get("command"),
        "args": last.get("args", []),
        "caller": last.get("caller"),
        "exit_code": last.get("exit_code"),
        "duration_ms": last.get("duration_ms"),
        "release_version": last.get("release_version"),
        "node_name": last.get("node_name"),
        "node_role": last.get("node_role"),
        "pack": last.get("pack"),
        "destructive": last.get("destructive"),
    }


def cmd_status(_: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    state = RuntimeState.load(p.state_file)
    payload = dict(state.__dict__)
    payload["last_run"] = _last_run_summary(p.logs)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    bundle = build_bundle(
        Path(args.release_dir),
        Path(args.bundle),
        payload_name=args.payload_name,
        sign=args.sign,
        secret_key=Path(args.secret_key) if args.secret_key else None,
    )
    print(f"built bundle: {bundle}")
    return 0


def cmd_inspect_bundle(args: argparse.Namespace) -> int:
    data = inspect_bundle(Path(args.bundle))
    print(json.dumps(data, indent=2))
    return 0


def cmd_sign_bundle(args: argparse.Namespace) -> int:
    sign_bundle(Path(args.bundle), Path(args.secret_key))
    print(f"signed bundle: {args.bundle}")
    return 0


def cmd_verify_bundle(args: argparse.Namespace) -> int:
    verify_bundle(Path(args.bundle))
    print("bundle verified")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    state_path = p.state_file
    state = RuntimeState.load(state_path)
    version = args.version
    staged = p.releases / version
    if staged.exists():
        shutil.rmtree(staged)
    manifest = pull_bundle(Path(args.bundle), staged)
    state.last_pull_at = utcnow()
    state.save(state_path)
    print(f"pulled {version}: {manifest.get('payload')}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    state_path = p.state_file
    state = RuntimeState.load(state_path)
    version = args.version
    release_dir = p.releases / version
    if not release_dir.exists():
        raise SystemExit(f"release not found: {version}")
    generated = apply_release_with_phases(
        release_dir,
        version,
        p.current,
        p.shims,
        p.libexec,
        state_path,
        p.staging,
        dry_run=args.dry_run,
    )
    if args.plan:
        print(
            f"plan: version={version} current={state.current_version} previous={state.previous_version} daemon_reload={bool(shutil.which('systemctl'))}"
        )
        return 0
    if args.dry_run:
        print(f"dry-run ok for {version}")
    else:
        print(f"active release is now {version} (generated {generated} shims)")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from .models import load_yaml_file

    p = resolve_paths()
    ensure_dirs(p)
    cfg = load_yaml_file(p.config / "atlas.yml")
    release = cfg.get("release", {})
    source = str(release.get("source", ""))
    version = str(release.get("version", ""))
    auto_apply = bool(release.get("auto_apply", True))
    if source.startswith("https://"):
        raise SystemExit("https source is configured but not yet supported")
    if not source.startswith("file://"):
        raise SystemExit("release.source must start with file:// or https://")
    bundle = Path(source[len("file://") :])
    cmd_pull(argparse.Namespace(bundle=str(bundle), version=version))
    if not args.no_apply and auto_apply:
        cmd_apply(argparse.Namespace(version=version, dry_run=args.dry_run, plan=False))
    return 0


def cmd_rollback(_: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    state_path = p.state_file
    state = RuntimeState.load(state_path)
    if not state.previous_version:
        raise SystemExit("no previous_version to rollback to")
    rollback_to(p.releases, state.previous_version, p.current)
    state.current_version, state.previous_version = (
        state.previous_version,
        state.current_version,
    )
    state.last_apply_status = "rollback"
    state.last_apply_at = utcnow()
    state.save(state_path)
    print(f"rolled back to {state.current_version}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    redact_values: list[str] = []
    if args.materialize_secrets:
        _, redact_values = materialize_secrets(p.config)
    result = run_command(
        p.current,
        p.locks,
        p.logs,
        p.config,
        args.command,
        args.args,
        timeout=args.timeout,
        allow_destructive=args.allow_destructive,
        redact_values=redact_values,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    return result.code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    build = sub.add_parser("build")
    build.add_argument("release_dir")
    build.add_argument("bundle")
    build.add_argument("--payload-name", default="payload.tar.zst")
    build.add_argument("--sign", action="store_true")
    build.add_argument("--secret-key")
    build.set_defaults(func=cmd_build)

    inspect_cmd = sub.add_parser("inspect-bundle")
    inspect_cmd.add_argument("bundle")
    inspect_cmd.set_defaults(func=cmd_inspect_bundle)

    verify_cmd = sub.add_parser("verify-bundle")
    verify_cmd.add_argument("bundle")
    verify_cmd.set_defaults(func=cmd_verify_bundle)

    sign_cmd = sub.add_parser("sign-bundle")
    sign_cmd.add_argument("bundle")
    sign_cmd.add_argument("--secret-key", required=True)
    sign_cmd.set_defaults(func=cmd_sign_bundle)

    pull = sub.add_parser("pull")
    pull.add_argument("bundle")
    pull.add_argument("--version", required=True)
    pull.set_defaults(func=cmd_pull)

    apply = sub.add_parser("apply")
    apply.add_argument("--version", required=True)
    apply.add_argument("--plan", action="store_true")
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(func=cmd_apply)

    update = sub.add_parser("update")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--no-apply", action="store_true")
    update.set_defaults(func=cmd_update)

    rollback = sub.add_parser("rollback")
    rollback.set_defaults(func=cmd_rollback)

    run = sub.add_parser("run")
    run.add_argument("command")
    run.add_argument("args", nargs=argparse.REMAINDER)
    run.add_argument("--timeout", type=int, default=300)
    run.add_argument("--allow-destructive", action="store_true")
    run.add_argument("--materialize-secrets", action="store_true")
    run.set_defaults(func=cmd_run)

    install_systemd = sub.add_parser("install-systemd")
    install_systemd.add_argument("--unit-dir", default="/etc/systemd/system")
    install_systemd.add_argument("--dry-run", action="store_true")
    install_systemd.add_argument("--strict", action="store_true")
    install_systemd.set_defaults(func=cmd_install_systemd)

    uninstall_systemd = sub.add_parser("uninstall-systemd")
    uninstall_systemd.add_argument("--unit-dir", default="/etc/systemd/system")
    uninstall_systemd.add_argument("--dry-run", action="store_true")
    uninstall_systemd.add_argument("--strict", action="store_true")
    uninstall_systemd.set_defaults(func=cmd_uninstall_systemd)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
