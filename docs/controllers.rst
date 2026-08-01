Operation controllers
=====================

First-party operation commands are grouped by domain. Install both releases, rebuild the shared
runtime, and regenerate shims:

.. code-block:: console

   $ atlas release install ./configuration-operations
   $ atlas release install ./infrastructure-operations
   $ atlas runtime install
   $ atlas release shims
   $ atlas command list
   configctl
   hostctl
   imagectl
   providerctl
   operationctl

Atlas creates shims for those five commands. Configuration, provider, and lifecycle phase jobs are
visible through ``atlas job list``, but they have no PATH entry.

Execution boundary
------------------

Each controller has one explicit subcommand parser. It invokes a private job through
``atlas job run`` or, for ``configctl diff-many``, invokes ``configctl diff`` as another public
process. Controllers do not import operation implementation functions.

Child processes receive an argument list with ``shell=False``. They inherit the caller's working
directory, environment, stdin, stdout, and stderr. Atlas records the child run with the same
operation correlation ID and the controller run as its parent.

stdout is reserved for results. JSON artifacts, diffs, and inventory can be redirected or piped.
Diagnostics and progress use stderr. A child exit status is returned unchanged; a missing child
returns 127.

Domain boundaries
-----------------

``configctl`` works on the configuration project in the current directory. ``hostctl`` owns the
durable managed-host lifecycle. ``imagectl`` owns machine-image artifacts. ``providerctl`` is
read-only. ``operationctl`` reads operation artifacts and durable Registry state without mutating a
provider or resource.

No controller accepts an arbitrary executable name, Python attribute, shell command string, or
provider mutation outside its documented verbs.
