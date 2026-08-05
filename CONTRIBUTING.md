# Contributing to Atlas

Keep each change focused on one behavior or documentation concern. Update tests and operator
documentation when a public command, configuration field, release artifact, or operational step
changes.

Do not commit credentials, production inventory, internal endpoints, personal paths, or generated
runtime state. Examples should use reserved domains such as `example.org`, documentation address
ranges such as `192.0.2.0/24`, and role-based paths such as `/srv/atlas/source` or
`/srv/provisioning`.

## Prepare the development environment

Atlas supports Python 3.11 through 3.14. The repository uses [mise](https://mise.jdx.dev/) to pin
the development toolchain:

```bash
mise install
mise run setup
```

Docker Compose is optional and is only needed for the container checks.

The host test environment must provide a writable delegated Linux cgroup v2 parent; the test and
release workflows fail closed when it is unavailable. The default Docker smoke image deliberately
has no delegation and verifies that an attempted release execution returns 125 instead of starting
release code.

## Verify a change

Run the full local check before submitting a pull request:

```bash
mise run check
make clean-docs html SPHINXOPTS=-W
```

`mise run check` runs Ruff, the test suite with 100% line and branch coverage, and the package
build. When container behavior changes, also run:

```bash
docker compose build
docker compose run --rm check
docker compose run --rm atlas atlas release list
```

## Submit a pull request

Explain the problem, the changes made, and their operational impact. Include compatibility or
deployment consequences when they exist. Keep unrelated changes in separate pull requests.
