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


def _heads(observed: str = "192.168.100.19") -> lan_ingress._ObservedHostHeads:
    return lan_ingress._ObservedHostHeads(
        f"{lan_ingress.OBSERVED_HOST_HEADER}: {observed}".encode()
    )


def _through(stream: bytes, size: int, observed: str = "192.168.100.19") -> bytes:
    heads = _heads(observed)
    return b"".join(
        heads.feed(stream[at : at + size]) for at in range(0, len(stream), size)
    )


_PULL = (
    b"POST /api/device-control/v1/configuration:pull HTTP/1.1\r\n"
    b"Host: eidolon-hub-f89c0ecca5d0070a7989.local:9443\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 17\r\n"
    b"\r\n"
    b'{"nonce": "abcd"}'
)


def test_every_request_on_one_connection_says_where_it_arrived() -> None:
    """Including the second, or the answer would depend on socket reuse.

    A device that keeps its connection open pulls its configuration again on
    the same one. If only the first request carried the observation, whether
    the Authority saw anything would turn on whether the client happened to
    reuse a socket — and the binding it mints would differ for reasons nobody
    could see from either end.
    """

    forwarded = _through(_PULL + _PULL, size=4096)

    assert forwarded.count(b"Eidolon-Observed-Host: 192.168.100.19\r\n") == 2
    # The body is not the relay's business and comes out untouched.
    assert forwarded.count(b'{"nonce": "abcd"}') == 2
    assert forwarded.endswith(b'{"nonce": "abcd"}')


def test_the_result_does_not_depend_on_how_the_stream_was_chopped() -> None:
    """A request head arrives split across reads whenever the network says so.

    This is the whole risk of looking at the stream at all: a relay that only
    works when a request lands in one read works on a loopback test and not on
    a board. So every split is exercised, including one byte at a time.
    """

    whole = _through(_PULL + _PULL, size=len(_PULL) * 2)

    for size in range(1, 40):
        assert _through(_PULL + _PULL, size) == whole, size


def test_a_client_supplied_copy_is_replaced_by_the_observation() -> None:
    claimed = (
        b"GET /x HTTP/1.1\r\n"
        b"Host: h\r\n"
        b"eidolon-observed-host: 10.42.0.2\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )

    forwarded = _through(claimed, size=4096)

    assert b"10.42.0.2" not in forwarded
    assert forwarded.count(b"Eidolon-Observed-Host: ") == 1
    assert b"Eidolon-Observed-Host: 192.168.100.19\r\n" in forwarded


@pytest.mark.parametrize(
    "stream",
    [
        # Chunked: this relay does not undertake to find the end of it.
        b"POST /x HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"4\r\nabcd\r\n0\r\n\r\n",
        # An upgrade stops being HTTP after the handshake.
        b"GET /x HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n\r\n\x81\x05hello",
        b"CONNECT h:443 HTTP/1.1\r\nHost: h\r\n\r\n\x16\x03\x01rawbytes",
        b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: banana\r\n\r\nbody",
        b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nx",
    ],
)
def test_framing_it_cannot_follow_gives_the_connection_back_untouched(stream) -> None:
    """Losing the header costs nothing; guessing a body length costs the stream.

    Everything downstream treats the observation as optional, so a connection
    this cannot read is one it simply does not annotate. What it must never do
    is carry on parsing and mistake a body for a request head.
    """

    forwarded = _through(stream, size=4096)

    # The first head is still annotated — it was read before anything was in
    # doubt — and after that every byte is the client's own, in order.
    assert forwarded.replace(b"Eidolon-Observed-Host: 192.168.100.19\r\n", b"") == stream


def test_a_stream_that_is_not_http_is_relayed_rather_than_buffered() -> None:
    """The head has to end somewhere, or a peer could pin memory by never ending it."""

    noise = b"\x16\x03\x01" + b"z" * (lan_ingress._MAX_HEAD_BYTES + 1024)

    assert _through(noise, size=8192) == noise


def test_nothing_worth_reporting_leaves_the_stream_alone() -> None:
    """Which is what a connection over loopback is, and it must cost nothing."""

    class Socket:
        def __init__(self, sockname):
            self.sockname = sockname

        def get_extra_info(self, name):
            return self.sockname if name == "sockname" else None

    assert lan_ingress._observed_host(Socket(("192.168.100.19", 9443))) == "192.168.100.19"
    for nothing in (("127.0.0.1", 9443), ("169.254.7.7", 9443), ("0.0.0.0", 9443),
                    ("/run/x.sock", 0), None, ()):
        assert lan_ingress._observed_host(Socket(nothing)) == "", nothing
