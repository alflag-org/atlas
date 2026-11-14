# Contributing to Atlas

Keep each change focused on one behavior or documentation concern. Update tests and operator
documentation when a public command, configuration field, package artifact, or operational step
changes.

Do not commit credentials, production inventory, internal endpoints, personal paths, or generated
runtime state. Examples should use reserved domains such as `example.org`, documentation address
ranges such as `192.0.2.0/24`, and role-based paths such as `/srv/atlas/program` or
`/srv/provisioning`.

## Prepare the development environment

Atlas supports Python 3.11 through 3.14. The repository uses [mise](https://mise.jdx.dev/) to pin
the development toolchain:

```bash
mise install
mise run setup
```

Perl tests use the system Perl, `local::lib`, MakeMaker, and a C compiler for an actual XS
build. Install these prerequisites with the host's normal package tooling.

Docker Compose is optional and is only needed for the container checks.

## Verify a change

Run the full local check before submitting a pull request:

```bash
mise run check
make clean-docs html SPHINXOPTS=-W
```

`mise run check` runs Ruff, Python tests with 100% line and branch coverage, both package
builds, Perl SDK tests, and an installed-SDK execution test against Atlas. Tests use real
interpreters, module search paths, and a compiled XS extension. Set `ATLAS_TEST_PERL` to choose
the Perl used by Python integration tests.

For independent distribution checks, run Python tools from `src/python` and
`perl Makefile.PL && make test dist` from `src/perl`. Python daemon and IPC examples
live in `src/python/examples` so the Python source distribution includes its own test inputs. When container behavior changes, also run:

```bash
docker compose build
docker compose run --rm check
```

## Submit a pull request

Explain the problem, the changes made, and their operational impact. Include deployment
consequences when they exist. Keep unrelated changes in separate pull requests.
