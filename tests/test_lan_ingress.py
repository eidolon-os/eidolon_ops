from __future__ import annotations

import asyncio
from argparse import Namespace

import pytest

from eidolon_ops import lan_ingress


class Writer:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FailingWriter(Writer):
    async def drain(self) -> None:
        raise ConnectionError("closed")


def test_relay_forwards_bounded_chunks_and_closes_writer() -> None:
    writer = Writer()

    async def exercise() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"one")
        reader.feed_data(b"two")
        reader.feed_eof()
        await lan_ingress._relay(reader, writer)

    asyncio.run(exercise())

    assert writer.chunks == [b"onetwo"]
    assert writer.closed is True


def test_relay_treats_peer_disconnect_as_normal_shutdown() -> None:
    writer = FailingWriter()

    async def exercise() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"one")
        await lan_ingress._relay(reader, writer)

    asyncio.run(exercise())
    assert writer.closed is True


def test_serve_configures_tls_listener_and_relays_connection(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Context:
        minimum_version = None

        def load_cert_chain(self, certificate, private_key) -> None:
            captured["identity"] = (certificate, private_key)

    class Server:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def serve_forever(self) -> None:
            captured["served"] = True

    async def start_server(callback, host, port, *, ssl) -> Server:
        captured["listener"] = (callback, host, port, ssl)
        return Server()

    monkeypatch.setattr(lan_ingress.ssl, "SSLContext", lambda _protocol: Context())
    monkeypatch.setattr(lan_ingress.asyncio, "start_server", start_server)
    arguments = Namespace(
        listen_host="0.0.0.0",
        listen_port=8443,
        upstream_host="127.0.0.1",
        upstream_port=8082,
        certificate="hub.crt",
        private_key="hub.key",
    )

    asyncio.run(lan_ingress._serve(arguments))

    assert captured["identity"] == ("hub.crt", "hub.key")
    assert captured["listener"][1:3] == ("0.0.0.0", 8443)
    assert captured["served"] is True


def test_listener_closes_client_when_upstream_is_unavailable(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Context:
        minimum_version = None

        def load_cert_chain(self, _certificate, _private_key) -> None:
            return None

    class Server:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def serve_forever(self) -> None:
            callback = captured["callback"]
            await callback(asyncio.StreamReader(), Writer())

    async def start_server(callback, *_args, **_kwargs) -> Server:
        captured["callback"] = callback
        return Server()

    async def unavailable(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(lan_ingress.ssl, "SSLContext", lambda _protocol: Context())
    monkeypatch.setattr(lan_ingress.asyncio, "start_server", start_server)
    monkeypatch.setattr(lan_ingress.asyncio, "open_connection", unavailable)
    arguments = Namespace(
        listen_host="0.0.0.0",
        listen_port=8443,
        upstream_host="127.0.0.1",
        upstream_port=8082,
        certificate="hub.crt",
        private_key="hub.key",
    )

    asyncio.run(lan_ingress._serve(arguments))


def test_main_rejects_invalid_port_and_handles_interrupt(monkeypatch) -> None:
    with pytest.raises(SystemExit):
        lan_ingress.main(
            [
                "--listen-port",
                "0",
                "--upstream-port",
                "8082",
                "--certificate",
                "hub.crt",
                "--private-key",
                "hub.key",
            ]
        )

    async def interrupted(_arguments) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(lan_ingress, "_serve", interrupted)
    assert (
        lan_ingress.main(
            [
                "--listen-port",
                "8443",
                "--upstream-port",
                "8082",
                "--certificate",
                "hub.crt",
                "--private-key",
                "hub.key",
            ]
        )
        == 0
    )
