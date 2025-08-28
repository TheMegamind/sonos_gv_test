"""Entity representing a Sonos number control."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import cast

from soco.exceptions import SoCoException
from soco.core import SoCo

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SONOS_CREATE_LEVELS, SONOS_SPEAKER_ACTIVITY
from .entity import SonosEntity
from .helpers import SonosConfigEntry, soco_error
from .speaker import SonosSpeaker

_LOGGER = logging.getLogger(__name__)

# (LEVEL_TYPES and other top-level code remains the same)
LEVEL_TYPES = {
    "audio_delay": (0, 5), "bass": (-10, 10), "balance": (-100, 100),
    "treble": (-10, 10), "sub_crossover": (50, 110), "sub_gain": (-15, 15),
    "surround_level": (-15, 15), "music_surround_level": (-15, 15),
}
type SocoFeatures = list[tuple[str, tuple[int, int]]]

SONOS_GROUP_VOLUME_REFRESHED = "sonos_group_volume_refreshed"

def _gv_signal(group_uid: str) -> str:
    return f"{SONOS_GROUP_VOLUME_REFRESHED}-{group_uid}"

def _balance_to_number(state: tuple[int, int]) -> float:
    left, right = state
    if max(right, left) == 0: return 0.0
    return (right - left) * 100 / max(right, left)

def _balance_from_number(value: float) -> tuple[int, int]:
    left = min(100, 100 - int(value))
    right = min(100, int(value) + 100)
    return left, right

LEVEL_TO_NUMBER = {"balance": _balance_to_number}
LEVEL_FROM_NUMBER = {"balance": _balance_from_number}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SonosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Sonos number platform from a config entry."""
    def available_soco_attributes(speaker: SonosSpeaker) -> SocoFeatures:
        features: SocoFeatures = []
        for level_type, valid_range in LEVEL_TYPES.items():
            if (state := getattr(speaker.soco, level_type, None)) is not None:
                setattr(speaker, level_type, state)
                features.append((level_type, valid_range))
        return features

    async def _async_create_entities(speaker: SonosSpeaker) -> None:
        entities = []
        available_features = await hass.async_add_executor_job(
            available_soco_attributes, speaker
        )
        for level_type, valid_range in available_features:
            entities.append(
                SonosLevelEntity(speaker, config_entry, level_type, valid_range)
            )
        entities.append(SonosGroupVolumeEntity(speaker, config_entry))
        async_add_entities(entities)

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SONOS_CREATE_LEVELS, _async_create_entities)
    )

class SonosLevelEntity(SonosEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    def __init__(
        self, speaker: SonosSpeaker, config_entry: SonosConfigEntry,
        level_type: str, valid_range: tuple[int, int],
    ) -> None:
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-{level_type}"
        self._attr_translation_key = level_type
        self.level_type = level_type
        self._attr_native_min_value, self._attr_native_max_value = valid_range
    async def _async_fallback_poll(self) -> None:
        await self.hass.async_add_executor_job(self.poll_state)
    @soco_error()
    def poll_state(self) -> None:
        state = getattr(self.soco, self.level_type)
        setattr(self.speaker, self.level_type, state)
    @soco_error()
    def set_native_value(self, value: float) -> None:
        from_number = LEVEL_FROM_NUMBER.get(self.level_type, int)
        setattr(self.soco, self.level_type, from_number(value))
    @property
    def native_value(self) -> float:
        to_number = LEVEL_TO_NUMBER.get(self.level_type, int)
        return cast(float, to_number(getattr(self.speaker, self.level_type)))

class SonosGroupVolumeEntity(SonosEntity, NumberEntity):
    _attr_translation_key = "group_volume"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-group_volume"
        self._group_uid: str | None = None
        self._unsubscribe_activity: Callable[[], None] | None = None
        self._unsubscribe_gv_signal: Callable[[], None] | None = None
        self._value: int | None = None

    def _coordinator_soco(self) -> SoCo:
        return (self.speaker.coordinator or self.speaker).soco

    def _current_group_uid(self) -> str | None:
        try:
            return self._coordinator_soco().group.uid
        except (SoCoException, OSError):
            return None

    def _is_grouped(self) -> bool:
        try:
            return len(self._coordinator_soco().group.members) > 1
        except (SoCoException, OSError):
            return False

    def _is_coordinator(self) -> bool:
        return (self.speaker.coordinator or self.speaker).uid == self.speaker.uid

    @property
    def available(self) -> bool:
        return self.speaker.available

    @property
    def native_value(self) -> float | None:
        return None if self._value is None else float(self._value)

    @soco_error()
    def set_native_value(self, value: float) -> None:
        level = int(round(value))
        if self._is_grouped():
            self._coordinator_soco().group.volume = level
        else:
            self.soco.volume = level

    async def _async_refresh_from_device(self) -> None:
        is_grouped = self._is_grouped()
        is_coordinator = self._is_coordinator()
        vol: int | None = None
        try:
            if not is_grouped:
                vol = await self.hass.async_add_executor_job(getattr, self.soco, "volume")
            elif is_coordinator:
                vol = await self.hass.async_add_executor_job(getattr, self._coordinator_soco().group, "volume")
            else:
                # Members do not poll, they wait for the coordinator's fan-out
                return
        except (SoCoException, OSError) as err:
            _LOGGER.debug("Error fetching volume for %s: %s", self.entity_id, err)

        if vol is None:
            return

        vol = int(vol)
        if self._value != vol:
            self._value = vol
            self.async_write_ha_state()

        if is_grouped and is_coordinator:
            if (group_uid := self._current_group_uid()):
                async_dispatcher_send(self.hass, _gv_signal(group_uid), vol)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The only listener needed at startup. It will handle the rest.
        self._unsubscribe_activity = async_dispatcher_connect(
            self.hass, SONOS_SPEAKER_ACTIVITY, self._on_any_activity
        )
        # Trigger the initial setup and refresh
        self.hass.async_create_task(self._on_any_activity())

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsubscribe_activity:
            self._unsubscribe_activity()
        if self._unsubscribe_gv_signal:
            self._unsubscribe_gv_signal()

    def _update_listeners(self) -> None:
        """Centralized method to set up listeners based on current topology."""
        new_group_uid = self._current_group_uid()
        if self._group_uid == new_group_uid and self._unsubscribe_gv_signal:
            # No change needed, already subscribed to the correct group signal
            return

        # Unsubscribe from the old group signal, if any
        if self._unsubscribe_gv_signal:
            self._unsubscribe_gv_signal()
            self._unsubscribe_gv_signal = None

        self._group_uid = new_group_uid
        if self._group_uid:
            # Subscribe to the new group's fan-out signal
            self._unsubscribe_gv_signal = async_dispatcher_connect(
                self.hass, _gv_signal(self._group_uid), self._on_group_volume_fanned
            )

    @callback
    def _on_group_volume_fanned(self, level: int) -> None:
        """Callback for when coordinator pushes a group volume update."""
        if not self._is_grouped():
            return
        if self._value != level:
            self._value = level
            self.async_write_ha_state()

    @callback
    async def _on_any_activity(self, *_: object) -> None:
        """
        The single entry point for all state changes.
        1. Update listeners to match the new topology.
        2. Trigger a refresh if this entity is responsible for polling.
        """
        self._update_listeners()
        if not self._is_grouped() or self._is_coordinator():
            await self._async_refresh_from_device()
