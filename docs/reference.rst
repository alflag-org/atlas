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

.. _application-logging-operations:

Application diagnostics
-----------------------

Python applications can use :ref:`application-logging-api` for diagnostics in
both stderr and a shared file. The default layout is flat::

   /var/log/atlas/
   ├── atlas.log
   ├── hermes.log
   └── ares.log

Only records explicitly sent through the common logger reach these files.
Command results and JSON data may still use stdout; raw stdout/stderr and
subprocess output are not captured. ``/var/lib/atlas/logs/runs.jsonl`` remains
the separate completed-command metadata log described below.

The destination is selected once per logger namespace, in this order:

1. An absolute ``configure_logging(path=...)`` path.
2. ``/var/log/atlas/<program>.log``, using ``ATLAS_PROGRAM_NAME`` or, when absent,
   the program name from an available execution context.
3. ``/var/log/atlas/<namespace>.log``, using the logger's first name component.

For example, ``hermes.dns`` and ``hermes.worker`` share ``hermes.log`` outside
Atlas, but use ``provisioning.log`` under ``ATLAS_PROGRAM_NAME=provisioning``.
Logger names are not rewritten. Program names used in filenames must match
``[A-Za-z0-9_][A-Za-z0-9_.-]*`` and must not contain ``..``; unsafe names raise
``ValueError`` unless an explicit path is supplied.

Set the initial level through the environment or application API::

   ATLAS_LOG_LEVEL=DEBUG atlas run dns-check

The environment is read at namespace initialization, not on every record. A
running application can change standard Logger levels through Python; Atlas
adds no level-control CLI, signal handler, IPC, or configuration watcher.

File provisioning and permissions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Provision parent directories through host configuration management. The library
never creates them, changes users, installs ACLs, or changes existing file
ownership or modes. When permitted, it creates the log file on first output
with mode at most ``0640``, further restricted by the process umask. Existing
files are opened for append without truncation. Symlinks in the file or any
parent component and non-regular destinations are rejected, including on reopen.

A root-owned ``/var/log/atlas`` with mode ``0755`` does not let an ordinary
application account create files. Choose a host policy: pre-create each file
owned by its application account, give a designated operational account control
of the log directory, or configure an absolute path in another writable area.
Atlas prescribes no production owner, shared group, ACL, or directory mode for
this flat layout. Account separation and provisioning remain operator decisions.

File failures and rotation
~~~~~~~~~~~~~~~~~~~~~~~~~~

A missing directory, denied access, unsafe destination, or file I/O failure emits
a diagnostic directly to stderr. Application execution and ordinary stderr logs
continue; file failure does not change the command exit status. The warning is
suppressed while the same file remains unavailable. Each subsequent enabled
record retries; successful output clears the failure, so a later outage can
warn again. Failed records are not buffered, replayed, or saved elsewhere.
Partial writes may leave an incomplete record after an I/O failure.

Use host-managed ``logrotate`` or equivalent for rename/create rotation. The
next record detects a different device/inode and safely opens the current path.
If the replacement has not been created, Atlas tries to create it with the same
permission rules; otherwise it warns and retries. Provision rotation ownership
and modes so the application can append to the replacement. Rotation may use
names such as ``hermes.log-20261101`` and compress older generations. Schedule,
size thresholds, compression, naming, and retention are host policy; Atlas
implements none of them.

Multiple processes may append to one file, but strict ordering and completeness
are not guaranteed. These files are not a transactional audit trail. Keep secrets
out of messages and tracebacks; there is no automatic redaction guarantee.

Standalone daemons use the same API and file rules. When systemd starts a daemon,
its journal may also collect stderr; ``atlas daemon logs`` continues to read that
journal. File retention and journald retention are separately managed by the host.

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

Cluster membership
------------------

Cluster shares host information between equal ``atlasd`` peers. UDP probes and
gossip disseminate observations; periodic bidirectional TCP exchanges reconcile
membership after lost packets and partitions. No initial host has special
privileges, and no operation requires a majority. Applications own target
selection, execution, locks, retries, and results. Cluster does not execute
commands, check application health, or supervise other services.

Setup and local control
~~~~~~~~~~~~~~~~~~~~~~~

