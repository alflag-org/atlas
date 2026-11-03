Python API
==========

Programs written in Python can read the current execution context without importing Atlas's host
implementation:

.. code-block:: python

   from atlas_core import get_context

   context = get_context()
   print(context.host.id)
   print(context.program.root)
   print(context.command.name)
   print(context.execution.operation_id)

The same context is available to non-Python programs as the JSON document named by
``ATLAS_CONTEXT_FILE``. The public package provides host identity, standard paths, execution context,
volatile execution storage, and external secret retrieval.

.. automodule:: atlas_core
   :members:
   :undoc-members:

.. automodule:: atlas_core.context
   :members:
   :undoc-members:

.. automodule:: atlas_core.host
   :members:
   :undoc-members:

.. automodule:: atlas_core.paths
   :members:
   :undoc-members:

Secrets
-------

.. automodule:: atlas_core.secrets
   :members:

Execution storage
-----------------

.. automodule:: atlas_core.execution
   :members:

.. _application-logging-api:

Application logging
-------------------

``atlas_core.logging`` provides ordinary shared ``logging.Logger`` objects for
scripts, CLI commands, and standalone daemons. No Atlas configuration or service
manager is required:

.. code-block:: python

   from atlas_core.logging import get_logger

   logger = get_logger("hermes.dns")
   logger.info("DNS validation completed")

The first retrieval configures the top-level namespace (``hermes``) with file
and stderr handlers. The file is opened only on the first enabled record, not
on import, configuration, retrieval, or filtered DEBUG calls. Descendants share
the file and retain their full logger names. Logger names consist of dot-separated
components matching ``[A-Za-z_][A-Za-z0-9_-]*``; the ``root`` namespace is rejected.

Configure the namespace at the application's entry point when defaults are not
suitable:

.. code-block:: python

   from atlas_core.logging import configure_logging, get_logger

   configure_logging(
       "hermes", level="DEBUG", path="/var/log/atlas/hermes.worker01.log",
   )
   logger = get_logger("hermes.worker")
   logger.debug("Worker initialized")

``configure_logging()`` accepts a top-level name only. Initially, level precedence
is the explicit argument, then ``ATLAS_LOG_LEVEL``, then INFO. Names are
case-insensitive; the accepted levels are DEBUG, INFO, WARNING, ERROR, and
CRITICAL, or their integer values 10, 20, 30, 40, and 50. NOTSET is reserved for
standard child inheritance, not accepted as a configuration level. An invalid
environment value warns once at namespace initialization and selects INFO.
Invalid API arguments raise ``TypeError`` or ``ValueError`` before settings change.

Once initialized, omitted or ``None`` arguments retain the current settings.
Repeated retrieval and configuration do not duplicate handlers or reset levels.
Changing the path closes the current file under the Atlas file handler lock.
The handler stays attached so in-flight records are not dropped during the switch.
Custom handlers remain installed. Use an absolute path without parent traversal or
control characters; see :ref:`application-logging-operations` for destination
precedence, permissions, rotation, and failure recovery.

Levels can be changed using the standard API:

.. code-block:: python

   import logging

   parent = get_logger("hermes")
   previous = parent.level
   try:
       parent.setLevel(logging.DEBUG)
       logger.debug("Detailed diagnostic")
   finally:
       parent.setLevel(previous)

Children inherit unless given an explicit level with ``setLevel()``. Atlas
handlers use NOTSET, so a child's explicit DEBUG level reaches both destinations
even when its parent is INFO. Handlers live only on the top-level logger, whose
``propagate`` is False. The root logger and unrelated loggers are unchanged.
Normal standard-library ``isEnabledFor()``, ``exception()``, ``extra`` fields,
filters, and custom formatters remain available. The default formatter does not
serialize arbitrary ``extra`` fields. Caller-added handlers can introduce their
own output and duplication.

Both destinations use UTF-8 text, a UTC timestamp with milliseconds and a trailing
``Z``, the level, full logger name, and message::

   2026-11-01T12:30:00.123Z INFO hermes.dns DNS validation completed

At namespace initialization, an available ``ATLAS_CONTEXT_FILE`` supplies
``host_id``, ``run_id``, and ``operation_id``. If it is absent or unreadable,
available ``ATLAS_HOST_ID``, ``ATLAS_RUN_ID``, and ``ATLAS_OPERATION_ID`` values
are used. Logging never loads standalone ``host.yml`` or generates identifiers.
Context values are captured once for the namespace and precede the message::

   2026-11-01T12:30:00.123Z INFO hermes.dns host_id=control01 run_id=run-1 operation_id=op-1 DNS validation completed

``logger.exception()`` includes the standard traceback; ``stack_info=True``
includes a stack trace. Neither messages nor exception details are automatically
redacted. Do not pass tokens, environment dumps, or sensitive responses to a
logger. Application logging never captures stdout/stderr or changes completed-run
history. Initialization is serialized within the process; writes from multiple
processes to one file are best effort, without ordering or completeness guarantees.
There is no logging server, replay buffer, or interprocess lock.

.. automodule:: atlas_core.logging
   :members: get_logger, configure_logging

Independent daemons
-------------------

Use ``atlas_core.daemon`` to run an async application in the foreground. Install
Atlas in the application's Python environment, then call ``run()`` from its main
thread, outside an existing event loop. No Atlas host configuration is needed.

.. literalinclude:: ../examples/daemon/sampled.py
   :language: python
   :start-at: from atlas_core.daemon

Run the example from a repository checkout::

    cd examples/daemon
    python -m sampled

Call ``ctx.ready()`` after required resources and workers have initialized.
Keep concurrent work in an ``asyncio.TaskGroup`` so worker failures reach ``main``.
Detached task failures may go unnoticed until garbage collection. See
:ref:`daemon-environment` for directories and systemd readiness notifications.

