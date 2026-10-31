Operator reference
==================

Install Atlas as the host-side runtime and keep the automation programs it executes outside the
Atlas directory. Atlas owns runtime links, Python venvs, generated shims, context files, and run
records.

Filesystem layout
-----------------

The default layout is:

.. code-block:: text

   /etc/atlas/
   ├── config.yml
   └── host.yml

   /opt/atlas/
   ├── runtimes/python/
   ├── venvs/
   ├── shims/
   └── launchers/

   /var/lib/atlas/
   ├── logs/runs.jsonl
   └── runtime-state/

The paths can be redirected for tests with ``ATLAS_HOME``, ``ATLAS_ETC_DIR``,
``ATLAS_VAR_DIR``, ``ATLAS_RUNTIMES_DIR``, ``ATLAS_VENVS_DIR``, ``ATLAS_SHIMS_DIR``,
``ATLAS_LAUNCHERS_DIR``, ``ATLAS_CONFIG_FILE``, and ``ATLAS_HOST_FILE``.

Register programs
-----------------

``config.yml`` contains only runtime selections and local program registrations:

.. code-block:: yaml

   runtime:
     python:
       version: "3.13"

   programs:
     provisioning:
       root: /srv/provisioning
       runtime:
         type: python
         python: "3.13"
         venv: provisioning
     image-tool:
       root: /opt/image-tool
       runtime:
         type: native

``root`` must be an absolute path. A Python program uses its own named venv. A native program is
started without a Python intermediary. The global Python selection is used when a Python program
does not specify its own version. A configured Python executable may be supplied as
``runtime.python.executable`` when the host's managed interpreter is not exposed through pyenv.

Install and update program trees with the program's own packaging or deployment tools.
Atlas uses the local files without managing their versions.

Command discovery and shims
---------------------------

Python files below ``program-root/commands`` are commands. Executable files below
``program-root/commands`` and, for native programs, ``program-root/bin`` are native commands.
Path segments use lowercase letters, digits, and hyphens. Nested paths are joined with hyphens.
Atlas rejects symlinks and duplicate command names.

Generate shims after registering or changing a program:

.. code-block:: console

   $ atlas command list --verbose
   $ atlas shim generate
   $ export PATH="/opt/atlas/shims:$PATH"
   $ host-diff web01

Generated shims call ``python -m atlas.cli run <command>``. They do not contain a second
execution implementation.

Runtime and venvs
-----------------

``atlas runtime install`` resolves each configured Python version and exposes it below
``/opt/atlas/runtimes/python``. If ``pyenv`` is available, Atlas installs a missing selected
version through that existing runtime manager; otherwise use ``runtime.python.executable`` or an
explicitly provisioned interpreter. Atlas does not use the host's system Python implicitly and
does not install program dependencies. ``atlas venv create <program>`` creates the dedicated venv.
Install dependencies using the program's existing ``pyproject.toml``, requirements file, lock
file, or other standard tooling.

.. code-block:: console

   $ atlas runtime status
   $ atlas runtime install
   $ atlas venv create provisioning
   $ atlas venv list

Execution context
-----------------

``/etc/atlas/host.yml`` identifies only the current host:

.. code-block:: yaml

   version: 1
   host:
     id: control01
     role: control
     site: kanagawa01

During execution Atlas writes a short-lived JSON document and sets ``ATLAS_CONTEXT_FILE``. The
document contains the host, standard paths, program, command, working directory, and
``run_id``, ``parent_run_id``, and ``operation_id``. The same identifiers are available as
environment variables. Python code can call ``atlas_core.get_context()``; other languages can
parse the JSON file directly.

The diagnostic command does not require a registered program:

.. code-block:: console

   $ atlas context
   $ atlas status
   $ atlas check

Run records
-----------

Each spawned command appends one JSON object to ``/var/lib/atlas/logs/runs.jsonl``. It records the
UTC timestamp, host id, user, program, command, run identifiers, working directory, duration, and
exit status. Stdout and stderr are not copied into this log.

The executor preserves the child exit status, returns ``124`` for a timeout, and maps a signal
termination to ``128 + signal``. The child is launched with the caller's working directory and
with an exact argument vector; shell interpretation is not used.

Each execution requires Linux with ``/dev/shm`` mounted as tmpfs. Atlas creates
an owner-only directory there and exposes its path as ``ATLAS_RUN_TEMP_DIR``.
Programs can obtain it through ``atlas_core.execution.get_run_directory()``.
Keep subprocesses in the inherited process group when using this storage; do
not start a new session or leave background work running beyond the command.
Nested Atlas executions receive separate subdirectories within the same execution
tree. Atlas registers their process groups before starting command code and
serializes registration with subtree shutdown.