Install ``atlas[cluster]`` in the interpreter that runs ``atlasd``. The extra
provides cryptography for the daemon; normal Atlas commands and the public
Cluster client do not import it. Provision a unique ``host.id`` in
``/etc/atlas/host.yml`` (or ``ATLAS_HOST_FILE``) on each host. Role and site come
from that profile; labels come from Cluster configuration.

Create ``/etc/atlas/cluster.yml`` with strict YAML (duplicate and unknown keys
are rejected)::

    bind: "192.0.2.10:7947"
    advertise: "atlas-a.example.test:7947"
    credentials_file: "/etc/atlas/cluster/credentials.json"
    seeds:
      - "atlas-b.example.test:7947"
    labels:
      environment: lab

``bind`` is an explicit local IP. ``advertise`` is the peer endpoint, reachable
from every other peer. TCP and UDP use the same configured port; a collision
fails startup. 7947 is a project default, not an official allocation. Endpoints
accept FQDN, IPv4, and IPv6. Use brackets for IPv6 with a port, for example
``[2001:db8::10]:7947``; bare IPv6 is a literal with the default port. SSH users,
SSH aliases, URL schemes, scoped IPv6, and wildcard advertise addresses are not
supported. DNS names are retained and resolved again on each attempt. Each TCP
address candidate gets a bounded share of the exchange deadline.

Provision the credential parent directory and daemon state/runtime directories
for the intended OS account; Atlas never grants privileges or switches users.
The first initialization creates the credential file exclusively with mode 0600.
An existing file is never overwritten. Start the daemon in the foreground::

    # Direct launch: use stable, private runtime/state directories prepared by the operator.
    export STATE_DIRECTORY=/var/lib/atlas/cluster
    export RUNTIME_DIRECTORY=/run/atlas/cluster
    export ATLAS_CLUSTER_SOCKET=/run/atlas/cluster/control.sock
    atlasd --config /etc/atlas/cluster.yml

The directories must have mode 0700 and belong to the daemon account. Without
service-manager directory variables, the Daemon SDK uses its documented XDG
state/runtime behavior. Its random temporary-runtime fallback requires an
explicit socket path in clients; use a stable runtime directory for operation.
An uninitialized daemon only accepts local initialization/join and does not
create a Cluster automatically. In another terminal, as that account::

    atlas cluster init --name homelab

Securely distribute the resulting credential file to the configured path on
other hosts, retaining owner-only access, before joining them::

    atlas cluster join atlas-a.example.test atlas-b.example.test
    atlas cluster members
    atlas cluster status
    atlas cluster leave

CLI operations connect only to the local Unix socket; they never start atlasd.
Use ``atlas cluster --socket /absolute/control.sock ...`` to select a socket.
Results are JSON on stdout; errors use ``atlas.cluster-error/v2`` on stderr and
exit 2. Ordinary Atlas execution works without Cluster configuration or atlasd.
Join tries multiple seeds within a ten-second total deadline and succeeds only
after saving membership and completing an authenticated exchange. It does not
promise propagation to all peers or inbound UDP connectivity. A failed request
may already have saved membership; inspect local status before retrying.

For systemd operation, use the existing ``atlas daemon`` declaration mechanism
with instance name ``cluster``, an absolute ``atlasd`` executable, and arguments
``["--config", "/etc/atlas/cluster.yml"]``. The generated unit supplies
``/var/lib/atlas/cluster`` and ``/run/atlas/cluster`` with private permissions.
Use an explicitly provisioned OS user and working directory. The generic daemon
manager remains responsible only for the declared process, not Cluster policy.

Lifecycle and observations
~~~~~~~~~~~~~~~~~~~~~~~~~~

SIGTERM/SIGINT stop the service without leaving. Restart advances the local
incarnation before publication and reconnects using configured seeds, saved seed
names, and known peers, including unreachable peers. No reachable seed is needed
to retain existing membership. Changes to the host profile, labels, or advertise
endpoint take effect on restart and propagate with the new incarnation.

