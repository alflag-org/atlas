API Reference
=============

Stable script runtime API
-------------------------

``atlas_core`` is the stable runtime library for installed scripts.
Scripts should import from this package rather than from ``atlas`` internals.

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

Internal Atlas modules
----------------------

The ``atlas`` package implements the command-line tool and host-side release management.
These modules are documented for maintainers, but they are not the public script API.

.. currentmodule:: atlas

.. autosummary::
   :toctree: generated
   :recursive:

   cli
   commands
   config
   files
   launchers
   paths
   releases
   runner
   runtime
   scriptsets
   sources
   yamlutil
