from __future__ import annotations

from pathlib import Path

from atlas.cli import main


def test_install_list_which(monkeypatch, tmp_path: Path, capsys) -> None:
    home = tmp_path / "opt/atlas"
    etc = tmp_path / "etc/atlas"
    var = tmp_path / "var/lib/atlas"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "host.yml").write_text("name: n1\n", encoding="utf-8")

    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setenv("ATLAS_SCRIPTS_DIR", str(home / "scripts/current"))

    release_src = Path("examples/scripts-release").resolve()
    assert main(["scripts", "install", str(release_src)]) == 0
    assert main(["scripts", "list"]) == 0
    out = capsys.readouterr().out
    assert "sample" in out
    assert "group-nested-sample" in out

    assert main(["which", "sample"]) == 0
    out2 = capsys.readouterr().out.strip()
    assert out2.endswith("/commands/sample.py")
