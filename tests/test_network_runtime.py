from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import iroh

from openagent_cli import main
from openagent_cli.client import GatewayClient
from openagent_cli.network import user_store
from openagent_cli.network.client.session import NetworkBinding, SessionDialer
from openagent_cli.network.identity import Identity, user_identity_path
from openagent_cli.network.iroh_node import IrohNode, NetworkAlpn


class _Connection:
    def close(self, _code, _reason):
        return None


class _DialNode:
    def __init__(self):
        self.calls: list[dict] = []

    async def dial(self, node_id, alpn, *, relay_url=None, addresses=None):
        self.calls.append({
            "node_id": node_id,
            "alpn": alpn,
            "relay_url": relay_url,
            "addresses": addresses,
        })
        return _Connection()


class _LifecycleNode:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class _LifecycleDialer:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _Proxy:
    ws_url = "ws://127.0.0.1:1/ws"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class CoordinatorHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_dialer_uses_hints_only_for_coordinator_node(self):
        node = _DialNode()
        binding = NetworkBinding(
            network_id="network-1",
            network_name="local",
            coordinator_node_id="coordinator-node",
            coordinator_pubkey_bytes=b"x" * 32,
            our_handle="alice",
            coordinator_relay_url="https://relay.invalid",
            coordinator_addresses=("127.0.0.1:45000", "[::1]:45000"),
        )
        dialer = SessionDialer(node=node, binding=binding, cert_wire=b"cert")

        first = await dialer._get_or_open_connection("coordinator-node")
        self.assertIs(first, await dialer._get_or_open_connection("coordinator-node"))
        await dialer._get_or_open_connection("agent-node")

        self.assertEqual(node.calls, [
            {
                "node_id": "coordinator-node",
                "alpn": NetworkAlpn.GATEWAY,
                "relay_url": "https://relay.invalid",
                "addresses": ["127.0.0.1:45000", "[::1]:45000"],
            },
            {
                "node_id": "agent-node",
                "alpn": NetworkAlpn.GATEWAY,
                "relay_url": None,
                "addresses": None,
            },
        ])

    async def test_from_network_forwards_saved_hints_to_refresh_lookup_and_binding(self):
        relay = "https://relay.invalid"
        addresses = ["127.0.0.1:46000"]
        network = SimpleNamespace(
            network_id="network-1",
            name="local",
            handle="alice",
            coordinator_node_id="coordinator-node",
            coordinator_pubkey_bytes=b"x" * 32,
            coordinator_relay_url=relay,
            coordinator_addresses=addresses,
        )
        store = SimpleNamespace(active_agent=None)
        node = _LifecycleNode()
        dialer = _LifecycleDialer()
        captured: dict = {}

        def make_dialer(**kwargs):
            captured["binding"] = kwargs["binding"]
            return dialer

        async def list_agents(**kwargs):
            captured["lookup"] = kwargs
            return [{"handle": "agent", "node_id": "coordinator-node"}]

        refresh = AsyncMock(return_value=b"refreshed-cert")
        with (
            patch("openagent_cli.client.user_store.load", return_value=store),
            patch("openagent_cli.client.user_store.find", return_value=network),
            patch("openagent_cli.client.user_store.ensure_user_identity_dir"),
            patch("openagent_cli.client.user_store.user_identity_path", return_value="identity"),
            patch("openagent_cli.client.user_store.read_cert", return_value=None),
            patch("openagent_cli.client.user_store.write_cert"),
            patch("openagent_cli.network.user_store.save"),
            patch("openagent_cli.client.load_or_create_identity", return_value=object()),
            patch("openagent_cli.client.IrohNode", return_value=node),
            patch("openagent_cli.client.SessionDialer", side_effect=make_dialer),
            patch("openagent_cli.client.LoopbackProxy", side_effect=_Proxy),
            patch("openagent_cli.network.client.login.refresh_cert", refresh),
            patch("openagent_cli.network.client.login.list_agents", side_effect=list_agents),
        ):
            client = await GatewayClient.from_network(
                handle="alice",
                network_name="local",
                password="test-only-password",
            )

        refresh_kwargs = refresh.await_args.kwargs
        self.assertEqual(refresh_kwargs["relay_url"], relay)
        self.assertEqual(refresh_kwargs["addresses"], addresses)
        self.assertEqual(captured["lookup"]["relay_url"], relay)
        self.assertEqual(captured["lookup"]["addresses"], addresses)
        self.assertEqual(captured["binding"].coordinator_relay_url, relay)
        self.assertEqual(
            captured["binding"].coordinator_addresses,
            tuple(addresses),
        )

        await client.disconnect()
        self.assertTrue(node.stopped)
        self.assertTrue(dialer.closed)

    async def test_agents_command_uses_saved_hints(self):
        network = SimpleNamespace(
            name="local",
            coordinator_node_id="coordinator-node",
            coordinator_relay_url="https://relay.invalid",
            coordinator_addresses=["127.0.0.1:47000"],
        )
        store = SimpleNamespace(
            active_network="local",
            active_agent=None,
            networks=[network],
        )
        node = _LifecycleNode()
        lookup = AsyncMock(return_value=[])
        with (
            patch("openagent_cli.network.user_store.load", return_value=store),
            patch("openagent_cli.network.user_store.find", return_value=network),
            patch("openagent_cli.network.user_store.ensure_user_identity_dir"),
            patch("openagent_cli.network.user_store.user_identity_path", return_value="identity"),
            patch("openagent_cli.network.identity.load_or_create_identity", return_value=object()),
            patch("openagent_cli.network.iroh_node.IrohNode", return_value=node),
            patch("openagent_cli.network.client.login.list_agents", lookup),
        ):
            await main._run_agents_cli("local")

        self.assertEqual(lookup.await_args.kwargs, {
            "node": node,
            "coordinator_node_id": "coordinator-node",
            "relay_url": "https://relay.invalid",
            "addresses": ["127.0.0.1:47000"],
        })
        self.assertTrue(node.stopped)


