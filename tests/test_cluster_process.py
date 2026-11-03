"""Real TCP/UDP/Unix-socket processes, including an actual packet-drop partition."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from atlas_core.cluster import (
    ClusterClient,
    ClusterError,
    ClusterUnavailable,
    MemberState,
)

ROOT = Path(__file__).resolve().parents[1]


def free_port(host):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as stream:
        stream.bind((host, 0))
        return stream.getsockname()[1]


class Gateway(asyncio.DatagramProtocol):
    """An endpoint proxy, never a mock of membership or transport results."""

    def __init__(self, host, destination):
        self.host, self.destination = host, destination
        self.blocked = False
        self.drop_sources = set()
        self.tasks = set()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        if not self.blocked and address[1] not in self.drop_sources:
            self.transport.sendto(data, (self.host, self.destination))

    async def forward(self, reader, writer):
        self.tasks.add(asyncio.current_task())
        upstream = None
        try:
            if self.blocked:
                return
            remote, upstream = await asyncio.open_connection(
                self.host, self.destination
            )

            async def copy(source, target):
                while data := await source.read(65536):
                    if self.blocked:
                        return
                    target.write(data)
                    await target.drain()
                target.close()

            async with asyncio.TaskGroup() as group:
                group.create_task(copy(reader, upstream))
                group.create_task(copy(remote, writer))
        except (OSError, ExceptionGroup):
            pass  # Deliberately cut connections are part of the partition harness.
        finally:
            writer.close()
            if upstream:
                upstream.close()
            self.tasks.discard(asyncio.current_task())


class Host:
    def __init__(self, directory, name, host="127.0.0.1", advertise_host=None):
        self.directory = directory / name
        self.directory.mkdir(mode=0o700)
        self.state = self.directory / "state"
        self.runtime = self.directory / "run"
        self.state.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        self.host = host
        self.port, self.gateway_port = free_port(host), free_port(host)
        self.gateway = Gateway(host, self.port)
        self.endpoint = (
            f"[{advertise_host or host}]:{self.gateway_port}"
            if ":" in host
            else f"{advertise_host or host}:{self.gateway_port}"
        )
        self.credentials = self.directory / "credentials.json"
        self.config = self.directory / "cluster.yml"
        bind = f"[{host}]:{self.port}" if ":" in host else f"{host}:{self.port}"
        self.config.write_text(
            json.dumps(
                {
                    "bind": bind,
                    "advertise": self.endpoint,
                    "credentials_file": str(self.credentials),
                }
            )
        )
        (self.directory / "host.yml").write_text(
            f"version: 1\nhost:\n  id: {name}\n  role: app\n"
        )
        self.client = ClusterClient(self.runtime / "control.sock", timeout=6)
        self.process = None
        self.output = (self.directory / "output").open("w+")

    async def start(self, *, entry=False):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ATLAS_")
            and key not in {"NOTIFY_SOCKET", "STATE_DIRECTORY", "RUNTIME_DIRECTORY"}
        }
        env.update(
            STATE_DIRECTORY=str(self.state),
            RUNTIME_DIRECTORY=str(self.runtime),
            ATLAS_HOST_FILE=str(self.directory / "host.yml"),
            PYTHONPATH=str(ROOT / "src"),
        )
        if entry:
            env["ATLAS_TEST_ENTRY"] = "1"
        from coverage import Coverage

        if Coverage.current() is not None:
            env["ATLAS_TEST_COVERAGE"] = str(self.directory / "coverage")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "tests/cluster_process.py"), str(self.config)],
            env=env,
            stdout=self.output,
            stderr=self.output,
        )
        for _ in range(200):
            assert self.process.poll() is None, self.logs()
            try:
                await self.client.status()
                return
            except ClusterUnavailable:
                await asyncio.sleep(0.02)
        raise AssertionError(self.logs())

    def logs(self):
        self.output.flush()
        return (self.directory / "output").read_text()

    async def stop(self, number=signal.SIGTERM):
        if self.process and self.process.poll() is None:
            self.process.send_signal(number)
            await asyncio.to_thread(self.process.wait, 5)
            assert self.process.returncode == (
                -signal.SIGKILL if number == signal.SIGKILL else 0
            ), self.logs()

        from coverage import Coverage, CoverageData

        active = Coverage.current()
        data_path = self.directory / "coverage"
        if active is not None and data_path.exists():
            data = CoverageData(basename=str(data_path))
            data.read()
            active.get_data().update(data)
            data_path.unlink()

    async def join(self, source, *seeds):
        shutil.copyfile(source.credentials, self.credentials)
        self.credentials.chmod(0o600)
        return await self.client.request(
            "join", {"seeds": list(seeds) or [source.endpoint]}
        )


@asynccontextmanager
async def hosts(tmp_path, names="abcd", host="127.0.0.1", advertise_host=None):
    nodes = []
    servers, transports = [], []
    try:
        for name in names:
            node = Host(tmp_path, name, host, advertise_host)
            nodes.append(node)
            servers.append(
                await asyncio.start_server(
                    node.gateway.forward, host, node.gateway_port
                )
            )
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda gateway=node.gateway: gateway,
                local_addr=(host, node.gateway_port),
            )
            transports.append(transport)
            await node.start()
        yield nodes
    finally:
        # Reap every process even when one shutdown assertion fails.
        stops = await asyncio.gather(
            *(node.stop() for node in nodes), return_exceptions=True
        )
        for server in servers:
            server.close()
            await server.wait_closed()
        for transport in transports:
            transport.close()
        for node in nodes:
            for task in node.gateway.tasks:
                task.cancel()
            await asyncio.gather(*node.gateway.tasks, return_exceptions=True)
            node.output.close()
        for result in stops:
            if isinstance(result, BaseException):
                raise result


async def converge(nodes, expected, *, timeout=12):
    async with asyncio.timeout(timeout):
        while True:
            snapshots = [await node.client.members() for node in nodes]
            if all(
                {m.id: m.state for m in snapshot.members} == expected
                for snapshot in snapshots
            ):
                return snapshots
            await asyncio.sleep(0.05)


def test_cluster_survives_seed_loss_crash_restart_partition_and_leave(tmp_path):
    async def scenario():
        async with hosts(tmp_path) as (a, b, c, d):
            assert (await a.client.status()).membership == "uninitialized"
            await a.client.request("init", {"name": "lab"})
            await b.join(a)
            await c.join(b)
            alive = dict.fromkeys("abc", MemberState.ALIVE)
            await converge([a, b, c], alive)
            cluster_id = (await a.client.status()).cluster_id
            assert {(await n.client.status()).cluster_id for n in (b, c)} == {
                cluster_id
            }

            # Real SIGKILL, persisted identity, no graceful leave on service stop.
            await a.stop(signal.SIGKILL)
            await c.stop()
            await converge(
                [b],
                alive | {"a": MemberState.UNREACHABLE, "c": MemberState.UNREACHABLE},
            )
            result = await d.join(b, a.endpoint, b.endpoint)
            assert result["exchange_completed"] and result["scope"] == "local_and_seed"
            await converge(
                [b, d],
                alive
                | {
                    "a": MemberState.UNREACHABLE,
                    "c": MemberState.UNREACHABLE,
                    "d": MemberState.ALIVE,
                },
            )
            await a.start()
            await c.start()
            all_alive = dict.fromkeys("abcd", MemberState.ALIVE)
            await converge([a, b, c, d], all_alive)

            # Isolate one host in both directions for both protocols.
            a.gateway.blocked = True
            for node in (b, c, d):
                node.gateway.drop_sources.add(a.port)
            # TCP outbound is blocked by stopping forwarding to the other partition.
            # All endpoint gates below close, while Unix control remains available.
            for node in (b, c, d):
                node.gateway.blocked = True
            await converge(
                [a], all_alive | dict.fromkeys("bcd", MemberState.UNREACHABLE)
            )
            await converge(
                [b], all_alive | dict.fromkeys("acd", MemberState.UNREACHABLE)
            )
            # Independent metadata changes while all network endpoints are cut.
            for node in (a, c):
                await node.stop()
                config = json.loads(node.config.read_text())
                config["labels"] = {"revision": node.directory.name}
                node.config.write_text(json.dumps(config))
                await node.start()
            for node in (a, b, c, d):
                node.gateway.blocked = False
                node.gateway.drop_sources.clear()
            snapshots = await converge([a, b, c, d], all_alive, timeout=18)
            for snapshot in snapshots:
                changed = {
                    m.id: m.labels for m in snapshot.members if m.id in {"a", "c"}
                }
                assert changed == {"a": {"revision": "a"}, "c": {"revision": "c"}}

            # Persist leave even when absolutely no endpoint can receive it.
            for node in (a, b, c):
                node.gateway.blocked = True
            result = await d.client.request("leave")
            assert result == {"membership": "left", "propagated_to_peer": False}
            await d.stop()
            await d.start()
            assert (await d.client.status()).membership == "left"
            for node in (a, b, c):
                node.gateway.blocked = False
            await converge([a, b, c], all_alive | {"d": MemberState.LEFT}, timeout=18)
            await d.client.request("join", {"seeds": [b.endpoint]})
            await converge([a, b, c, d], all_alive)
            assert "key" not in d.logs()

    asyncio.run(scenario())


def test_direct_udp_loss_uses_indirect_probe(tmp_path):
    async def scenario():
        async with hosts(tmp_path, "abc") as (a, b, c):
            await a.client.request("init", {"name": "lab"})
            await b.join(a)
            await c.join(b)
            await converge([a, b, c], dict.fromkeys("abc", MemberState.ALIVE))
            b.gateway.drop_sources.add(a.port)
            a.gateway.drop_sources.add(b.port)
            # Observe longer than the suspicion grace; C's relayed acks keep A/B alive.
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                await converge(
                    [a, b, c], dict.fromkeys("abc", MemberState.ALIVE), timeout=0.5
                )
                await asyncio.sleep(0.1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("host", "advertise"), [("127.0.0.1", "localhost"), ("::1", "::1")]
)
def test_real_address_families(tmp_path, host, advertise):
    if host == "::1" and not socket.has_ipv6:
        pytest.skip("IPv6 unavailable")

    async def scenario():
        async with hosts(tmp_path, "ab", host, advertise) as (a, b):
            await a.client.request("init", {"name": "lab"})
            await b.join(a)
            await converge([a, b], dict.fromkeys("ab", MemberState.ALIVE))
            await a.stop()
            with pytest.raises(ClusterUnavailable):
                await a.client.status()

    asyncio.run(scenario())


def test_bad_credentials_do_not_join(tmp_path):
    async def scenario():
        async with hosts(tmp_path, "ab") as (a, b):
            await a.client.request("init", {"name": "lab"})
            from atlas.cluster.store import Credential

            Credential.create(b.credentials)
            with pytest.raises(ClusterError, match="authentication_or_format_failed"):
                await b.client.request("join", {"seeds": [a.endpoint]})
            assert (await b.client.status()).membership == "uninitialized"
            assert len((await a.client.members()).members) == 1

    asyncio.run(scenario())