On completion, timeout, SIGINT or SIGTERM, Atlas stops the command's process
group and registered nested groups, including descendants left by an exited
group leader. The outer supervisor deletes the execution tree's directories
after stopping those groups. SIGINT and SIGTERM are forwarded without changing
the signal, with SIGKILL escalation after the grace period. Run records are
written after shutdown. SIGKILL of the Atlas supervisor or host
failure cannot run cleanup. Clear abandoned volatile files before reusing a
recovered host. Disable swap or use encrypted swap for secret-bearing storage.

External secrets
----------------

Programs can retrieve required secrets through ``atlas_core.secrets``. A logical
name has the form ``system.purpose[.detail]``, for example
``mysql.backup.password``. Programs declare these names; the Atlas host owns the
mapping to external identifiers. Values remain in the retrieving process, and
successful reads are cached only for the lifetime of that provider instance.
Create a new instance for each execution to observe subsequent secret updates.

The initial adapter uses the official Bitwarden Secrets Manager Python SDK.
Install the optional dependency in every interpreter that retrieves secrets,
including a program's dedicated virtual environment::

    python -m pip install 'atlas[secrets]'

The SDK requires a supported platform wheel. Atlas's core installation does not
require it. ``SecretProvider`` exposes only ``get`` and ``get_many``; provider
configuration, authentication and backend identifiers stay behind the adapter.
There is no secret creation, rotation, persistent cache, or secret export API.

Host setup
~~~~~~~~~~

In Bitwarden Secrets Manager Cloud, create a project and a machine account with
read permission on that project. Create an access token for that account. Keep
administrative recovery access outside the infrastructure being rebuilt. Do not
use a production access token in CI.

Place the access token in ``/etc/atlas/credentials/secret-provider`` without
printing it or putting it in shell arguments. The regular file must belong to
the user executing the program, have mode ``0600`` and a single hard link, and
have no symlink components. Each execution account needs its own securely
provisioned credential. Atlas does not change users or grant access.

Create ``/etc/atlas/secrets.yml`` with identifiers, never values:

.. code-block:: yaml

    provider: bitwarden
    config:
      region: us
      credential_file: /etc/atlas/credentials/secret-provider
      project_id: 00000000-0000-4000-8000-000000000001
    mappings:
      mysql.backup.password:
        secret_id: 00000000-0000-4000-8000-000000000002

Replace these example UUIDs with the project's and secret's identifiers. The
``region`` is ``us`` or ``eu``. Responses must belong to the configured project.
Duplicate YAML keys, unsupported providers, malformed mappings, missing values,
empty values and retrieval errors fail closed. Configuration errors are distinct
from retrieval errors. A missing SDK is a local configuration error. Provider
error bodies are not included in diagnostics.
The adapter never requests an SDK state file.

``ATLAS_ETC_DIR`` selects a different Atlas configuration directory, as it does
for other Atlas configuration files. Keep this host configuration outside the
Provisioning repository. The bootstrap credential is separate from this mapping
and must not be backed up into Git.

Check the names needed by an execution without displaying their values::

    atlas secret check mysql.backup.password

The command reports the available count and returns zero only if every requested
name resolves. Failure returns status 2. There is no value-printing command.
An empty request is not an authentication check and is rejected by the CLI.

Program integration
~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    from atlas_core.secrets import load_provider

    provider = load_provider()
    values = provider.get_many(["mysql.backup.password"])

Resolve the complete required set before starting operations. Do not log values,
returned dictionaries, or local variables containing them. Program output is
outside Atlas's run-record redaction boundary. Consumers must protect all output
channels and use Ansible's ``no_log: true`` for tasks that expose secrets.
The public Python API returns strings; it is not a sandbox for trusted programs.
Tests can supply any object implementing ``get`` and ``get_many`` without loading
the SDK or connecting to a service.

To add a secret, create it in the external project, add its logical-name mapping
on the Atlas host, and declare the logical name in the consuming program. To
rotate a value, update the external secret and coordinate the corresponding
service credential change. A new program execution retrieves the updated value;
Atlas does not rotate the target service credential itself.

Recovery and manual acceptance
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use a dedicated test project and synthetic values for this acceptance procedure.
Do not copy an existing control host's disk, snapshot, secret cache, or credential
file into the test host.

1. Provision a new Linux control host and install Atlas and its optional secrets
   dependency. Register a clean checkout of the automation program and prepare
   its interpreter and Ansible dependencies.
