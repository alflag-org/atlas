"""Python venvs and interpreter-bound Perl local::lib environments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .config import AtlasConfig, ProgramConfig
from .paths import AtlasPaths
from .runtime import check_executable, ensure_runtime, resolve_runtime, selection

PERL_VARIABLES = (
    "PERL5LIB",
    "PERLLIB",
    "PERL5OPT",
    "PERL_LOCAL_LIB_ROOT",
    "PERL_MM_OPT",
    "PERL_MB_OPT",
)
_PERL_IDENTITY = r"""
use Config;
use Digest::SHA qw(sha256_hex);
use JSON::PP;
print JSON::PP->new->canonical->encode({
    version => $Config{version}, archname => $Config{archname},
    config_sha256 => sha256_hex(Config::config_sh()),
});
"""
_LOCAL_LIB = r"""
use JSON::PP;
require local::lib;
use Encode qw(decode_utf8);
my $lib = local::lib->new->activate(decode_utf8($ARGV[0]), { no_create => 1 });
print JSON::PP->new->encode({ $lib->build_environment_vars });
"""


def clean_perl_environment() -> dict[str, str]:
    """Discard caller module paths, startup options, and installer destinations."""
    return {
        key: value for key, value in os.environ.items() if key not in PERL_VARIABLES
    }


def _perl_json(interpreter: Path, source: str, *args: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            [str(interpreter), "-e", source, *args],
            check=True,
            capture_output=True,
            text=True,
            env=clean_perl_environment(),
        )
    except subprocess.CalledProcessError as exc:
        # Perl startup diagnostics may include application paths or environment data.
        raise RuntimeError(
            "Perl environment probe failed; install local::lib in the selected interpreter"
        ) from exc
    return json.loads(result.stdout)


def _identity(interpreter: Path, language: str) -> dict[str, object]:
    source = check_executable(interpreter, f"{language} executable").resolve()
    identity: dict[str, object] = {
        "executable": str(source),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    if language == "perl":
        identity.update(_perl_json(source, _PERL_IDENTITY))
    return identity


def environment_path(paths: AtlasPaths, program: ProgramConfig) -> Path:
    """Resolve the configured dependency environment, rejecting native programs."""
    if program.runtime.type == "native" or program.runtime.environment is None:
        raise ValueError(
            f"program does not use a dependency environment: {program.name}"
        )
    return paths.environment(program.runtime.type, program.runtime.environment)


def _python_executable(target: Path) -> Path:
    return check_executable(target / "bin/python", "environment Python executable")


def _validate(target: Path, interpreter: Path, language: str) -> None:
    if target.is_symlink() or not target.is_dir():
        raise ValueError(f"environment path must be a directory: {target}")
    manifest = target / ".atlas-interpreter.json"
    if json.loads(manifest.read_text(encoding="utf-8")) != _identity(
        interpreter, language
    ):
        raise ValueError(
            f"environment interpreter changed; recreate the environment and reinstall dependencies: {target}"
        )


def create_environment(
    paths: AtlasPaths, config: AtlasConfig, program: ProgramConfig
) -> Path:
    """Create an empty dependency environment without installing program dependencies."""
    target = environment_path(paths, program)
    language = program.runtime.type
    interpreter = ensure_runtime(paths, language, selection(config, program))
    if target.exists() or target.is_symlink():
        _validate(target, interpreter, language)
        if language == "python":
            _python_executable(target)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    try:
        identity = _identity(interpreter, language)
        if language == "python":
            # venv writes absolute shebangs, so create it at its final location.
            subprocess.run([str(interpreter), "-m", "venv", str(target)], check=True)
            _python_executable(target)
        else:
            (target / "bin").mkdir()
            (target / "lib/perl5").mkdir(parents=True)
            _perl_json(interpreter, _LOCAL_LIB, str(target))
        (target / ".atlas-interpreter.json").write_text(
            json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(target)
        raise
    return target


def environment_executable(
    paths: AtlasPaths, config: AtlasConfig, program: ProgramConfig
) -> Path:
    """Check the selected interpreter before exposing any program dependencies."""
    target = environment_path(paths, program)
    language = program.runtime.type
    interpreter = resolve_runtime(language, selection(config, program))
    _validate(target, interpreter, language)
    if language == "python":
        executable = _python_executable(target)
        if executable.resolve() != interpreter:
            raise ValueError(
                f"environment Python does not match selected interpreter: {target}"
            )
        return executable
    return interpreter


def perl_environment(interpreter: Path, target: Path) -> dict[str, str]:
    """Ask local::lib for installer settings and actual module search paths."""
    variables = _perl_json(interpreter, _LOCAL_LIB, str(target))
    variables["PATH"] = os.pathsep.join(
        [str(target / "bin"), str(interpreter.parent), os.environ.get("PATH", "")]
    )
    return variables
