Configuration
=============

Atlas reads host-level configuration from ``/etc/atlas`` by default.
Runtime state and installed assets are kept under ``/opt/atlas`` and ``/var/lib/atlas``.

Main paths
----------

* ``/etc/atlas/config.yml``: runtime and scripts configuration
* ``/etc/atlas/host.yml``: host metadata exposed to installed scripts
* ``/opt/atlas``: runtime, shims, launchers, and installed script releases
* ``/var/lib/atlas``: logs, cache, and runtime state

Runtime configuration
---------------------

``runtime.python.version`` controls the Python version used for the scripts runtime.

.. code-block:: yaml

   runtime:
     python:
       version: "3.12.3"

Scripts configuration
---------------------

Atlas supports multiple configured releases.
Registry aliases are resolved from local configuration rather than built-in Atlas logic.

.. code-block:: yaml

   runtime:
     python:
       version: "3.12.3"

   scripts:
     releases:
       common:
         source: common
       kitsunebi:
         source: kitsunebi

     registries:
       common:
         source: "git+https://github.com/example/common-scripts.git#v0.1.0"
       kitsunebi:
         source: "git+https://github.com/example/kitsunebi-scripts.git#v0.1.0"

Host profile
------------

``host.yml`` must be a YAML mapping with a non-empty string ``name``.
Optional ``site``, ``zone``, ``role``, ``environment``, and ``runtime_kind`` values must be
strings when present. ``tags`` may be absent, null, or a list of strings.

.. code-block:: yaml

   name: worker-01
   site: nrt
   environment: production
   tags:
     - batch
     - trusted