2. Sign in to Bitwarden using recovery access held outside Proxmox. Issue a new
   access token for the read-only machine account and securely place it in the
   new host's owner-only credential file.
3. Restore the non-secret Atlas configuration and logical-name mappings from
   their independent backup. Restore host identity and program registration.
4. Run ``atlas secret check`` with every name required by the test execution.
5. Start the program against a disposable local Ansible target and verify the
   expected result without displaying the injected value.
6. Change a synthetic value in Bitwarden, start a new execution, and verify it
   observes the change. Revoke the test token and verify a subsequent execution
   fails before Ansible starts. Confirm that logs and persistent directories
   contain neither synthetic values nor the bootstrap token.

A mocked-provider test does not prove this external recovery procedure. Record
live acceptance evidence separately from repository test results. Existing
secret copies must remain recoverable until external storage and recovery have
been verified; remove superseded local or Ansible Vault copies after that check.
A credential committed in plaintext requires rotation, even if removed from HEAD.
History rewriting requires a separately agreed scope.

Changing providers
~~~~~~~~~~~~~~~~~~

Implement the same retrieval contract in another adapter, then migrate data using
that provider's supported tools. Maintain a controlled correspondence between
each logical name, the old identifier, and the new identifier. Keep exported
values out of Git, command output, and persistent plaintext staging files.
Verify destination values and recovery with a test execution before switching
host configuration and retiring old access. Only the adapter, host configuration,
mappings and bootstrap credential change; program declarations and Ansible roles
retain their logical names. Atlas does not currently ship other adapters.

Official setup details are available in the `SDK documentation
<https://bitwarden.com/help/secrets-manager-sdk/>`_ and `machine account guidance
<https://bitwarden.com/help/machine-accounts/>`_.

.. _daemon-environment:

Standalone daemon environment
-----------------------------

Install Atlas in the daemon's Python environment and run its executable or
module directly. See :doc:`api` for the lifecycle API and ``sampled`` example.

The SDK selects state and runtime directories separately:

.. list-table:: Daemon directories
   :header-rows: 1

   * - Purpose
     - Service-manager override
     - Standalone default
   * - Persistent state
     - ``STATE_DIRECTORY``
     - ``$XDG_STATE_HOME/atlas/<name>`` or ``~/.local/state/atlas/<name>``
   * - Runtime files and sockets
     - ``RUNTIME_DIRECTORY``
     - ``$XDG_RUNTIME_DIR/atlas/<name>`` or a random private temporary directory

Set ``name`` when calling ``run()``; it defaults to ``daemon``. Names start with
a lowercase letter and contain lowercase letters, digits, and single separating
hyphens. Use different names or explicit directories for separate instances.
The SDK does not lock instances against concurrent starts.

Each manager override accepts one absolute path, not a colon-separated list.
All directory paths must be absolute, with no parent traversal or symlink
components. Missing directories are created with mode ``0700``. The selected
directories and any XDG runtime base must belong to the caller and have mode ``0700``. Existing permissions are left
unchanged. Invalid paths, ownership, permissions, or filesystem access fail startup.
Empty XDG variables use defaults; empty manager overrides are errors.

State and explicitly selected runtime directories remain after exit. The temporary
runtime fallback is removed when ``run()`` exits. SIGKILL or host failure can leave
temporary files behind. Read the path from ``ctx.runtime_dir``; it changes
between starts.
The fallback may be disk-backed, so do not store secrets there.

For systemd, start the executable directly and set directory modes to ``0700``.
Use ``Type=notify`` and ``NotifyAccess=main`` to wait for readiness. ``ctx.ready()``
sends ``READY=1`` to ``NOTIFY_SOCKET``, which accepts a filesystem path or a Linux
abstract socket address. Without that variable, readiness is recorded locally.
An invalid or unavailable socket raises an error; sending a datagram does not
confirm that the manager accepted it. Readiness reports initialization, not ongoing
health. The SDK provides no health check or watchdog.

The daemon runs as the invoking user. Provision directory ownership and service
permissions for that account; the SDK does not change users.

Managing host daemons
---------------------

An operator declares each service in ``/etc/atlas/daemons.d/<name>.yml``.
``ATLAS_ETC_DIR`` changes this configuration root. Provision the application,
its OS account, and its working directory before installing the service::

    schema: atlas.daemon-instance/v1
    name: sampled
    executable: /opt/sampled/bin/sampled
    arguments: []
    user: sample
    working_directory: /var/lib/sample
    environment_files: []
    restart: on-failure
    restart_delay_seconds: 5
    startup_timeout_seconds: 30
    stop_timeout_seconds: 30
    notify: true