class UserDirectoryOverrideTests(unittest.TestCase):
    def test_override_is_the_single_root_for_identity_store_and_certs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "isolated-user"
            with patch.dict(os.environ, {"OPENAGENT_USER_DIR": str(root)}):
                self.assertEqual(user_identity_path(), root / "identity.key")
                self.assertEqual(user_store.store_path(), root / "networks.toml")
                self.assertEqual(
                    user_store.cert_path_for("network/1", "Alice Smith"),
                    root / "certs" / "network_1__Alice_Smith.cert",
                )
                self.assertEqual(user_store.ensure_user_identity_dir(), root)

            self.assertTrue((root / "certs").is_dir())

    def test_saved_coordinator_hints_survive_reload_and_hintless_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"OPENAGENT_USER_DIR": tmp}):
                store = user_store.UserStore()
                user_store.add_or_update(
                    store,
                    name="local",
                    network_id="network-1",
                    coordinator_node_id="coordinator-node",
                    coordinator_pubkey_hex="00" * 32,
                    handle="alice",
                    coordinator_relay_url="https://relay.invalid",
                    coordinator_addresses=["127.0.0.1:48000"],
                )
                user_store.save(store)

                reloaded = user_store.load()
                updated = user_store.add_or_update(
                    reloaded,
                    name="local",
                    network_id="network-1",
                    coordinator_node_id="coordinator-node",
                    coordinator_pubkey_hex="00" * 32,
                    handle="alice",
                )

            self.assertEqual(updated.coordinator_relay_url, "https://relay.invalid")
            self.assertEqual(updated.coordinator_addresses, ["127.0.0.1:48000"])


class _Endpoint:
    def node_id(self):
        return "local-node"


class _RuntimeNode:
    def __init__(self):
        self.shutdown_called = False

    def endpoint(self):
        return _Endpoint()

    async def shutdown(self):
        self.shutdown_called = True


class _IrohRuntime:
    def __init__(self):
        self.runtime_node = _RuntimeNode()

    def node(self):
        return self.runtime_node


class DiscoveryOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_override_builds_node_with_discovery_disabled(self):
        runtime = _IrohRuntime()
        options: dict = {}

        def capture_options(**kwargs):
            options.update(kwargs)
            return object()

        with (
            patch.dict(os.environ, {"OPENAGENT_IROH_DISCOVERY": " NoNe "}),
            patch("iroh.iroh_ffi.uniffi_set_event_loop"),
            patch("openagent_cli.network.iroh_node.iroh.NodeOptions", side_effect=capture_options),
            patch(
                "openagent_cli.network.iroh_node.iroh.Iroh.memory_with_options",
                new=AsyncMock(return_value=runtime),
            ),
        ):
            node = IrohNode(Identity.generate())
            await node.start()
            await node.stop()

        self.assertIs(options["node_discovery"], iroh.NodeDiscoveryConfig.NONE)
        self.assertTrue(runtime.runtime_node.shutdown_called)


if __name__ == "__main__":
    unittest.main()
