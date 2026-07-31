Concepts
========

Atlas responsibilities
----------------------

Atlas owns release acquisition, strict artifact validation, atomic installation and activation,
the shared Python runtime, public command shims, non-public job execution, correlated run logs,
timeouts, advisory locks, and Atlas-owned systemd artifact management.

Atlas does not own environment desired state. Keep Ansible inventory, playbooks, roles, Chef
policy, Terraform definitions, service-specific units, and provider configuration in independent
infrastructure repositories. Atlas also does not clone, pull, or switch those repositories.

Artifact types
--------------

``command``
   An operator-facing executable. Commands receive shims under ``/opt/atlas/shims``.

``job``
   A non-interactive, one-shot executable. Jobs never receive shims and run through
   ``atlas job``.

``service``
   A logical command or job reference with native init artifacts. A service is not itself an
   executable.

``init artifact``
   A native service-manager definition. Atlas v1 implements systemd only.

``module``
   A release-private Python library under ``modules/``.

``asset``
   A static file required by release code. Environment inventory and desired state are not assets.

UNIX command contract
---------------------

A primitive command handles one project, one target, one main operation, and one main child
process. A composition command invokes public primitive executables as child processes. It does
not import their internal implementation.

Commands write results to stdout, diagnostics and progress to stderr, preserve child exit status,
use argument lists with ``shell=False``, and make mutations explicit in their names.

Failure policy
--------------

Atlas fails closed when a manifest has unknown keys, a path leaves the release root, a release
contains symlinks, a command name collides, a job reference is missing, or a systemd destination
is a symlink. The jobs, locks, and logs directories cannot be symlinks; instance, lock, and run-log
files must have the expected regular-file type. Atlas does not silently fall back to file
discovery or legacy filesystem paths.
