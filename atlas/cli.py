from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from .config import ensure_dirs, execute_layout_migration, plan_layout_migration, resolve_paths
from .models import RuntimeState, utcnow
from .release import pull_bundle, rollback_to
from .runtime import apply_release_with_phases, run_command
from .secrets import materialize_secrets


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
        "node_role": last.get("node_role"),
    }


def cmd_status(_: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    state = RuntimeState.load(p.state / "runtime.yml")
    payload = dict(state.__dict__)
    payload["last_run"] = _last_run_summary(p.logs)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    state_path = p.state / "runtime.yml"
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
    state_path = p.state / "runtime.yml"
    state = RuntimeState.load(state_path)
    version = args.version
    release_dir = p.releases / version
    if not release_dir.exists():
        raise SystemExit(f"release not found: {version}")
    generated = apply_release_with_phases(release_dir, version, p.active, p.shims, state_path, p.staged)
    print(f"active release is now {version} (generated {generated} shims)")
    return 0


def cmd_rollback(_: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    state_path = p.state / "runtime.yml"
    state = RuntimeState.load(state_path)
    if not state.previous_version:
        raise SystemExit("no previous_version to rollback to")
    rollback_to(p.releases, state.previous_version, p.active)
    state.current_version, state.previous_version = state.previous_version, state.current_version
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
        _, redact_values = materialize_secrets(p.etc)
    result = run_command(p.active, p.locks, p.logs, p.etc, args.command, args.args, timeout=args.timeout, allow_destructive=args.allow_destructive, redact_values=redact_values)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    return result.code


def cmd_migrate_layout(args: argparse.Namespace) -> int:
    p = resolve_paths()
    ensure_dirs(p)
    planned = plan_layout_migration(p)
    if not planned:
        print("no migration targets found")
        return 0

    print("layout migration plan:")
    for src, dst, action in planned:
        print(f"- {src} -> {dst} [{action}]")

    if args.execute:
        print("executing migration...")
        for src, dst, result in execute_layout_migration(p):
            print(f"  {src} -> {dst}: {result}")
    else:
        print("dry-run only. pass --execute to apply changes.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    pull = sub.add_parser("pull")
    pull.add_argument("bundle")
    pull.add_argument("--version", required=True)
    pull.set_defaults(func=cmd_pull)

    apply = sub.add_parser("apply")
    apply.add_argument("--version", required=True)
    apply.set_defaults(func=cmd_apply)

    rollback = sub.add_parser("rollback")
    rollback.set_defaults(func=cmd_rollback)

    run = sub.add_parser("run")
    run.add_argument("command")
    run.add_argument("args", nargs=argparse.REMAINDER)
    run.add_argument("--timeout", type=int, default=300)
    run.add_argument("--allow-destructive", action="store_true")
    run.add_argument("--materialize-secrets", action="store_true")
    run.set_defaults(func=cmd_run)

    migrate_layout = sub.add_parser("migrate-layout")
    mode = migrate_layout.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show migration plan only (default)")
    mode.add_argument("--execute", action="store_true", help="perform migration")
    migrate_layout.set_defaults(func=cmd_migrate_layout)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
