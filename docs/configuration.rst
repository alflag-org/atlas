Configuration
=============

Host configuration
------------------

Atlas reads ``/etc/atlas/config.yml``. The schema is strict and rejects unknown keys.

.. code-block:: yaml

   runtime:
     python:
       version: "3.14.6"

   releases:
     operations:
       source: "/srv/releases/operations"
       enabled: true

     maintenance:
       source: "https://example.test/maintenance-1.2.0.tar.gz"
       enabled: false

``atlas release update`` updates enabled entries. An explicit
``atlas release update maintenance`` updates that entry even when disabled.

Release sources
---------------

Supported source forms are:

.. list-table::
   :header-rows: 1

   * - Form
     - Example
   * - Local directory
     - ``/srv/releases/operations``
   * - File URL
     - ``file:///srv/releases/operations``
   * - Local archive
     - ``operations-1.0.0.tar.gz``
   * - HTTP(S) archive
     - ``https://example.test/operations-1.0.0.tar.gz``
   * - Git release source
     - ``git+https://github.com/example/operations.git#v1.0.0``

Git source handling applies only to Atlas release acquisition. Atlas never changes Git state in
the infrastructure repository used as an artifact working directory.

Host profile
------------

``/etc/atlas/host.yml`` must contain a non-empty ``name``. Optional string fields are ``site``,
``zone``, ``role``, ``environment``, and ``runtime_kind``. ``tags`` is a list of strings.

.. code-block:: yaml

   name: control-01
   site: kng01
   zone: management
   role: control
   environment: production
   runtime_kind: vm
   tags:
     - trusted

Host-side path variables
------------------------

``ATLAS_HOME``
   Defaults to ``/opt/atlas``.

``ATLAS_ETC_DIR``
   Defaults to ``/etc/atlas``.

``ATLAS_VAR_DIR``
   Defaults to ``/var/lib/atlas``.

``ATLAS_RUNTIME_DIR``
   Defaults to ``$ATLAS_HOME/runtime``.

``ATLAS_TMP_DIR``
   Defaults to ``$ATLAS_HOME/tmp``.

The release and current roots are fixed relative to ``ATLAS_HOME``. Former
``ATLAS_SCRIPTS_*`` variables are not read.

Artifact execution variables
----------------------------

Atlas supplies the following values to each command or job:

``ATLAS_RELEASE_NAME``
   Canonical manifest release name.

``ATLAS_RELEASE_VERSION``
   Value read from the release ``VERSION`` file.

``ATLAS_ARTIFACT_TYPE``
   ``command`` or ``job``.

``ATLAS_ARTIFACT_NAME``
   Manifest artifact identifier.

``ATLAS_RELEASE_ROOT``
   Installed release directory used by this execution.

``ATLAS_HOST_FILE``
   Resolved host profile path.

``ATLAS_RUN_ID``, ``ATLAS_PARENT_RUN_ID``, ``ATLAS_OPERATION_ID``
   Correlation identifiers for nested execution.

These variables are generated for child execution. Do not set ``ATLAS_RELEASE_ROOT`` as a
host-side path override.
