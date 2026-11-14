SPHINXBUILD ?= python -m sphinx
SPHINXOPTS ?=
SOURCEDIR = docs
BUILDDIR = build

.PHONY: html docs clean-docs

html:
	python -m pip install -e './src/python[dev]'
	$(SPHINXBUILD) $(SPHINXOPTS) -b html $(SOURCEDIR) $(BUILDDIR)/html

docs: html

clean-docs:
	rm -rf $(BUILDDIR)/html docs/generated
