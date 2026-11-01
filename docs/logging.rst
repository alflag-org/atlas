Logging and output
==================

Atlas has two distinct logging contracts: a completed-run record written by
Atlas itself, and application diagnostics emitted by registered programs. They
must not be combined. The shared logging helpers are optional and never require
a daemon, collector, or logging service.

Output destinations
-------------------

.. list-table:: Output ownership
   :header-rows: 1

   * - Output
     - Default destination
     - Owner
   * - CLI command result and machine-readable data
     - Standard output (stdout)
     - Program
   * - CLI progress, warnings, errors and diagnostic logs
     - Standard error (stderr)
     - Program
   * - Foreground daemon diagnostic logs
     - stderr, collected by journald when launched by systemd
     - Daemon/service manager
   * - Atlas completed-run metadata
     - /var/lib/atlas/logs/runs.jsonl
     - Atlas Executor

Do not mix log messages into stdout, because command results may be consumed by
shell pipelines or another program. Atlas passes through child stdout and stderr
without copying either to the completed-run log. It does not write separate
application log files or create /var/log/atlas. Operators who want persistent
application output must configure their supervisor, logging pipeline or explicit
shell redirection. Daemons run directly without the Atlas Executor do not write
Atlas completed-run records.

Run records
-----------

One JSON object is appended after each Executor invocation. It contains a UTC
timestamp, host, OS user, program, command, run_id, parent_run_id,
operation_id, working directory, exit code, execution duration, and timeout
status. It deliberately omits command arguments, stdout and stderr. The
log is created as an owner-only file (mode 0600) by the invoking OS user.

Atlas does not yet rotate or expire runs.jsonl. For long-lived hosts, arrange
file-based rotation using the host's existing logrotate or other retention policy.
The Executor opens the file separately for each append; prefer rename-based
rotation rather than copytruncate. Ensure the invoking account can create the
new file after rotation. Log retention and archival are operator policies, not an
Atlas scheduler or daemon feature.

Python application logs
-----------------------

Python programs can use atlas_core.logging at their entry point:

.. code-block:: python

   from atlas_core.logging import configure_logging, get_logger, log_event

   logger = configure_logging("hermes")
   logger.info("inventory scan started")
   log_event(
       logger,
       "inventory.scan.completed",
       message="inventory scan complete",
       fields={"hosts": 12},
   )

   child_logger = get_logger("hermes.inventory")
   child_logger.warning("one source was unavailable")

configure_logging() configures only the named stdlib logger, never the root
logger. It replaces only its own handler on subsequent calls. If an application
attaches other handlers to that logger, those handlers remain its responsibility.
The default is level INFO with one-line text on stderr. Set level="DEBUG" or
format="json" explicitly when needed. The JSON option emits one JSON object per
line on stderr, not stdout. The stream argument can supply an alternative text
stream for tests or an application-specific logging pipeline; the helper does
not open any log file.

log_event() accepts a stable event name and optional scalar fields. Ordinary
logger.info(...), logger.warning(...), and logger.error(...) continue to work.
The helper only emits records through the configured stdlib logger; it does not
alter execution history.

The JSON format is atlas.application-log/v1:

.. code-block:: json

   {
     "schema": "atlas.application-log/v1",
     "timestamp": "2026-11-01T01:00:00.000Z",
     "level": "INFO",
     "logger": "hermes",
     "message": "inventory scan complete",
     "event": "inventory.scan.completed",
     "fields": {"hosts": 12},
     "host_id": "control01",
     "run_id": "run-example",
     "operation_id": "run-example"
   }

The context keys host_id, program, command, run_id, parent_run_id and
operation_id are included only when the corresponding ATLAS_* variables exist.
The Executor supplies these variables to its children. A standalone daemon
normally has no run IDs, which must not be fabricated. Applications should
supply their own operation identifiers when they perform work outside atlas run.

Secrets and failures
--------------------

Neither Atlas nor atlas_core.logging performs automatic message redaction.
Do not log secret values, tokens, environment dumps, sensitive request bodies,
or exception messages that might contain secrets. logger.exception() includes
tracebacks and exception messages and must be used only when their contents are
safe to expose. Prefer concise error categories for failures involving secrets.
There is no general-purpose log-upload, log-search, rotation, retention, or
secret-scrubbing service in Atlas.

Daemon output
-------------

A foreground daemon should send diagnostics to stderr using the same logging
conventions. When operated under systemd, stderr is directed to journald, and
logs can be viewed with journalctl -u UNIT. journald storage and retention
depend on systemd configuration; Atlas does not override them. Readiness events,
service status, process output and Atlas completed-run history are separate
concepts. Use the process supervisor for daemon lifecycle and logs.

.. automodule:: atlas_core.logging
   :members: configure_logging, get_logger, log_event
