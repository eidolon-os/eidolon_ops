"""Deployment-owned TLS ingress for loopback-only Eidolon HTTP services."""

from __future__ import annotations

import argparse
import asyncio
import ssl
from collections.abc import Sequence
from contextlib import suppress


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
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
        await asyncio.gather(
            _relay(client_reader, upstream_writer),
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
