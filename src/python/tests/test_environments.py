"""Real interpreter paths, dependency isolation, and interpreter binding."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from atlas import environments
from atlas.catalog import command_index
from atlas.cli import main
from atlas.config import (
    AtlasConfig,
    ProgramConfig,
    ProgramRuntime,
    RuntimeConfig,
    load_config,
)
from atlas.execution import execute
from atlas.paths import ensure_dirs, get_paths
from atlas_core import get_context

from .support import configure_environment


@pytest.fixture
def perl():
    return Path(os.environ.get("ATLAS_TEST_PERL", shutil.which("perl"))).resolve()


def configuration(tmp_path, language, executable):
    paths = get_paths({"ATLAS_HOME": str(tmp_path / "atlas")})
    program = ProgramConfig(
        "tool", tmp_path / "program", ProgramRuntime(language, environment="tool")
    )
    config = AtlasConfig(
        paths.config_file,
        {language: RuntimeConfig(executable=executable)},
        {"tool": program},
    )
    return paths, config, program


def test_python_uses_venv_and_separates_dependencies(tmp_path):
    paths, config, program = configuration(tmp_path, "python", Path(sys.executable))
    target = environments.create_environment(paths, config, program)
    assert environments.create_environment(paths, config, program) == target
    executable = environments.environment_executable(paths, config, program)
    assert executable == target / "bin/python"
    result = subprocess.run(
        [
            str(executable),
            "-c",
            'import sys,sysconfig; print(sys.prefix); print(sysconfig.get_path("purelib"))',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    prefix, library = result.stdout.splitlines()
    assert prefix == str(target)
    Path(library, "only_here.py").write_text("VALUE = 42\n")
    result = subprocess.run(
        [str(executable), "-c", "import only_here; print(only_here.VALUE)"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "42\n"
    assert (
        str(target / "bin/python") in (target / "bin/pip").read_text().splitlines()[0]
    )
    second = ProgramConfig(
        "second", program.root, ProgramRuntime("python", environment="second")
    )
    environments.create_environment(paths, config, second)
    result = subprocess.run(
        [
            str(paths.environment("python", "second") / "bin/python"),
            "-c",
            "import only_here",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "only_here" in result.stderr

    # Execution must not expose the framework's entire site-packages directory.
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "atlas"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    ensure_dirs(paths)
    paths.host_file.write_text("version: 1\nhost:\n  id: isolated\n")
    commands = program.root / "commands"
    commands.mkdir(parents=True)
    (commands / "isolated.py").write_text(
        "import importlib.util\nassert importlib.util.find_spec('coverage') is None\nimport only_here\nassert only_here.VALUE == 42\n"
    )
    assert execute(paths, config, command_index(config)["isolated"], []) == 0


def test_perl_paths_context_shims_and_isolation(tmp_path, monkeypatch, capfd, perl):
    root = tmp_path / "program"
    commands = root / "commands/host"
    commands.mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "lib/ProgramModule.pm").write_text(
        'package ProgramModule; sub value { "program" } 1;\n'
    )
    (commands / "inspect.pl").write_text("""use strict; use warnings;
use JSON::PP; use ProgramModule; use PrivateDependency;
use Encode qw(decode_utf8); @ARGV = map { decode_utf8($_) } @ARGV;
open my $file, '<', $ENV{ATLAS_CONTEXT_FILE} or die $!;
local $/; my $context = decode_json(<$file>);
print encode_json({ context => $context, args => \\@ARGV, inc => \\@INC,
    module => ProgramModule::value(), dependency => PrivateDependency::value(),
    perl => $^X, startup => $ENV{PERL5OPT}, install => $ENV{PERL_MM_OPT} });
print STDERR "diagnostic\\n";
exit 7;
""")
    text = f"""runtime:
  perl:
    executable: {perl}
programs:
  inventory:
    root: {root}
    runtime:
      type: perl
      environment: inventory