``leave`` saves the local leave intent and a new tombstone before attempting
bounded propagation. ``propagated_to_peer=false`` means local leave completed
without an acknowledged peer exchange. Restart alone does not rejoin; explicitly
``join`` to publish an incarnation above the observed tombstone. Other peers can
still fetch the tombstone through TCP synchronization. There is no forced removal
or permanent revocation of a shared-key holder.

Records merge by incarnation, then by ``alive < suspect < unreachable < left``.
Conflicting metadata at the same ID/incarnation is rejected. A running owner
refutes suspect/unreachable with a durably increased incarnation. Old acks and
old alive gossip never roll back suspicion or leave. Restored alive peers are
reported as local ``unknown`` until a correlated UDP probe confirms them.

Probes run every two seconds with one-second direct and indirect waits, up to
three helpers, and a ten-second suspicion grace. Repeated suspicion does not
extend that grace. Gossip runs every half second to up to three peers; TCP sync
tries a rotating reconnect candidate every ten seconds. DNS/send failures do not
delete records. CPU stalls or network delay can cause false suspicions. These
are conservative failure-detection heuristics, not proof of a crash or liveness.
TCP reachability alone does not count as a successful UDP probe.

Members are local observations with a retrieval timestamp, not globally fresh
inventory. Status reports only local membership and the last peer communication
in this boot. Membership status gives no execution authorization or resource
ownership. A healthy Unix socket says nothing about remote application health.

Security, limits, and recovery
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Restrict both TCP and UDP to the management network in the host/network firewall.
Every peer must be mutually reachable. NAT traversal, proxies, mDNS, DNS SRV,
automatic interface selection, and multiple Clusters per daemon are unsupported.

Credentials contain a random Cluster ID and a 256-bit shared key. AES-GCM protects
both transports; version, Cluster ID, and message type are authenticated. Every
envelope uses an independent 256-bit OS-random nonce, with negligible collision
probability across senders and restarts. TCP uses a fresh server challenge and
request ID; UDP acks match a pending random probe ID and target incarnation.
Old gossip may be replayed but is merged monotonically, never counted as fresh
communication. Encryption alone does not establish freshness or node identity.
All key holders and the executing OS account are trusted. Metadata is descriptive,
not authentication evidence. A compromised participant is outside this boundary.

The daemon accepts at most 16 concurrent TCP exchanges, each with a three-second
deadline and a 1 MiB frame limit. UDP datagrams are at most 1,200 bytes, with a
256-packet receive queue and at most 16 indirect-probe tasks. Each member record
is at most 700 JSON bytes, including at most 16 labels. The gossip queue holds
only the latest changed record per ID. Larger exchanges use TCP, with at most
256 records including tombstones. There is no fragmentation, automatic tombstone
GC, or silently truncated snapshot. Capacity exhaustion requires an explicit
operator decision to reconfigure; leave does not recover a record slot.

State is an owner-only ``membership.json`` under the Daemon SDK state directory.
A local process lock prevents two daemons from sharing it. Writes fsync the
file and parent directory around atomic replacement. Storage failure blocks
further publication; repair storage and restart. State contains membership,
latest records/tombstones, local incarnation/leave intent, credential fingerprint,
and seed names. It contains neither execution output nor probe history.

Stop the daemon before backing up state. Reuse the directory only for the same
host identity and credential. Do not clone one host's ID/state onto simultaneously
running hosts. Do not restore old snapshots while another copy is active; old
incarnations can collide with already distributed records. Corrupt or mismatched
state is rejected, not reset. Credential mismatch also rejects startup. Key
changes require a planned stop and explicit reconfiguration of all peers; there
is no online rotation or key distribution protocol.

Migration from SSH Registry
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The SSH Registry, ``_registry``, ``--via``, ``user@host``, ``--cached``,
``--directory``, ``join --abort``, ``leave --member``, revision/registration cache,
and the old SDK are removed. Existing external callers must adopt the local IPC
API in :doc:`api`; the old ``address``, ``stale``, and ``registry_reachable`` fields
have no aliases. Reconfigure every host with endpoints, credentials, and atlasd.

Old ``atlas.membership/v1`` state is rejected with
``legacy_state_requires_reconfiguration``. Stop the old tooling and explicitly
archive its state before preparing an empty state directory. Atlas does not
migrate, overwrite, or delete old membership automatically.
