from __future__ import annotations

import json

from atlas_core import get_context


print(json.dumps(get_context().to_dict(), default=str, sort_keys=True))
