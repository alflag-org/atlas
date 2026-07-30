Python API
==========

Stable release API
------------------

Release code may import ``atlas_core``. It exposes the host profile, final Atlas paths, current
artifact identity, and run correlation identifiers.

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

.. code-block:: python

   from atlas_core import get_context

   context = get_context()
   print(context.host.name)
   print(context.artifact.name)
   print(context.artifact.artifact_type)
   print(context.artifact.operation_id)

Host-side implementation
------------------------

Modules under ``atlas`` are implementation details and must not be imported by release code.

.. currentmodule:: atlas

.. autosummary::
   :toctree: generated
   :recursive:

   catalog
   cli
   config
   errors
   execution
   files
   job_instances
   jobs
   launchers
   locks
   manifests
   paths
   releases
   runtime
   sources
   yamlutil
