"""Custom recording capacity, including occupied and available native slots."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .stored_audio import StoredAudioError


async def async_setup_entry(hass, entry, async_add_entities):
    entities = []
    for library in entry.runtime_data.values():
        try:
            if library.profile is None:
                continue
        except StoredAudioError:
            continue
        # A failed catalog read leaves capacity unavailable; the status sensor stays visible.
        await library.async_refresh()
        official = hass.config_entries.async_get_entry(library.entry_id)
        entities.extend(
            (
                CustomRecordingSlots(library, official.title),
                CustomRecordingSlots(library, official.title, free=True),
                AudioUploadStatus(library, official.title),
            )
        )
    async_add_entities(entities)


class CustomRecordingSlots(CoordinatorEntity, SensorEntity):
    _attr_icon = "mdi:message-audio"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "recordings"

    def __init__(self, coordinator, title, *, free=False):
        super().__init__(coordinator)
        self._free = free
        key = "free_recording_slots" if free else "custom_recordings"
        label = "free recording slots" if free else "custom recordings"
        self._attr_name = f"Reolink Audio Library {title} {label}"
        self._attr_unique_id = f"{DOMAIN}:{coordinator.entry_id}:{coordinator.channel}:{key}"
        self._attr_native_unit_of_measurement = "slots" if free else "recordings"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        used = len(self.coordinator.data.custom.files)
        capacity = self.coordinator.data.custom.max_files
        return max(0, capacity - used) if self._free and capacity is not None else used

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}
        data = self.coordinator.data.as_dict()
        return {
            **{key: data[key] for key in ("max_files", "used_slots", "free_slots", "slots", "max_duration")},
            "last_recording_change": self.coordinator.last_recording_change,
            "last_recording_error": self.coordinator.last_recording_error,
        }


class AudioUploadStatus(CoordinatorEntity, SensorEntity):
    """Report operation progress even when the catalog is unavailable."""

    _attr_icon = "mdi:cloud-upload-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, title):
        super().__init__(coordinator)
        self._attr_name = f"Reolink Audio Library {title} upload status"
        self._attr_unique_id = f"{DOMAIN}:{coordinator.entry_id}:{coordinator.channel}:upload_status"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return self.coordinator.upload_status

    @property
    def extra_state_attributes(self):
        return {
            "last_uploaded_recording": self.coordinator.last_upload_result,
            "last_upload_error": self.coordinator.last_upload_error,
            "last_recording_change": self.coordinator.last_recording_change,
            "last_recording_error": self.coordinator.last_recording_error,
        }
