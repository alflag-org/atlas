Staged migration from Atlas 0.3 to Atlas 1.0
============================================

Current and final interfaces
----------------------------

The table below records the Atlas 0.3 baseline and the final target. This checkout has completed
the manifest and job stages: implicit command discovery is removed, jobs and job instances use the
shared correlated executor, and only commands receive shims. The scripts CLI, configuration
environment, and filesystem paths remain current behavior.

.. list-table::
   :header-rows: 1

   * - Concern
     - Atlas 0.3
     - Atlas 1.0 target
   * - Release declaration
     - ``commands/**/*.py`` discovery
     - strict ``release.yml`` with ``atlas.release/v1``
   * - Executable kinds
     - every discovered file is a public command
     - public commands and non-public jobs
   * - Release storage
     - ``/opt/atlas/scripts/releases``
     - ``/opt/atlas/releases``
   * - Active release links
     - ``/opt/atlas/scripts/current``
     - ``/opt/atlas/current``
   * - Runner
     - ``script-runner``
     - ``artifact-runner``
   * - Init files
     - unmanaged by Atlas
     - declared systemd artifacts managed through ``atlas init``

The migration is staged to keep every review and validation bounded. Staging does not create a
compatibility promise: the final release does not read the old configuration, expose old CLI
aliases, map old environment variables, or preserve old filesystem aliases.

Pull request sequence
---------------------

1. Record the target architecture, terminology, artifact definitions, command naming rules,
   repository ownership, migration order, and rollback rules without changing runtime behavior.
2. Add strict ``release.yml`` parsing, command artifacts, explicit manifest discovery, collision
   handling, migrated examples, and complete tests. Do not add jobs, init support, provisioning
   content, or path renames.
3. Add shared command and job execution, ``atlas job`` and job instances, nested run correlation,
   Git context, timeout, process-group handling, and advisory locks. Commands alone receive shims.
4. Add the first-party ``operations`` release with separate ``config-validate``, ``config-check``,
   ``config-diff``, ``config-apply``, ``inventory-show``, and ``config-diff-many`` commands.
   Composition invokes the primitive executable. Register the release only after Global Registry
   exposes a documented software-release contract.
5. Create ``alflag-org/provisioning`` from the Daedalus Ansible history. Make its repository root
   the Ansible project root; do not copy wrappers, Atlas packaging, shims, or the Daedalus Python
   package.
6. Compare replacement operations against Daedalus on representative hosts. Remove its registry
   entry and stale shim only after real-host smoke tests, then archive the repository.
7. Add systemd validation and ``atlas init list|diff|install|remove``. Install unit files
   atomically, run ``systemctl daemon-reload``, and leave enable/start/stop/restart to systemd.
8. Replace scripts-specific storage, runner, environment, status, and CLI terminology with the
   Atlas 1.0 paths and names. Remove the old surfaces instead of retaining compatibility aliases.
9. Classify later Hermes and Ares content by responsibility. Move reusable primitives or
   composition into the operations release and move desired state into the appropriate external
   repository; do not merge whole repositories into Atlas.

Production cutover
------------------

Before changing a host, record installed release sources and active versions, stop schedulers and
services that invoke old shims, and back up ``/etc/atlas``, ``/opt/atlas/scripts``, and required
run logs. Prepare the independent provisioning repository and its dependencies explicitly.

Run new read-only validation and diff commands against representative targets and compare them
with the old wrapper's output. Install the new release, rebuild the runtime and shims, inspect job
instances and systemd diffs, and only then switch callers. Remove the old tree after the agreed
observation period.

Rollback by stage
-----------------

.. list-table::
   :header-rows: 1

   * - Stage
     - Rollback
   * - Manifest and job support
     - Revert the stage before its release; restore the previously active release directory and link.
   * - Operations release
     - Stop new callers and return them to the old wrapper retained during comparison.
   * - Provisioning repository
     - Keep the old Daedalus checkout read-only and restore its pinned revision.
   * - Systemd artifacts
     - Remove installed ``atlas-<release>-<service>`` unit files and run ``systemctl daemon-reload``.
   * - Filesystem terminology
     - Stop new callers, restore the backed-up Atlas 0.3 tree and configuration, and restore the old executable path.

Rollback restores one complete version; it does not activate an in-process dual-read mode. Atlas
1.0 must never be pointed at an Atlas 0.3 filesystem with the expectation of automatic conversion.
