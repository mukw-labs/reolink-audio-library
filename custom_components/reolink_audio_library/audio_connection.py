"""Owned library sessions for stored-audio operations; never reconfigure ports."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager

from .stored_audio import StoredAudioError


async def _close_owned_host(api) -> None:
    try:
        async with asyncio.timeout(10):
            await api.logout()
    except Exception:
        pass
    finally:
        connection = getattr(api.baichuan, "_connection", None)
        if connection is not None:
            async with asyncio.timeout(6):
                await connection.close()


def owned_host_factory(data: Mapping, *, session_callback=None):
    """Construct a new Host per context, using existing entry credentials.

    HTTP authentication must fail rather than letting reolink_aio enable camera
    ports. Disconnecting an interrupted import lets the firmware clean staging.
    """
    port, use_https = data.get("port"), data.get("use_https")
    if type(port) is not int or not 1 <= port <= 65535 or type(use_https) is not bool:
        raise StoredAudioError("An explicitly configured HTTP(S) port and scheme are required")

    @asynccontextmanager
    async def connect():
        from reolink_aio.api import Host

        options = dict(
            host=data["host"],
            username=data["username"],
            password=data["password"],
            port=port,
            use_https=use_https,
            bc_port=int(data.get("baichuan_port", 9000)),
        )
        if session_callback is not None:
            options["aiohttp_get_session_callback"] = session_callback
        api = Host(**options)

        async def configured_login():
            if api._port != port or api._use_https is not use_https:
                raise StoredAudioError("The configured HTTP(S) endpoint changed; automatic discovery is disabled")
            await api.login()

        async def reject_port_change(*_args, **_kwargs):
            raise StoredAudioError("Camera port reconfiguration is disabled for stored audio")

        api._login_open_port = configured_login
        api._login_try_ports = reject_port_change
        api.baichuan.set_port_enabled = reject_port_change
        try:
            yield api
        finally:
            task = asyncio.create_task(_close_owned_host(api))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    return connect
