# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Read-only TLS client for the bridge's local notification port (R1.1, R5.7, R5.8).

Never writes to the socket (R4.3). The legacy cipher and disabled chain
verification apply only to this one connection; an optional SHA-256 pin
restores endpoint authenticity on the LAN.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
from typing import Callable

from .protocol import FrameParser, decode_notification

log = logging.getLogger(__name__)


def bridge_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # private, untrusted certificate; see pinning below
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("AES256-SHA256:@SECLEVEL=0")  # the only cipher the BG 220 offers
    return ctx


class CertPinError(ConnectionError):
    pass


class LocalFeed:
    def __init__(self, host: str, port: int, on_event: Callable[[str, dict, int | None], None],
                 on_status: Callable[[bool], None], pin_sha256: str = "",
                 on_new_pin: Callable[[str], None] | None = None,
                 ssl_context: ssl.SSLContext | None = None, idle_timeout: float = 300.0) -> None:
        self.host, self.port = host, port
        self.on_event, self.on_status = on_event, on_status
        self.pin = pin_sha256.lower()
        self.on_new_pin = on_new_pin
        self.ctx = ssl_context or bridge_ssl_context()
        self.idle_timeout = idle_timeout
        self.connected = False
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _check_pin(self, writer: asyncio.StreamWriter) -> None:
        sslobj = writer.get_extra_info("ssl_object")
        der = sslobj.getpeercert(binary_form=True) if sslobj else None
        if not der:
            raise CertPinError("no peer certificate")
        fp = hashlib.sha256(der).hexdigest()
        if self.pin:
            if fp != self.pin:
                raise CertPinError("bridge certificate does not match the pinned fingerprint")
        else:
            self.pin = fp  # trust on first use (R5.7)
            log.info("event=bridge_cert_pinned sha256=%s", fp)
            if self.on_new_pin:
                self.on_new_pin(fp)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            got_frame = False
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, ssl=self.ctx, server_hostname=None), 10)
                try:
                    self._check_pin(writer)
                    self.connected = True
                    self.on_status(True)
                    parser = FrameParser()
                    while not self._stop.is_set():
                        try:
                            data = await asyncio.wait_for(reader.read(4096), self.idle_timeout)
                        except asyncio.TimeoutError:
                            log.info("event=local_feed_idle seconds=%d", int(self.idle_timeout))
                            break
                        if not data:
                            break  # the bridge closes about every 30 s (normal)
                        for payload in parser.feed(data):
                            ev = decode_notification(payload)
                            if ev:
                                got_frame = True
                                self.on_event(ev.dev, ev.attrs, ev.idx)
                        for w in parser.pop_warnings():
                            log.warning("event=local_framing detail=%s", w)
                finally:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), 2)
                    except Exception:
                        pass
            except CertPinError as e:
                log.error("event=local_feed_cert_error detail=%s", e)
                backoff = 60.0
            except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
                log.debug("event=local_feed_connect_failed error=%s", type(e).__name__)
                backoff = min(backoff * 2, 60.0)
            if self.connected:
                self.connected = False
                self.on_status(False)
            if got_frame:
                backoff = 1.0
            delay = 0.2 if got_frame else backoff
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
