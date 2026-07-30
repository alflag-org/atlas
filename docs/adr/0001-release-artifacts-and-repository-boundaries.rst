ADR 0001: Declare release artifacts and keep desired state external
===================================================================

:Status: Accepted
:Date: 2026-07-31

Context
-------

Atlas 0.3 treats a release as a directory of automatically discovered Python commands. Every
discovered command receives a shim, and scripts-specific names appear in the CLI, environment,
and filesystem. That model cannot express a non-public scheduled job, a logical service, or the
init files required by that service without adding hidden conventions.

Atlas also needs reusable infrastructure operations without becoming the owner of inventory,
playbooks, provider resources, or environment-specific service definitions.

Decision
--------

Atlas 1.0 will use a strict ``release.yml`` manifest with schema ``atlas.release/v1``. A release
may declare commands, jobs, services, init artifacts, modules, and assets. Only commands receive
public shims. Jobs remain non-interactive one-shot executables reached through ``atlas job``.
Services refer to a command or job and to init artifacts; service declarations are not themselves
executables. The first implemented runtime is Python and the first implemented init adapter is
systemd.

Command and job execution will share logging, nested-run correlation, path construction, timeout,
lock, signal, and redaction behavior. Public commands use domain-first names, and composition
commands invoke primitive public executables rather than importing their implementation.

Environment-specific desired state stays in independent repositories. Atlas will not manage
those repositories' Git state, discover them automatically, or install their dependencies.

Implementation will follow the stages in :doc:`../migration`. This ADR does not change current
runtime behavior. The final migration removes the scripts-specific paths, commands, environment
variables, runner name, and implicit discovery. No compatibility layer remains in Atlas 1.0.

Consequences
------------

Release authors must declare executable artifacts explicitly and receive validation errors before
installation. Commands remain discoverable from ``PATH`` while jobs cannot accidentally become
operator-facing commands. Systemd integration can refer to a stable Atlas entrypoint rather than
a versioned release directory.

Atlas and external infrastructure repositories can evolve independently across a documented
execution boundary. An operator remains responsible for repository checkout, dependency setup,
desired-state review, and native service lifecycle operations.

Each migration pull request must keep its own tests green and document rollback. An intermediate
stage may still use a current path that a later stage replaces; it must not add an alias or
dual-read fallback solely to preserve the superseded interface.

Alternatives not selected
-------------------------

Atlas will not introduce a workflow DSL, arbitrary shell hooks, automatic repository discovery,
automatic Git mutation, automatic dependency installation, a universal project manifest, or a
generic adapter framework without demonstrated use cases. Desired state will not be packaged as
an Atlas asset.
