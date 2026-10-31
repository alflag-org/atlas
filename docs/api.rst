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

``atlas_core.daemon`` runs an application's async entry point in its own foreground
process. It needs the Atlas Python distribution, but no Atlas CLI invocation,
execution context, host configuration, service manager, or Cluster dependency.
Call ``run`` from the executable's main thread, outside an existing event loop.

.. literalinclude:: ../examples/daemon/hoged.py
   :language: python
   :start-at: from atlas_core.daemon

After installing Atlas in the application's interpreter, run the example from a
repository checkout::

    cd examples/daemon
    python -m hoged

Interrupt with Ctrl-C, or send SIGTERM to the foreground process. Initialization
must finish before ``ctx.ready()``. Use ``asyncio.TaskGroup`` inside ``main`` for
required concurrent work; a task failure propagates to ``main`` and makes the
service fail. Workers should observe ``ctx.stopping`` and release their resources
in ``finally`` blocks. Keep required tasks awaited: a detached task whose failure
is never retrieved can escape observation until garbage collection. Unhandled
loop callbacks and reported task exceptions fail the run without logging their
potentially secret-bearing contents.

.. code-block:: python

    import asyncio

    async def main(ctx):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(worker(ctx))
            ctx.ready()  # All required initialization must already have succeeded.
            await ctx.wait_for_stop()

A TaskGroup does not stop workers on normal exit: each worker must finish when
stopping is set. If worker initialization is asynchronous, explicitly await its
startup result before signalling readiness. ``ready()`` is idempotent; the first
call after a stop request fails. It records readiness locally without systemd,
or sends ``READY=1`` to a configured Unix datagram ``NOTIFY_SOCKET`` (filesystem
or Linux abstract address). A configured socket that cannot accept the datagram
fails initialization. Delivery is not an acknowledgement of manager acceptance.
Readiness is distinct from health; this SDK supplies no health or watchdog loop.

``run()`` owns its event loop and temporarily installs SIGTERM/SIGINT handlers,
restoring the original handlers on exit. Either signal sets ``ctx.stopping``;
applications can also set that event to request their own graceful stop. Repeated
signals do not extend the shutdown deadline. The return contract is:

* ``0``: readiness was signalled and the application finished cleanly, including
  a graceful SIGTERM or SIGINT.
* ``1``: missing readiness, initialization/application failure, failed cleanup,
  cancellation, unhandled event-loop failure, or shutdown timeout.
* ``2``: invalid options or directory setup/cleanup failure.

Ordinary application exceptions are diagnosed by class name only. No exception
message, traceback, arguments, or environment values are logged automatically.
Application log messages and custom handlers remain the application's responsibility.
``ctx.logger`` is a per-run INFO logger with a stderr handler; it accepts normal
``logging`` methods and ``extra`` fields for application-installed formatters.
It does not modify the root logger or write Atlas JSONL execution records.
``SystemExit`` and other ``BaseException`` subclasses propagate after cleanup.
Misuse from another thread or an active event loop raises ``RuntimeError``.

``shutdown_timeout`` defaults to 30 seconds and must be finite and positive.
After the cooperative grace period, outstanding tasks are cancelled. Task
cancellation and async-generator finalization each have an additional budget of
``min(shutdown_timeout, 1)`` seconds. Cleanup failure or expiration is nonzero,
even if the application suppresses cancellation. A blocked event loop cannot
enforce these deadlines. Applications must own and stop their threads,
subprocesses, executors, and external resources; the SDK is not a process-tree
supervisor. Use an OS service manager's hard stop deadline for uncooperative code.
There is no startup timer in the SDK; a service manager may enforce one.

.. automodule:: atlas_core.daemon
   :members: DaemonContext, DaemonMain, run
