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
        self.dropped = {"tcp": set(), "udp": set()}
        self.forwarded = {"tcp": set(), "udp": set()}

    def connection_made(self, transport):
        self.transport = transport

    def allows(self, source, protocol):
        if self.blocked or source in self.drop_sources:
            self.dropped[protocol].add(source)
            return False
        self.forwarded[protocol].add(source)
        return True

    def datagram_received(self, data, address):
        if self.allows(address[0], "udp"):
            self.transport.sendto(data, (self.host, self.destination))

    async def forward(self, reader, writer):
        self.tasks.add(asyncio.current_task())
        upstream = None
        peer = writer.get_extra_info("peername")[0]
        try:
            if not self.allows(peer, "tcp"):
                return
            remote, upstream = await asyncio.open_connection(
                self.host, self.destination
            )

            async def copy(source, target):
                while data := await source.read(65536):
                    if not self.allows(peer, "tcp"):
                        break
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
async def hosts(tmp_path, names="abcd", host=None, advertise_host=None):
    nodes = []
    servers, transports = [], []
    try:
        for index, name in enumerate(names, start=2):
            address = host or f"127.0.0.{index}"
            node = Host(tmp_path, name, address, advertise_host)
            nodes.append(node)
            servers.append(
                await asyncio.start_server(
                    node.gateway.forward, address, node.gateway_port
                )
            )
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda gateway=node.gateway: gateway,
                local_addr=(address, node.gateway_port),
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


async def converge(nodes, expected, *, timeout=12, labels=None):
    snapshots = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                snapshots = [await node.client.members() for node in nodes]
                states_match = all(
                    {m.id: m.state for m in snapshot.members} == expected
                    for snapshot in snapshots
                )
                labels_match = labels is None or all(
                    {m.id: m.labels for m in snapshot.members if m.id in labels}
                    == labels
                    for snapshot in snapshots
                )
                if states_match and labels_match:
                    return snapshots
                await asyncio.sleep(0.05)
    except TimeoutError:
        observed = {
            node.directory.name: {
                m.id: (m.state.value, m.incarnation, m.labels) for m in snapshot.members
            }
            for node, snapshot in zip(nodes, snapshots, strict=False)
        }
        logs = {node.directory.name: node.logs() for node in nodes}
        raise AssertionError(
            f"expected={expected}, observed={observed}, logs={logs}"
        ) from None


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
                node.gateway.drop_sources.add(a.host)
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


def block_between(first, second):
    for node in first:
        node.gateway.drop_sources.update(peer.host for peer in second)
    for node in second:
        node.gateway.drop_sources.update(peer.host for peer in first)


async def update_labels(node, labels):
    await node.stop()
    config = json.loads(node.config.read_text())
    config["labels"] = labels
    node.config.write_text(json.dumps(config))
    await node.start()


def test_one_broken_pair_preserves_indirect_probes_and_metadata(tmp_path):
    async def scenario():
        async with hosts(tmp_path) as (a, b, c, d):
            await a.client.request("init", {"name": "lab"})
            await b.join(a)
            await c.join(b)
            await d.join(c)
            alive = dict.fromkeys("abcd", MemberState.ALIVE)
            await converge([a, b, c, d], alive)
            block_between([b], [c])
            await update_labels(b, {"revision": "isolated-link"})
            await converge(
                [a, b, c, d], alive, labels={"b": {"revision": "isolated-link"}}
            )
            # Observe beyond the suspicion grace, checking states throughout.
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                await converge([a, b, c, d], alive, timeout=0.5)
                await asyncio.sleep(0.05)
            for protocol in ("tcp", "udp"):
                assert b.host in c.gateway.dropped[protocol]
                assert c.host in b.gateway.dropped[protocol]
                assert a.host in d.gateway.forwarded[protocol]
                assert d.host in a.gateway.forwarded[protocol]

    asyncio.run(scenario())


def test_partition_join_leave_and_recovery_without_a_majority(tmp_path):
    async def scenario():
        async with hosts(tmp_path, "abcde") as (a, b, c, d, e):
            await a.client.request("init", {"name": "lab"})
            await b.join(a)
            await c.join(b)
            await d.join(c)
            alive = dict.fromkeys("abcd", MemberState.ALIVE)
            await converge([a, b, c, d], alive)
            block_between([a, b, e], [c, d])
            await converge([a, b], alive | dict.fromkeys("cd", MemberState.UNREACHABLE))
            await converge([c, d], alive | dict.fromkeys("ab", MemberState.UNREACHABLE))
            await e.join(b)
            result = await d.client.request("leave")
            assert result == {"membership": "left", "propagated_to_peer": True}
            await converge(
                [a, b, e],
                dict.fromkeys("abe", MemberState.ALIVE)
                | dict.fromkeys("cd", MemberState.UNREACHABLE),
            )
            await converge(
                [c],
                {
                    "a": MemberState.UNREACHABLE,
                    "b": MemberState.UNREACHABLE,
                    "c": MemberState.ALIVE,
                    "d": MemberState.LEFT,
                },
            )
            for left in (a, b):
                for right in (c, d):
                    for protocol in ("tcp", "udp"):
                        assert left.host in right.gateway.dropped[protocol]
                        assert right.host in left.gateway.dropped[protocol]
            for node in (a, b, c, d, e):
                node.gateway.drop_sources.clear()
            expected = dict.fromkeys("abce", MemberState.ALIVE) | {
                "d": MemberState.LEFT
            }
            await converge([a, b, c, e], expected, timeout=18)
            assert (await d.client.status()).membership == "left"
            # A normal restart and repeated synchronization must retain D's tombstone.
            await b.stop()
            await b.start()
            await converge([a, b, c, e], expected)

    asyncio.run(scenario())


def test_self_seed_alias_does_not_count_as_peer_contact_or_leave(tmp_path):
    async def scenario():
        async with hosts(tmp_path, "ab", host="127.0.0.1") as (a, b):
            aliases = [a.endpoint, f"localhost:{a.gateway_port}"]
            config = json.loads(a.config.read_text())
            config["seeds"] = aliases
            a.config.write_text(json.dumps(config))
            await a.stop()
            await a.start()
            await a.client.request("init", {"name": "lab"})
            # The explicit self-join exercises both incoming and outgoing ID checks.
            for alias in aliases:
                with pytest.raises(ClusterError, match="self_peer"):
                    await a.client.request("join", {"seeds": [alias]})
            assert (await a.client.status()).last_peer_contact is None
            await b.join(a)
            await converge([a, b], dict.fromkeys("ab", MemberState.ALIVE))
            assert (await a.client.status()).last_peer_contact is not None
            await b.stop()
            await a.stop()
            await a.start()
            assert (await a.client.status()).last_peer_contact is None
            result = await a.client.request("leave")
            assert result == {"membership": "left", "propagated_to_peer": False}
            assert (await a.client.status()).last_peer_contact is None

    asyncio.run(scenario())
