Documentation hosting
=====================

Sphinx builds static HTML into ``build/html``. ``wrangler.jsonc`` deploys that directory as
Cloudflare Workers static assets for the ``atlas-docs`` Worker.

.. code-block:: bash

   make html SPHINXOPTS=-W
   npx wrangler dev
   npx wrangler deploy

Workers Builds uses the repository root, ``make html`` as the build command, and
``npx wrangler deploy`` as the deploy command. The production branch is ``master``.
