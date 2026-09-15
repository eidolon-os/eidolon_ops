"""Deployment-owned TLS ingress for loopback-only Eidolon HTTP services.

Stands in front of a service that binds loopback and holds the LAN identity
the service does not: the TLS material is the deployment's, not the
component's, which is the whole reason this exists rather than the service
terminating its own TLS.

That position makes it the only thing on the Host that can see something the
service behind it needs. A service bound to loopback cannot tell which of this
machine's addresses a peer reached it on — every connection it sees comes from
127.0.0.1, because this relay opened it. This socket is where the peer actually
arrived, so it says so, in one request header the service may use and is never
owed.

Stdlib only, and no import from the package it lives in: this file is shipped
on its own to /usr/local/libexec and run by the system python, so anything it
cannot get from the standard library it cannot have.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import ssl
from collections.abc import Callable, Sequence
from contextlib import suppress

#: Where this relay tells the service behind it the peer arrived. Named for
#: what it is rather than borrowed from `Forwarded`/`X-Forwarded-For`, which
#: carry the peer's own address; this is the other end of the same socket.
OBSERVED_HOST_HEADER = "Eidolon-Observed-Host"

_OBSERVED_FIELD = OBSERVED_HOST_HEADER.lower().encode()
#: A request head larger than this is not one this relay will hold in memory
#: waiting for its end. Generous next to any real one, and reaching it means
#: the stream is not the HTTP this expects, so it stops looking at it.
_MAX_HEAD_BYTES = 64 * 1024


def _observed_host(writer: asyncio.StreamWriter) -> str:
    """Which of this Host's addresses the peer reached it on, if it is worth saying.

    The local end of the accepted connection, which survives TLS termination —
    and not the peer's own address, which answers a different question: what
    the service behind this has to hand out is somewhere to find *this* Host.

    Empty when the answer cannot be that. A connection that arrived over
    loopback, a link-local address, or no address this recognises has nothing
    to report, and reporting it anyway would hand the service an address it
    would have to know to throw away.
    """

    sockname = writer.get_extra_info("sockname")
    if not isinstance(sockname, (tuple, list)) or not sockname:
        return ""
    try:
        address = ipaddress.ip_address(str(sockname[0]))
    except ValueError:
        return ""
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        return ""
    return str(address)


def _body_length(head: bytes) -> int | None:
    """How many body bytes follow this request head, or None to stop reading.

    None is not a failure. It means the framing is one this relay does not
    undertake to follow — a chunked body, a protocol upgrade, a CONNECT, a
    Content-Length that is not a number — and the honest response to that is
    to stop pretending to read HTTP and go back to being a byte relay. The
    header this adds is optional to everything downstream, so losing it on
    such a connection costs nothing; guessing a body length wrong would cost
    the connection itself.
    """

    lines = head.split(b"\r\n")
    if lines[0].upper().startswith(b"CONNECT "):
        return None
    length = 0
    declared = False
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        field = name.strip().lower()
        if field in (b"transfer-encoding", b"upgrade"):
            return None
        if field != b"content-length":
            continue
        if declared:
            # Two of them disagree about where this request ends, and nothing
            # good comes of picking one.
            return None
        declared = True
        try:
            length = int(value.strip())
        except ValueError:
            return None
        if length < 0:
            return None
    return length


def _with_observed_host(head: bytes, header: bytes) -> bytes:
    """The same request head, stating where it arrived and only once.

    Any copy the client sent is dropped first. Not because the value is a
    trust input — the service reading it only reorders addresses it already
    has, so a made-up one selects nothing — but because a header named for an
    observation should carry the observation, and a reader should not have to
    wonder which of two lines was the real one.
    """

    lines = head[:-2].split(b"\r\n")[:-1]
    kept = [lines[0]]
    kept.extend(
        line
        for line in lines[1:]
        if line.partition(b":")[0].strip().lower() != _OBSERVED_FIELD
    )
    return b"\r\n".join(kept) + b"\r\n" + header + b"\r\n\r\n"


class _ObservedHostHeads:
    """States where each request on one connection arrived, or gets out of the way.

    One connection carries a sequence of requests and the address they arrived
    at is the same for all of them, so every head gets the line — a keep-alive
    connection whose second request was missing it would make the service's
    answer depend on whether the client reused a socket.

    Finding those heads means following the framing, and following it wrongly
    would corrupt a body. So this follows only the framing it is sure of and
    hands the rest of the connection back to the plain relay the moment it is
    not, which is what `_body_length` returning None means.
    """

    def __init__(self, header: bytes) -> None:
        self._header = header
        self._buffer = bytearray()
        self._body_remaining = 0
        self._relaying = False

    def feed(self, chunk: bytes) -> bytes:
        if self._relaying:
            return chunk
        self._buffer += chunk
        out = bytearray()
        while self._buffer and not self._relaying:
            if self._body_remaining:
                taken = min(self._body_remaining, len(self._buffer))
                out += self._buffer[:taken]
                del self._buffer[:taken]
                self._body_remaining -= taken
                continue
            end = self._buffer.find(b"\r\n\r\n")
            if end < 0:
                self._relaying = len(self._buffer) > _MAX_HEAD_BYTES
                break
            head = bytes(self._buffer[: end + 4])
            del self._buffer[: end + 4]
            length = _body_length(head)
            out += _with_observed_host(head, self._header)
            if length is None:
                self._relaying = True
                break
            self._body_remaining = length
        if self._relaying:
            out += self._buffer
            self._buffer.clear()
        return bytes(out)


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    transform: Callable[[bytes], bytes] | None = None,
) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            forwarded = chunk if transform is None else transform(chunk)
            if forwarded:
                writer.write(forwarded)
                await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def _serve(arguments: argparse.Namespace) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(arguments.certificate, arguments.private_key)

    async def connected(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                arguments.upstream_host,
                arguments.upstream_port,
            )
        except OSError:
            client_writer.close()
            await client_writer.wait_closed()
            return
        observed = _observed_host(client_writer)
        await asyncio.gather(
            _relay(
                client_reader,
                upstream_writer,
                _ObservedHostHeads(
                    f"{OBSERVED_HOST_HEADER}: {observed}".encode()
                ).feed
                if observed
                else None,
            ),
            # Untouched in this direction. Nothing upstream says is about how
            # the request arrived.
            _relay(upstream_reader, client_writer),
        )

    server = await asyncio.start_server(
        connected,
        arguments.listen_host,
        arguments.listen_port,
        ssl=context,
    )
    async with server:
        await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="terminate TLS and relay bytes to one loopback service"
    )
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", required=True, type=int)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--private-key", required=True)
    arguments = parser.parse_args(argv)
    for name in ("listen_port", "upstream_port"):
        if not 1 <= getattr(arguments, name) <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be in 1..65535")
    with suppress(KeyboardInterrupt):
        asyncio.run(_serve(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