SIGTERM or SIGINT sets ``ctx.stopping`` and unblocks ``ctx.wait_for_stop()``.
The application can also set this event to stop itself. Each worker must observe
the event and finish; a TaskGroup waits for its workers on normal exit. Release
resources in ``finally`` blocks. ``run()`` restores the previous signal handlers
when it exits.

The return codes are:

* ``0``: the application signalled readiness and finished cleanly, including
  graceful signal handling.
* ``1``: missing readiness, an application or event-loop failure, cancellation,
  failed cleanup, or a shutdown timeout.
* ``2``: invalid options or directory setup/cleanup failure.

The shutdown grace period defaults to 30 seconds. After it expires, the SDK
cancels outstanding tasks. Cancellation and async-generator cleanup each get
another ``min(shutdown_timeout, 1)`` seconds. Repeated signals do not reset the
deadline. The SDK has no startup timer.

These deadlines require a responsive event loop. The application must stop its
own threads, subprocesses, and executors. Configure the service manager's hard
stop deadline for code that may block or ignore cancellation.

``ctx.logger`` is ``atlas_core.logging.get_logger(ctx.name)`` and uses the shared
file and stderr configuration described above. Configure that name before
``run()`` to select a level or instance-specific path. The logger stays usable
after ``run()`` returns; shutdown does not disable it or remove shared handlers.
It leaves the root logger unchanged and writes no Atlas execution records.
Configuration failures before the application logger is available are reported
directly to stderr, including failures in logging configuration itself.
SDK diagnostics include exception class names, but omit exception messages,
tracebacks, arguments, and environment values. Applications must keep secrets
out of their own messages and handlers.

.. automodule:: atlas_core.daemon
   :members: DaemonContext, DaemonMain, run

Local service APIs
------------------

``atlas_core.ipc`` provides unary JSON requests over private Linux Unix sockets.
An application owns its protocol version, handler, client models, and errors.
``serve()`` takes an explicit authorization callback and checks Linux peer
credentials on every request. Messages are limited to 1 MiB; the client and
server impose request deadlines. Transport failures are not retried.

The independent example in ``examples/service/sample_service`` publishes
``sample_service.api.SampleClient`` without registering an Atlas plugin::

    cd examples/service
    python -m sample_service

Use an explicit ``RUNTIME_DIRECTORY`` to select a stable socket path. Import
``SampleClient`` from another Python program and call its async ``status()``
method. The example's protocol and models belong to that package, not Atlas.

.. automodule:: atlas_core.ipc
   :members: Client, Peer, IPCError, Unavailable, serve

Cluster membership
------------------

Use the local ``atlasd`` API to read host observations. Applications own target
selection, connection failures, execution, locks, retries, and results::

    from atlas_core.cluster import ClusterClient, MemberState

    async def targets():
        async with ClusterClient.connect() as cluster:
            snapshot = await cluster.members()
        return [m for m in snapshot.members
                if m.role == "app" and m.state is MemberState.ALIVE]

``ClusterClient.connect(path=None, timeout=15)`` checks the local daemon with a
status request. Each method uses one bounded Unix-socket request. It starts no
service, performs no peer networking, and imports no cryptography or private
Cluster implementation. ``ATLAS_CLUSTER_SOCKET`` selects an explicit path;
otherwise the client uses ``$XDG_RUNTIME_DIR/atlas/cluster/control.sock`` or
``/run/atlas/cluster/control.sock``. Run it as the daemon's OS account.

``members()`` returns ``ClusterMembers.members`` (a tuple), ``fetched_at`` (UTC),
and ``observation="local"``. Every ``ClusterMember`` contains ``id``, ``hostname``,
``endpoint`` (``PeerEndpoint.host`` and ``port``), ``role``, ``site``, ``labels``,
``incarnation``, and ``state``. The endpoint is for Cluster TCP/UDP, not SSH.
``MemberState`` is ``ALIVE``, ``SUSPECT``, ``UNREACHABLE``, ``LEFT``, or the local
``UNKNOWN`` used for restored alive peers until a correlated UDP probe succeeds.
An alive observation is neither application health nor a promise that a future
connection will succeed. Incarnations order membership, not resource ownership.

``status()`` returns ``local_id``, ``cluster_id``, ``name``, ``membership``
(``uninitialized``, ``joined``, or ``left``), and ``last_peer_contact``. The last
contact is a successful correlated probe or authenticated TCP exchange during
this boot; it is null after restart until such contact occurs. TCP contact does
not certify UDP reachability. There is no global freshness flag.

``request(method, params=None)`` exposes the same local control operations as the
CLI: ``init`` with ``{"name": "lab"}``, ``join`` with ``{"seeds": ["peer.example.test"]}``,
and ``leave`` without parameters. Join returns ``exchange_completed`` and
``scope="local_and_seed"``. Leave returns ``propagated_to_peer``, which only means
that at least one peer acknowledged an exchange. Neither implies convergence.

``ClusterUnavailable`` has code ``daemon_unavailable``. There is no saved-file
fallback. ``ClusterError.code`` distinguishes ``not_joined``, ``invalid_state``,
``legacy_state_requires_reconfiguration``, ``member_conflict``,
``membership_too_large``, ``cluster_mismatch``, ``credential_mismatch``,
``authentication_or_format_failed``, ``peer_unavailable``, ``join_timeout``, and
``persistence_failed``. A failed or cancelled control request may have committed;
inspect status before retrying. See :doc:`reference` for setup and migration.

.. automodule:: atlas_core.cluster
   :members: ClusterClient, MemberState, PeerEndpoint, ClusterMember, ClusterMembers, ClusterStatus, ClusterError, ClusterUnavailable