The filename and ``name`` must agree. Names start with a lowercase letter and
contain at most 63 lowercase letters, digits, underscores, or hyphens. Paths
are absolute. Configuration and environment files must be regular files owned
by root or the caller, without group or other write access. Their directory
components must also be owner controlled; symlinks are rejected. Executables
may be native programs or installed Python entry points. No application code
is imported during management.

``restart`` accepts ``no`` (quote it in YAML), ``on-failure``, or ``always``.
Timeouts and restart delays must be positive and finite. Set ``notify: false``
for applications that do not send systemd readiness notifications; these use
``Type=exec``. Unknown fields and invalid types are rejected.

Inspect, install, and start are separate operations::

    atlas daemon list
    atlas daemon inspect sampled --json
    atlas daemon validate sampled
    atlas daemon install sampled
    atlas daemon enable sampled
    atlas daemon start sampled
    atlas daemon status sampled --json
    atlas daemon logs sampled --since today

``install`` atomically writes ``atlas-daemon-sampled.service`` in
``/etc/systemd/system`` and reloads systemd. It restores the previous unit if
reload fails. It does not enable or start the service, and will not overwrite
an unmanaged unit. The system manager runs the executable directly as the
configured user, with private state and runtime directories at
``/var/lib/atlas/sampled`` and ``/run/atlas/sampled``. Arguments are literal;
systemd variable and specifier expansion is escaped. See the upstream
`command-line rules <https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Command%20lines>`_.

Use ``stop``, ``restart``, and ``disable`` explicitly. ``remove`` refuses an
active or enabled unit, removes only the managed unit, and retains application
state. Calls use the caller's OS authorization without ``sudo`` or an
interactive privilege prompt. ``status`` and ``stop`` still work when the
configured executable has disappeared.

Status JSON uses ``atlas.daemon-status/v1``. ``active`` and ``enabled`` retain
systemd's state strings; ``installed`` reports a loaded unit. ``ready`` is a
boolean for notify services and ``null`` when readiness is unsupported.
``health`` is ``unknown``: running is not evidence of application health.
``inspect --json`` returns the instance schema. Failures exit 2; JSON diagnostics
use ``atlas.daemon-error/v1`` with ``invalid_configuration`` or
``operation_failed``. ``logs`` returns journalctl's exit code.

Application output and service events stay in journald. Successful operator
requests also emit ``atlas.daemon-event/v1`` records through syslog with the
instance, action, and caller UID. No daemon is recorded as a permanent entry
in the completed-command run log.

Cluster setup
-------------

Cluster is optional. Install ``atlas[cluster]`` in the environment running
``atlasd``. The extra supplies X.509 support; ordinary commands and the Daemon
SDK do not require it. ``atlasd`` is a foreground application using the same
SDK as ``sampled``. It manages Cluster membership and command operations;
other services run independently.

Give each instance private persistent and runtime directories. For a direct
start, set ``STATE_DIRECTORY`` and ``RUNTIME_DIRECTORY`` as described above::

    atlasd --endpoint tls://192.0.2.10:7443 --display-name control

The endpoint is an explicit IPv4 or bracketed IPv6 address and TCP port.
It is both the listening and advertised address. No TCP listener starts until
``init`` or ``join`` succeeds. Restarted members use their persisted endpoint.
To run under systemd, declare an ``atlasd`` instance with this executable and
arguments, then install and start it with ``atlas daemon``.

The local CLI connects to ``/run/atlas/atlasd/control.sock``. Set
``ATLAS_CLUSTER_SOCKET`` or use ``atlas cluster --socket <path> ...`` for
another instance. The socket and its directory admit only the daemon's OS
account. Provision access deliberately; Atlas does not change accounts.

Initialize one authority, then create a short-lived invitation::

    atlas cluster init --name lab
    atlas cluster invite create --ttl 10m --label group=app --output join.json

Transfer the invitation through an authenticated confidential channel. On the
joining node, start ``atlasd`` with its own endpoint and directories, then run::

    atlas cluster join --invite-file join.json
    atlas cluster members
    atlas cluster status

Invitations contain a secret and pinned CA certificate. Files must be owned
by the caller with mode ``0600``; output files are created exclusively and
never overwritten. Invitations expire within one hour and admit one node.
The joining node generates its private key locally. Subsequent peer calls
use TLS 1.3 with certificates plus a live membership and permission check.
Display names and host IDs confer no authority.

