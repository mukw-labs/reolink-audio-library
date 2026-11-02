"""Offline operator boundaries: no credentials or physical camera access."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from reolink_aio.exceptions import LoginError

from custom_components.reolink_audio_library.audio_connection import owned_host_factory
from custom_components.reolink_audio_library.stored_audio import StoredAudioError


@pytest.fixture
def entry():
    return {
        "data": {"host": "camera.invalid", "username": "test", "password": "SECRET", "port": 443, "use_https": True}
    }


@pytest.fixture
def host(monkeypatch):
    instance = SimpleNamespace(
        _port=443,
        _use_https=True,
        login=AsyncMock(),
        logout=AsyncMock(),
        baichuan=SimpleNamespace(_connection=SimpleNamespace(close=AsyncMock()), set_port_enabled=AsyncMock()),
    )
    constructor = Mock(return_value=instance)
    monkeypatch.setattr("reolink_aio.api.Host", constructor)
    return instance, constructor


@pytest.mark.parametrize(
    "field,value", [("port", None), ("port", True), ("port", 0), ("port", 65536), ("use_https", None), ("use_https", 1)]
)
def test_factory_requires_explicit_endpoint_before_host_creation(entry, host, field, value):
    entry["data"][field] = value
    with pytest.raises(StoredAudioError, match="explicitly configured"):
        owned_host_factory(entry["data"])
    host[1].assert_not_called()


async def test_http_fallback_uses_only_configured_login(entry, host):
    instance, _ = host
    async with owned_host_factory(entry["data"])() as api:
        await api._login_open_port()
        instance.login.assert_awaited_once()
        with pytest.raises(StoredAudioError, match="reconfiguration"):
            await api.baichuan.set_port_enabled("http", True)
        with pytest.raises(StoredAudioError, match="reconfiguration"):
            await api._login_try_ports()
    instance.logout.assert_awaited_once()
    instance.baichuan._connection.close.assert_awaited_once()


async def test_failed_http_login_propagates_without_reconfiguration(entry, host):
    instance, _ = host
    instance.login.side_effect = LoginError("TEST-ONLY")
    async with owned_host_factory(entry["data"])() as api:
        with pytest.raises(LoginError):
            await api._login_open_port()
    assert instance.login.await_count == 1


async def test_changed_endpoint_rejected_before_login(entry, host):
    instance, _ = host
    async with owned_host_factory(entry["data"])() as api:
        api._port = None
        with pytest.raises(StoredAudioError, match="endpoint changed"):
            await api._login_open_port()
    instance.login.assert_not_awaited()


async def test_cleanup_closes_socket_even_when_logout_fails(entry, host):
    instance, _ = host
    instance.logout.side_effect = RuntimeError("SECRET")
    async with owned_host_factory(entry["data"])():
        pass
    instance.baichuan._connection.close.assert_awaited_once()
