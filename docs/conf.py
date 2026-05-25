from __future__ import annotations

from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

project = "atlas"
author = "atlas contributors"
language = "ja"
with (ROOT / "pyproject.toml").open("rb") as fh:
    release = tomllib.load(fh)["project"]["version"]

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

autosummary_generate = True
autodoc_typehints = "description"
autodoc_member_order = "bysource"

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "shibuya"
html_title = "Atlas ドキュメント"
html_theme_options = {
    "accent_color": "blue",
    "github_url": "https://github.com/viasnake/atlas",
}