Invited members receive ``read`` and ``execute`` permissions by default.
Use repeated ``--permission`` flags to set permissions explicitly; ``admin``
permits invitations and removal. Labels are set by the inviting administrator.
``atlas cluster leave`` revokes the calling member; ``atlas cluster remove
<node-id>`` revokes another member. The authority cannot remove itself.
Revocation retains local identity and audit state. Use a new isolated instance
for a new membership.

If the join reply is lost, the node retains its key and refuses another join.
``atlas cluster join --invite-file join.json --reconcile`` retrieves an already
committed admission using the original invitation and key. It never admits a
new member. An unresolved admission remains unjoined.

The authority serializes membership changes in SQLite with WAL and full
synchronous commits. Local status on another node is marked ``stale``;
``members`` obtains a current authority response. An unreachable authority
makes new membership changes and command operations unavailable. There is no
automatic election, failover, discovery-based admission, or promotion from a
cached snapshot. Certificates expire after one year; automatic renewal is not
implemented. Recommission nodes before expiry.

Cluster workflows
-----------------

Workflows are operator-owned files. Commands must already be registered on
each target. A group selects the ``group`` label assigned during admission;
``targets: {nodes: [<node-id>, ...]}`` selects explicit members::

    schema: atlas.workflow/v1
    name: deploy
    steps:
      - id: check
        targets: {group: app}
        command: app-check
        mutates: false
        max_parallel: 3
      - id: deploy
        needs: [check]
        targets: {group: app}
        command: app-deploy
        arguments: []
        mutates: true
        rollout: {max_parallel: 1, max_unavailable: 1}
        timeout_seconds: 120
        verify: {command: app-health, timeout_seconds: 30}

Plan without running commands, review the file, then confirm its digest::

    atlas cluster plan deploy --workflow deploy.yml --out deploy.plan.json
    atlas cluster apply deploy.plan.json --confirm <digest>
    atlas cluster operation inspect <operation-id>
    atlas cluster operation reconcile <operation-id>

Plans expire after ten minutes. Their digest binds resolved node IDs,
commands, literal arguments, dependencies, concurrency, verification, required
permission, membership revision, registration configuration, and command entry-point hashes. Entry points
are checked again before execution. Program trees remain mutable: imported
modules, dependencies, and concurrent edits are not immutable snapshots.
Keep secret values out of workflows; resolve references inside applications.

Apply acknowledges a durable operation; inspect reports its progress.
Independent steps are processed in dependency order, with parallel execution
within each step. There are at most 100 steps and 32 concurrent targets per
step. Step IDs use up to 64 letters, digits, underscores, or hyphens and start
with a letter. ``rollout`` bounds concurrency by both supplied limits. A command or
verification timeout is at most 120 seconds. The first failed or unknown
result prevents further dependent work and queued rollout targets.

The authority permits one operation at a time, including read-only operations.
It issues a monotonically increasing fencing epoch and a five-minute lease;
workers validate the grant before execution and verification. Each worker
records execution identity before launching ``atlas run``. Repeated requests
return journal state. Executor arguments, cleanup, exit conventions, and run
records retain their existing behavior; ``operation_id`` links all runs, and
``parent_run_id`` identifies the worker step.

An operation moves from ``prepared`` to ``running`` and then ``succeeded``,
``failed``, or ``unknown``. Worker results also distinguish ``timed_out``.
Connection loss, process crashes, and incomplete journals can leave an outcome
unknown. The lease remains reserved and new operations are refused even after
its deadline. Reconcile reads worker journals without re-executing commands;
when all dispatched work has a terminal result it marks the interrupted
operation ``cancelled`` and releases the reservation. It does not run omitted
steps or roll back changes. A worker whose journal is still unknown requires
application-specific investigation; Atlas cannot infer external side effects
from a process exit or a successful health probe.

Cluster state and recovery
--------------------------

``cluster.sqlite3`` contains identity, CA material on the authority, invitation
hashes, membership, operation journals, and fencing state. Protect the entire
state directory and its backups as credentials. One process holds the database
lock. Database corruption fails startup; it is not replaced with an empty
Cluster. Restart changes incomplete executions to ``unknown``.

For a consistent backup, stop the instance and copy its private state directory,
including any SQLite WAL files, to protected storage. Keep the original
permissions. Restore only while stopped. Before restoring an authority on a
replacement host, fence the old host and prevent it from serving requests.
Restore the authority's complete state and original endpoint, then inspect
membership and reconcile incomplete operations before admitting new work.
Never reconstruct an authority from a follower's local status or clear an
unknown operation merely to resume changes.
