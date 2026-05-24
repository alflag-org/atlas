Development
===========

Use mise for the standard local workflow:

.. code-block:: bash

   mise install
   mise run setup
   mise run check

Documentation
-------------

Build the Sphinx HTML documentation with:

.. code-block:: bash

   mise run docs

The generated HTML is written to ``docs/_build/html``.
When editing API documentation, prefer keeping public script-facing behavior in ``atlas_core``
and treating ``atlas`` modules as maintainer-facing internals.
