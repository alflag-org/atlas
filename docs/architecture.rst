Atlas 1.0 target architecture
=============================

Status and current behavior
---------------------------

This page records the accepted Atlas 1.0 target. The current checkout implements strict
``release.yml`` parsing, command and job artifacts, job instances, advisory locks, timeout
handling, and correlated execution while retaining the scripts CLI, configuration, environment,
and filesystem names. Services, init artifacts, and the final terminology remain later stages.

Atlas responsibilities
----------------------

Atlas owns behavior that is reusable across sites and infrastructure repositories:

* runtime installation, status reporting, and atomic replacement;
* release source resolution and release validation, installation, and activation;
* manifest-declared artifact validation;
* public command shims and non-public job execution;
* one shared execution path for commands and jobs;
* run logging, nested-process correlation, timeout handling, locking, and argument redaction;
* validation, diff, installation, and removal of Atlas-owned init artifacts; and
* backend-independent safety rules.

Atlas does not own environment-specific desired state. Inventory, host and group variables,
Ansible roles and playbooks, Chef policy, Terraform resources, service deployment definitions,
repository Git operations, and persistent secret storage belong outside this repository.

Atlas release and artifact definitions
--------------------------------------

An **Atlas release** is a named, versioned installable directory containing ``VERSION`` and a
strict ``release.yml`` manifest. An **artifact** is one component declared by that manifest.
File discovery must not publish executable artifacts implicitly.

.. list-table::
   :header-rows: 1
   :widths: 16 42 18 24

   * - Artifact
     - Responsibility
     - Public shim
     - Invocation
   * - ``command``
     - One operator-facing operation with stdout, stderr, and an accurate exit status.
     - Yes
     - Directly from ``PATH`` or through Atlas.
   * - ``job``
     - One non-interactive, one-shot operation for a scheduler, service manager, or composition command.
     - No
     - Explicitly through ``atlas job``.
   * - ``service``
     - A logical declaration that binds one command or job to a user, arguments, environment, and init implementation.
     - No
     - Through the native service manager.
   * - ``init artifact``
     - A file supplied to a native service manager. Atlas 1.0 implements only systemd.
     - No
     - Installed through ``atlas init`` and operated with native tools.
   * - ``module``
     - Release-internal Python code imported by commands and jobs.
     - No
     - Imported; never executed directly.
   * - ``asset``
     - Static data required by the release, excluding environment-specific desired state.
     - No
     - Read by another artifact.

The first manifest schema is ``atlas.release/v1``. It supports Python command and job entrypoints
and systemd files referenced by service declarations. Validation fails before installation when a
required file is missing, an unknown key is present, a path escapes the release root, a symlink is
encountered, an artifact name is invalid, or a reference cannot be resolved.

Command names and composition
-----------------------------

Public command names use this grammar:

.. code-block:: text

   [a-z][a-z0-9]*(?:-[a-z0-9]+)*

Names normally use ``<domain>-<verb>``. The domain comes first so related commands are adjacent in
shell completion and listings. Read-only verbs include ``list``, ``show``, ``status``,
``validate``, ``check``, ``diff``, ``plan``, ``query``, ``inspect``, ``collect``, and ``render``.
Mutation verbs include ``apply``, ``create``, ``update``, ``delete``, ``enable``, ``disable``,
``install``, ``remove``, ``start``, ``stop``, ``rotate``, and ``restore``. Public names do not use
ambiguous verbs such as ``run``, ``exec``, ``manage``, ``operate``, ``process``, or ``do``.

A command that applies the same read-only primitive to multiple targets adds ``-many``. For
example, ``config-diff-many`` invokes the public ``config-diff`` executable once per target.
Composition commands call public executables as child processes; they do not import and bypass
the primitive command's internal entrypoint. Mutation commands do not use ``-all``.

Commands and jobs share execution behavior
------------------------------------------

Commands and jobs ultimately enter the same Atlas execution path. Atlas preserves argv rather
than invoking a shell, preserves the caller's working directory, prepends command shims and the
runtime environment to ``PATH``, records read-only Git context when available, and records one run
event with an accurate exit status.

Each run has ``run_id``, ``parent_run_id``, and ``operation_id`` values. A root run creates an
operation; a nested child receives the same ``operation_id`` and records the caller's ``run_id`` as
its parent. Timeout and interruption handling apply to the child process group so descendants are
not left running.

Filesystem target
-----------------

The Atlas 1.0 target removes scripts-specific path names:

.. code-block:: text

   /etc/atlas/
     config.yml
     host.yml
     jobs.d/
     env/

   /opt/atlas/
     bin/atlas
     bin/artifact-runner
     runtime/
     releases/<release>/<version>/
     current/<release> -> ../releases/<release>/<version>
     shims/
     lib/
     tmp/

   /var/lib/atlas/
     logs/runs.jsonl
     locks/
     cache/

The current ``/opt/atlas/scripts`` tree and ``script-runner`` name remain current behavior until
the filesystem migration pull request. The final state does not retain path aliases, dual-read
configuration, old environment variables, or old CLI aliases.

External repository boundary
----------------------------

An external infrastructure repository defines what a particular environment should contain.
Atlas invokes reusable operations against that repository from the caller's current working
directory. It does not hard-code ``/home/ops/repos``, discover repositories, or run ``clone``,
``pull``, ``checkout``, ``reset``, ``clean``, or dependency installation.

An Ansible repository therefore owns ``ansible.cfg``, inventories, variables, playbooks, roles,
collections, tests, and its own setup tasks. Atlas may provide reusable commands such as
``config-check`` and ``config-diff`` but must not copy that desired state below ``/opt/atlas``.

Release lookup through Global Registry requires a documented software-release contract. Atlas
must not invent that contract or represent a host-local alias map as registry integration.

Systemd boundary
----------------

Atlas validates, diffs, atomically installs, and removes only init artifacts declared by an Atlas
release. Systemd unit names use ``atlas-<release>-<service>.service`` and, when supplied,
``atlas-<release>-<service>.timer``. ``ExecStart`` uses the stable Atlas executable rather than a
versioned release path.

After an install or removal Atlas runs ``systemctl daemon-reload``. It does not enable, start,
stop, or restart units. Operators use ``systemctl`` and ``journalctl`` for those native lifecycle
operations.
