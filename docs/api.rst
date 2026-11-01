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

``ctx.logger`` writes INFO and higher levels to stderr during ``run()``. It uses
standard ``logging`` methods and accepts ``extra`` fields for custom formatters.
It leaves the root logger unchanged and writes no Atlas execution records.
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

Use ``atlas_core.cluster`` to discover hosts. Applications choose targets and
implement any execution, locking, retry, or rollback policy::

    from atlas_core.cluster import ClusterClient

    async def targets():
        async with ClusterClient.connect() as cluster:
            members = await cluster.members()
        if members.stale:
            raise RuntimeError("fresh membership required")
        return [member for member in members if member.labels.get("role") == "app"]

``ClusterMember`` contains ``id``, ``hostname``, ``address``, ``role``, ``site``,
and ``labels``. It describes a host; it has no execution methods or permissions.
``id``, ``role``, and ``site`` come from the existing host profile at registration.
Labels and the advertised SSH address are supplied when initializing or joining.

``members()`` returns an iterable ``ClusterMembers`` with ``stale`` and
``fetched_at`` (an ISO 8601 UTC timestamp). A remote read normally contacts the
registry. If SSH fails, it returns the last saved snapshot with ``stale=True``.
``members(cached=True)`` reads only local state. Invalid responses or a changed
cluster identity or a lower registry revision raise an error rather than silently using cached data.

``status()`` returns membership information, including ``joined`` and
``registry_reachable``: true after a successful read, false after a connection
failure, and ``None`` when no remote check was requested. A local registry is
always authoritative. ``joined`` and other fields may be stale; they do not
assert that a host or application is healthy. A pending registration without a
cached snapshot has no cluster ID or fetch timestamp while offline.

``ClusterClient.connect(directory=None, timeout=10)`` does not start a service or
open a persistent connection. Each read uses a short local file transaction and,
when needed, one SSH process. Cancellation terminates that SSH process group.
Use the same OS account and state directory as the CLI. The default directory is
``$XDG_STATE_HOME/atlas/cluster`` or ``~/.local/state/atlas/cluster``.

``ClusterError.code`` includes ``not_joined``, ``invalid_state``,
``cluster_changed``, ``registry_revision_regressed``, and ``member_conflict``. ``ClusterUnavailable`` reports
``registry_unavailable`` or ``no_cached_members``. There are no automatic retries.
See :doc:`reference` for SSH setup and membership changes.

.. automodule:: atlas_core.cluster
   :members: ClusterClient, ClusterMember, ClusterMembers, ClusterStatus, ClusterError, ClusterUnavailable
