from __future__ import annotations

from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

project = "atlas"
author = "atlas contributors"
language = "en"
with (ROOT / "pyproject.toml").open("rb") as fh:
    release = tomllib.load(fh)["project"]["version"]
html_baseurl = "https://atlas-docs.jp0.workers.dev/"

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
templates_path = ["_templates"]

html_theme = "shibuya"
html_title = "Atlas documentation"
html_theme_options = {
    "accent_color": "blue",
    "color_mode": "auto",
    "ethical_ads_publisher": "",
    "globaltoc_expand_depth": 1,
    "github_url": "https://github.com/alflag-org/atlas",
    "nav_links": [
        {"title": "Usage", "url": "usage"},
        {"title": "Configuration", "url": "configuration"},
        {"title": "Python API", "url": "api"},
    ],
    "nav_links_align": "left",
    "toctree_titles_only": False,
}
html_context = {
    "default_mode": "auto",
}
