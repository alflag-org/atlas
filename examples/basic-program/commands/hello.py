from __future__ import annotations

import json
import sys

from atlas_core import get_context


context = get_context()
print(
    json.dumps(
        {
            "host": context.host.id,
            "program": context.program.name,
            "command": context.command.name,
            "run_id": context.execution.run_id,
            "arguments": sys.argv[1:],
        },
        sort_keys=True,
    )
)