"""
    home, _etc, var = configure_environment(monkeypatch, tmp_path, text)
    for key in ("PERL5LIB", "PERLLIB", "PERL_LOCAL_LIB_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / "foreign"))
    monkeypatch.setenv("PERL5OPT", "-MDoesNotExist")
    assert main(["env", "list"]) == 0
    assert "missing" in capfd.readouterr().out
    assert main(["env", "create", "inventory"]) == 0
    target = home / "environments/perl/inventory"
    (target / "lib/perl5/PrivateDependency.pm").write_text(
        'package PrivateDependency; sub value { "private" } 1;\n'
    )
    assert main(["check"]) == 0
    assert main(["env", "list"]) == 0
    assert "ready" in capfd.readouterr().out
    assert main(["shim", "generate"]) == 0
    capfd.readouterr()
    args = ["space value", "", "日本語", "$(false)", "--flag"]
    assert main(["run", "host-inspect", *args]) == 7
    output = capfd.readouterr()
    payload = json.loads(output.out)
    assert output.err == "diagnostic\n"
    assert payload["args"] == args
    assert payload["module"] == "program" and payload["dependency"] == "private"
    assert payload["startup"] is None and str(target) in payload["install"]
    assert str(tmp_path / "foreign") not in payload["inc"]
    assert str(target / "lib/perl5") in payload["inc"]
    assert payload["perl"] == str(perl)
    context_file = tmp_path / "received.json"
    context_file.write_text(json.dumps(payload["context"]))
    context = get_context(dict(os.environ, ATLAS_CONTEXT_FILE=str(context_file)))
    assert context.execution.run_id == payload["context"]["execution"]["run_id"]
    assert (
        context.program.environment == target and context.program.runtime_type == "perl"
    )
    child = subprocess.run(
        [str(home / "shims/host-inspect"), *args],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
    )
    assert child.returncode == 7
    shim_payload = json.loads(child.stdout)
    assert shim_payload["args"] == args and shim_payload["context"][
        "working_directory"
    ] == str(tmp_path)
    assert os.environ["PERL5OPT"] == "-MDoesNotExist"
    cfg = load_config(get_paths().config_file)
    other = ProgramConfig("other", root, ProgramRuntime("perl", environment="other"))
    other_target = environments.create_environment(get_paths(), cfg, other)
    env = environments.clean_perl_environment()
    env.update(environments.perl_environment(perl, other_target))
    result = subprocess.run(
        [str(perl), "-MPrivateDependency", "-e", "1"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "PrivateDependency" in result.stderr
    assert target != get_paths().environment("python", "inventory")
    records = [
        json.loads(line) for line in (var / "logs/runs.jsonl").read_text().splitlines()
    ]
    assert len(records) == 2 and all(record["exit_code"] == 7 for record in records)
    assert not {"args", "environment", "output"} & records[0].keys()
    assert list((var / "runtime-state/contexts").iterdir()) == []


def test_mismatch_and_creation_failures(tmp_path, monkeypatch, perl):
    paths, config, program = configuration(tmp_path, "perl", perl)
    target = environments.create_environment(paths, config, program)
    assert environments.create_environment(paths, config, program) == target
    manifest = target / ".atlas-interpreter.json"
    identity = json.loads(manifest.read_text())
    identity["config_sha256"] = "different-build"
    manifest.write_text(json.dumps(identity))
    with pytest.raises(ValueError, match="interpreter changed"):
        environments.environment_executable(paths, config, program)
    with pytest.raises(ValueError, match="interpreter changed"):
        environments.create_environment(paths, config, program)
    shutil.rmtree(target)
    target.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="must be a directory"):
        environments.create_environment(paths, config, program)
    target.unlink()
    with pytest.raises(ValueError, match="does not use"):
        environments.create_environment(
            paths, config, ProgramConfig("native", tmp_path, ProgramRuntime("native"))
        )

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "perl")

    monkeypatch.setattr(environments.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="probe failed"):
        environments.create_environment(paths, config, program)
    assert not target.exists()


def test_python_detects_replaced_interpreter(tmp_path):
    paths, config, program = configuration(tmp_path, "python", Path(sys.executable))
    target = environments.create_environment(paths, config, program)
    interpreter = target / "bin/python"
    interpreter.unlink()
    interpreter.symlink_to("/bin/sh")
    with pytest.raises(ValueError, match="does not match"):
        environments.environment_executable(paths, config, program)


def test_real_xs_load_and_interpreter_change_rejection(tmp_path, perl):
    paths, config, program = configuration(tmp_path, "perl", perl)
    target = environments.create_environment(paths, config, program)
    source = tmp_path / "xs"
    shutil.copytree(Path(__file__).parent / "fixtures/xs", source)
    clean = environments.clean_perl_environment()
    for argv in (
        [str(perl), "Makefile.PL", f"INSTALL_BASE={target}"],
        ["make"],
        ["make", "pure_install"],
    ):
        subprocess.run(
            argv, cwd=source, env=clean, check=True, capture_output=True, text=True
        )
    env = dict(clean, **environments.perl_environment(perl, target))
    result = subprocess.run(
        [
            str(perl),
            "-MAtlasTest::XS",
            "-e",
            'print AtlasTest::XS::answer(), "\\n", $INC{"AtlasTest/XS.pm"}',
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    answer, module_path = result.stdout.splitlines()
    assert answer == "42" and module_path.startswith(str(target / "lib/perl5"))
    assert list((target / "lib/perl5").rglob("XS.so"))
    # A different executable must never reuse the previously compiled environment.
    changed = tmp_path / "another-perl"
    shutil.copy2(perl, changed)
    changed_config = AtlasConfig(
        config.path, {"perl": RuntimeConfig(executable=changed)}, config.programs
    )
    with pytest.raises(ValueError, match="interpreter changed"):
        environments.environment_executable(paths, changed_config, program)


def test_cli_marks_incompatible_environment_invalid(
    tmp_path, monkeypatch, capsys, perl
):
    root = tmp_path / "program"
    root.mkdir()
    text = f"runtime:\n  perl:\n    executable: {perl}\nprograms:\n  tool:\n    root: {root}\n    runtime:\n      type: perl\n"
    configure_environment(monkeypatch, tmp_path, text)
    assert main(["env", "create", "tool"]) == 0
    manifest = get_paths().environment("perl", "tool") / ".atlas-interpreter.json"
    manifest.unlink()
    assert main(["env", "list"]) == 0
    assert "invalid" in capsys.readouterr().out
    assert main(["check"]) == 2
    assert "atlas check:" in capsys.readouterr().err


@pytest.mark.parametrize("language", ["python", "perl"])
def test_paths_reject_unsupported_languages_and_path_escape(language):
    paths = get_paths({})
    with pytest.raises(ValueError, match="runtime type"):
        paths.runtime("native", "1")
    with pytest.raises(ValueError, match="environment type"):
        paths.environment("native", "tool")
    for value in ("", ".", "..", "../escape", "/absolute"):
        with pytest.raises(ValueError, match="runtime version"):
            paths.runtime(language, value)
        with pytest.raises(ValueError, match="environment name"):
            paths.environment(language, value)
