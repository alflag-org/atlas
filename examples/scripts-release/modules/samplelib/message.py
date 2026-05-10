from __future__ import annotations


def build_message(name: str, host: str, script: str) -> str:
    return f"[sample] hello {name} from {host} via {script}"
