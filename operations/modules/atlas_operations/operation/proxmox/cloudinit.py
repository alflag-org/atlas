from __future__ import annotations


def ipconfig0(spec: dict) -> str:
    network = spec["network"]
    value = f"ip={network['ip']}/{network['prefix']},gw={network['gateway']}"
    return value


def nameserver(spec: dict) -> str | None:
    servers = spec.get("network", {}).get("dnsServers") or []
    if not servers:
        return None
    return " ".join(str(server) for server in servers)
