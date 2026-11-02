"""Offline stored-audio evidence boundaries; no device or upload transport IO.

Fixtures are synthetic examples of documented XML, never hardware captures.
These tests cannot establish persistent storage or audible native playback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from reolink_aio.exceptions import (
    ReolinkConnectionError,
)

from custom_components.reolink_audio_library.stored_audio import (
    MAX_XML_BYTES,
    AudioProtocolError,
    StoredAudioError,
    async_read_inventory,
    parse_audio_capabilities,
    parse_audio_inventory,
)

FIXTURES = Path(__file__).parent / "fixtures" / "audio"
MODERN = (FIXTURES / "modern.xml").read_text()
LEGACY = (FIXTURES / "legacy.xml").read_text()
CAPABILITIES = (FIXTURES / "capabilities.xml").read_text()
SECRET = "secret-password signed_url?token=private-audio"


@pytest.fixture
def api():
    """A borrowed Host: only explicit mocked operations can run."""
    return SimpleNamespace(
        baichuan=SimpleNamespace(send=AsyncMock(return_value=MODERN), GetAudioFileList=AsyncMock()),
        supported=Mock(return_value=True),
        get_state=AsyncMock(),
        quick_reply_dict=Mock(return_value={-1: "off", 0: "Wait & welcome", 9: "日本語の録音"}),
        play_quick_reply=AsyncMock(),
        login=AsyncMock(),
        logout=AsyncMock(),
        close=AsyncMock(),
    )


def test_reference_library_shape_without_list_wrapper():
    inventory = parse_audio_inventory("<body><audioFileInfo><id>18</id><fileName>A</fileName></audioFileInfo></body>")
    assert inventory.files[0].id == 18
    assert inventory.max_files is None


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("<body>", "malformed"),
        ("<body/>", "recognized audio inventory"),
        ("<body><Error>unsupported</Error></body>", "recognized audio inventory"),
        ("<body><audioFileInfoList/><audioFileInfoList/></body>", "multiple audio lists"),
        ("<audioFileInfo><fileName>A</fileName></audioFileInfo>", "missing id"),
        ("<audioFileInfo><id>1</id></audioFileInfo>", "missing its name"),
        ("<audioFileInfo><id>-1</id><fileName>off</fileName></audioFileInfo>", "must not be negative"),
        ("<audioFileInfo><id>x</id><fileName>A</fileName></audioFileInfo>", "must be an integer"),
        (
            "<audioFileInfo><id>1</id><fileName>A</fileName><fileSize>-4</fileSize></audioFileInfo>",
            "must not be negative",
        ),
        (
            "<audioFileInfo><id>1</id><fileName>A</fileName><audioLen>1.5</audioLen></audioFileInfo>",
            "must be an integer",
        ),
        (
            "<audioFileInfoList><audioFileInfo><id>1</id><fileName>A</fileName></audioFileInfo>"
            "<audioFileInfo><id>1</id><fileName>B</fileName></audioFileInfo></audioFileInfoList>",
            "duplicate",
        ),
        ('<!DOCTYPE body [<!ENTITY injected "expanded">]><body>&injected;</body>', "document types or entities"),
        ("x" * (MAX_XML_BYTES + 1), "size limit"),
        (None, "invalid"),
    ],
)
def test_invalid_metadata_does_not_become_an_empty_supported_list(xml, message):
    with pytest.raises(AudioProtocolError, match=message):
        parse_audio_inventory(xml)


def test_capabilities_keep_unproven_capacity_units_and_format_applicability():
    result = parse_audio_capabilities(CAPABILITIES)
    assert result["max_capacity_raw"] == 50000
    assert result["max_capacity_unit"] is None
    assert result["verified_for_upload"] is False
    assert result["formats"][0] == {
        "audio_type": "adpcm",
        "sample_rate": 16000,
        "sample_precision": 16,
        "length_per_encoder": 1024,
        "sound_track": "mono",
    }
    assert result["formats"][1]["sample_precision"] is None


@pytest.mark.parametrize("xml", ["<body/>", "<body><AudioEncodeAbility/><AudioEncodeAbility/></body>"])
def test_unrecognized_capabilities_are_not_success(xml):
    with pytest.raises(AudioProtocolError, match="one AudioEncodeAbility"):
        parse_audio_capabilities(xml)


async def test_deadline_cancels_hung_read_without_authentication_or_mutation(api):
    cancelled = asyncio.Event()

    async def hung_read(**_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    api.baichuan.send.side_effect = hung_read
    with pytest.raises(StoredAudioError, match="TimeoutError"):
        await async_read_inventory(api, 0, timeout=0.01)
    assert cancelled.is_set()
    api.logout.assert_not_awaited()
    api.login.assert_not_awaited()


async def test_next_read_can_reuse_borrowed_session_after_connection_recovery(api):
    api.baichuan.send.side_effect = [ReolinkConnectionError(SECRET), MODERN]
    with pytest.raises(StoredAudioError, match="ReolinkConnectionError"):
        await async_read_inventory(api, 3)
    recovered = await async_read_inventory(api, 3)
    assert recovered.files[0].id == 0
    assert api.baichuan.send.await_args_list == [call(cmd_id=347, channel=3), call(cmd_id=347, channel=3)]
    api.login.assert_not_awaited()
    api.logout.assert_not_awaited()
    api.close.assert_not_awaited()
