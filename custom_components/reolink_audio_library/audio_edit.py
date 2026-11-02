"""Guarded custom rename/delete with independent-session result verification."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from .audio_edit_transport import async_change_recording
from .audio_upload import UploadProfile, profile_for_device_info, validate_inventory_consistency
from .audio_upload_transport import _PinnedConnection, validate_recording_name
from .native_audio import async_read_native_inventory
from .recording_identity import RecordingSelection, recording_fingerprint
from .stored_audio import AudioInventory, StoredAudioError, _xml_root, async_read_inventory


class RecordingChangeError(StoredAudioError):
    def __init__(self, message, *, outcome_uncertain=False, status=None):
        self.outcome_uncertain = outcome_uncertain
        self.rspCode = status
        super().__init__(message)


@dataclass(frozen=True)
class RecordingState:
    custom: AudioInventory
    native: AudioInventory
    profile: UploadProfile


@dataclass(frozen=True)
class RecordingChangeReceipt:
    action: str
    id: int
    old_name: str
    name: str | None
    changed: bool
    acknowledged: bool
    verified_after_reconnect: bool
    status: str


async def _read_state(api, channel) -> RecordingState:
    profile = profile_for_device_info(await api.baichuan.get_info(), channel)
    native = await async_read_native_inventory(api, channel)
    custom = await async_read_inventory(api, channel)
    validate_inventory_consistency(custom, native, profile)
    if custom.max_files is None or not 0 < custom.max_files <= 12 or len(custom.files) > custom.max_files:
        raise RecordingChangeError("The camera reported an unrecognized recording capacity")
    return RecordingState(custom, native, profile)


async def _read_auto_reply(api, channel) -> tuple[int, int, int]:
    """Read-only command 427; never change the official reply selection."""
    xml = await api.baichuan.send(cmd_id=427, channel=channel, retry=1)
    root = _xml_root(xml)
    nodes = root.findall("AutoReply") if root.tag == "body" else []
    if len(nodes) != 1:
        raise RecordingChangeError("Could not verify the camera's automatic reply selection")
    info = nodes[0]
    values = []
    for tag in ("enable", "audioId", "timeout"):
        children = info.findall(tag)
        if len(children) != 1:
            raise RecordingChangeError("Automatic reply metadata is incomplete or ambiguous")
        text = children[0].text
        if not isinstance(text, str) or not text.isascii() or len(text) > 11:
            raise RecordingChangeError("Invalid automatic reply metadata")
        try:
            value = int(text)
            if str(value) != text:
                raise ValueError
            values.append(value)
        except ValueError:
            raise RecordingChangeError("Invalid automatic reply metadata") from None
    enable, file_id, delay = values
    if enable not in (0, 1) or not -1 <= file_id <= 14 or not 0 <= delay <= 2**31 - 1:
        raise RecordingChangeError("Invalid automatic reply metadata")
    if info.findtext("channelId") not in (None, str(channel)):
        raise RecordingChangeError("Automatic reply belongs to a different camera channel")
    return enable, file_id, delay


def _selected(state, selection):
    record = next((item for item in state.custom.files if item.id == selection.file_id), None)
    native = next((item for item in state.native.files if item.id == selection.file_id), None)
    if record is None or native is None or record.type != "reply":
        raise RecordingChangeError("Choose an existing custom recording; factory recordings cannot be changed")
    if recording_fingerprint(state, native) != selection.fingerprint:
        raise RecordingChangeError("This recording changed or its slot was reused; browse the camera again")
    if not isinstance(record.external_id, str):
        raise RecordingChangeError("The recording UUID is unknown; its identity cannot be preserved")
    return record


def _verify(before, after, file_id, new_name):
    if before.profile != after.profile or before.custom.max_files != after.custom.max_files:
        raise RecordingChangeError("Camera identity or recording capacity changed during verification")
    for field in ("custom", "native"):
        old = getattr(before, field)
        actual = {item.id: item for item in getattr(after, field).files}
        expected = {
            item.id: replace(item, name=new_name) if item.id == file_id else item
            for item in old.files
            if item.id != file_id or new_name is not None
        }
        if actual != expected:
            raise RecordingChangeError(
                "The change or preservation of other recordings could not be verified; inspect the inventory"
            )


async def async_edit_recording(
    connect,
    channel: int,
    selection: RecordingSelection,
    *,
    action: str,
    new_name: str | None = None,
    confirm_delete: bool = False,
    device_write_authorized: bool = False,
    before_write=None,
) -> RecordingChangeReceipt:
    """Caller holds the physical-camera lock; every context owns a distinct Host."""
    if device_write_authorized is not True:
        raise RecordingChangeError("An explicit recording action is required for camera writes")
    if (
        type(channel) is not int
        or channel != 0
        or not isinstance(selection, RecordingSelection)
        or type(selection.file_id) is not int
        or not 3 <= selection.file_id <= 14
        or action not in ("rename", "delete")
    ):
        raise RecordingChangeError("Choose a custom recording on the verified camera; factory recordings are protected")
    if action == "rename":
        validate_recording_name(new_name)
    elif confirm_delete is not True or new_name is not None:
        raise RecordingChangeError("Confirm deletion of the selected custom recording")
    attempted = acknowledged = False
    send_error = None
    try:
        async with asyncio.timeout(90):
            async with connect() as api:
                before = await _read_state(api, channel)
                record = _selected(before, selection)
                if action == "rename" and any(
                    item.id != record.id and item.name == new_name for item in before.native.files
                ):
                    raise RecordingChangeError("Another recording already has that name")
                if action == "rename" and record.name == new_name:
                    return RecordingChangeReceipt(
                        action, record.id, record.name, new_name, False, False, False, "unchanged"
                    )
                pin = _PinnedConnection.capture(api.baichuan)
                auto_reply = await _read_auto_reply(api, channel) if action == "delete" else None
                if auto_reply is not None and auto_reply[1] == record.id:
                    raise RecordingChangeError(
                        "This recording is the configured automatic reply; choose a different reply in Reolink first"
                    )
                pin.check(api.baichuan, "recording preflight")
                if before_write is not None:
                    before_write()
                original_api, original_bc, original_transport = api, api.baichuan, pin.transport
                attempted = True
                try:
                    await async_change_recording(
                        api.baichuan,
                        channel,
                        record.id,
                        new_name=new_name,
                        external_id=record.external_id,
                        device_write_authorized=True,
                        pin=pin,
                    )
                    acknowledged = True
                except Exception as error:
                    # A missing/bad ACK never triggers another mutation. Only
                    # exact read-back on a fresh session can reconcile success.
                    send_error = error
                if acknowledged:
                    _verify(before, await _read_state(api, channel), record.id, new_name)
                    if auto_reply is not None and await _read_auto_reply(api, channel) != auto_reply:
                        raise RecordingChangeError(
                            "Automatic reply settings changed during deletion; inspect the camera"
                        )
            async with connect() as verification_api:
                if verification_api is original_api or verification_api.baichuan is original_bc:
                    raise RecordingChangeError("Verification requires a fresh independent camera session")
                state = await _read_state(verification_api, channel)
                transport = getattr(getattr(verification_api.baichuan, "_connection", None), "_transport", None)
                if transport is original_transport:
                    raise RecordingChangeError("Verification reused the mutation transport")
                _verify(before, state, record.id, new_name)
                if auto_reply is not None and await _read_auto_reply(verification_api, channel) != auto_reply:
                    raise RecordingChangeError("Automatic reply settings changed during deletion; inspect the camera")
            return RecordingChangeReceipt(
                action,
                record.id,
                record.name,
                new_name,
                True,
                acknowledged,
                True,
                "renamed_verified_after_reconnect" if action == "rename" else "deleted_verified_after_reconnect",
            )
    except RecordingChangeError as error:
        error.outcome_uncertain = attempted
        code = getattr(send_error, "rspCode", None)
        error.rspCode = code if type(code) is int else None
        raise
    except Exception as error:
        code = getattr(send_error if send_error is not None else error, "rspCode", None)
        raise RecordingChangeError(
            f"Recording {action}/verification failed ({type(error).__name__}); inspect the inventory before retrying",
            outcome_uncertain=attempted,
            status=code if type(code) is int else None,
        ) from None
