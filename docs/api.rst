Python API
==========

Release API
-----------

Release code can import ``atlas_core`` for the host profile, resolved Atlas paths, artifact
identity, and run correlation identifiers.

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
