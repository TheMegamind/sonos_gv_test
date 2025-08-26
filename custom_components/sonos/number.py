"""Entity representing a Sonos number control."""

from __future__ import annotations

from collections.abc import Callable
import logging
import asyncio # Import asyncio for the delay
from typing import cast

from soco.exceptions import SoCoException

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

# (LEVEL_TYPES and balance functions remain unchanged)
# ...
LEVEL_TYPES = {
    "audio_delay": (0, 5),
    "bass": (-10, 10),
    "balance": (-100, 100),
    "treble": (-10, 10),
    "sub_crossover": (50, 110),
    "sub_gain": (-15, 15),
    "surround_level": (-15, 15),
    "music_surround_level": (-15, 15),
}
type SocoFeatures = list[tuple[str, tuple[int, int]]]

_GV_SIGNAL_BASE = "sonos.group_volume.refreshed"  # suffixed by group_uid

def _gv_signal(gid: str) -> str:
    return f"{_GV_SIGNAL_BASE}-{gid}"

def _balance_to_number(state: tuple[int, int]) -> float:
    left, right = state
    if max(right, left) == 0:
        return 0.0
    return (right - left) * 100 / max(right, left)

def _balance_from_number(value: float) -> tuple[int, int]:
    left = min(100, 100 - int(value))
    right = min(100, int(value) + 100)
    return left, right

LEVEL_TO_NUMBER = {"balance": _balance_to_number}
LEVEL_FROM_NUMBER = {"balance": _balance_from_number}


# (async_setup_entry remains unchanged)
# ...
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
        entities: list[NumberEntity] = []
        available_features = await hass.async_add_executor_job(
            available_soco_attributes, speaker
        )
        for level_type, valid_range in available_features:
            _LOGGER.debug(
                "Creating %s number control on %s", level_type, speaker.zone_name
            )
            entities.append(
                SonosLevelEntity(speaker, config_entry, level_type, valid_range)
            )
        entities.append(SonosGroupVolumeEntity(speaker, config_entry))
        async_add_entities(entities)

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SONOS_CREATE_LEVELS, _async_create_entities)
    )

# (SonosLevelEntity class remains unchanged)
# ...
class SonosLevelEntity(SonosEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    def __init__(
        self,
        speaker: SonosSpeaker,
        config_entry: SonosConfigEntry,
        level_type: str,
        valid_range: tuple[int, int],
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
    """Native Sonos group volume for the player's current group (0–100)."""

    _attr_translation_key = "group_volume"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        """Initialize the Sonos group volume number entity."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-group_volume"
        self._group_uid: str | None = None
        self._unsub_activity: Callable[[], None] | None = None
        self._unsub_stop: Callable[[], None] | None = None
        self._unsub_gv_signal: Callable[[], None] | None = None
        self._value: int | None = None

    # ---------------------- helpers ----------------------

    def _current_group_uid(self) -> str | None:
        group = getattr(self.soco, "group", None)
        return getattr(group, "uid", None)

    def _is_grouped(self) -> bool:
        """Return True if the player is in a group with 2+ members."""
        group = getattr(self.soco, "group", None)
        if group is None:
            return False
        members = getattr(group, "members", None)
        return bool(members and len(members) > 1)

    def _is_coordinator(self) -> bool:
        return (self.speaker.coordinator or self.speaker).uid == self.speaker.uid

    def _coordinator_soco(self):
        return (self.speaker.coordinator or self.speaker).soco

    # ------------------ HA entity bits -------------------

    @property
    def available(self) -> bool:
        return bool(self.speaker.available)

    @property
    def native_value(self) -> float | None:
        return None if self._value is None else float(self._value)

    # ------------------ read / write ---------------------

    @soco_error()
    def set_native_value(self, value: float) -> None:
        """Set group volume (0–100). If not grouped, set player volume."""
        level = int(round(max(0.0, min(100.0, float(value)))))

        if self._is_grouped():
            self._coordinator_soco().group.volume = level
            # The activity from setting the volume will trigger the refresh cycle
        else:
            self.soco.volume = level
            # Update value immediately for ungrouped players for responsiveness
            self._value = level
            self.async_write_ha_state()

    async def _async_fallback_poll(self) -> None:
        await self._async_refresh_from_device()

    async def _async_refresh_from_device(self) -> None:
        """Refresh volume from the device and fan out if coordinator."""
        # Add a brief delay to allow the Sonos system to settle its state.
        await asyncio.sleep(0.2)

        is_grouped = self._is_grouped()
        is_coordinator = self._is_coordinator()

        vol: int | None = None
        try:
            if is_grouped:
                if is_coordinator:
                    vol = await self.hass.async_add_executor_job(
                        getattr, self._coordinator_soco().group, "volume"
                    )
            else:
                # Ungrouped, so get own volume
                vol = await self.hass.async_add_executor_job(
                    getattr, self.soco, "volume"
                )
        except (SoCoException, OSError) as err:
            _LOGGER.debug(
                "Failed to read volume for %s: %s", self.speaker.zone_name, err
            )

        if vol is None:
            return

        vol = int(vol)
        if self._value != vol:
            self._value = vol
            self.async_write_ha_state()

        # If we are the coordinator, fan-out the authoritative value to members
        if is_grouped and is_coordinator:
            gid = self._current_group_uid()
            if gid:
                async_dispatcher_send(self.hass, _gv_signal(gid), vol)

    # ------------------ wiring & lifecycle ----------------

    async def async_added_to_hass(self) -> None:
        """Finish setup and bind the single activity listener."""
        await super().async_added_to_hass()
        self._group_uid = self._current_group_uid()

        # The single, global listener that triggers a re-evaluation for all entities
        self._unsub_activity = async_dispatcher_connect(
            self.hass, SONOS_SPEAKER_ACTIVITY, self._on_any_activity
        )

        # Initial setup of listener for fanned-out group volume
        self._rebind_group_listener()
        
        @callback
        def _on_stop(_event) -> None:
            return
        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _on_stop
        )

        # Initial read
        self.hass.async_create_task(self._async_refresh_from_device())

    async def async_will_remove_from_hass(self) -> None:
        """Clean up signal subscriptions on removal."""
        await super().async_will_remove_from_hass()
        if self._unsub_gv_signal:
            self._unsub_gv_signal()
        if self._unsub_activity:
            self._unsub_activity()
        if self._unsub_stop:
            self._unsub_stop()

    # ------------------ signal handlers ------------------

    def _rebind_group_listener(self) -> None:
        """(Re)subscribes this entity to group volume fan-out signals."""
        new_group_uid = self._current_group_uid()
        if new_group_uid == self._group_uid and self._unsub_gv_signal:
            return  # Already subscribed to the correct group

        if self._unsub_gv_signal:
            self._unsub_gv_signal()
            self._unsub_gv_signal = None

        self._group_uid = new_group_uid
        if self._group_uid:
            self._unsub_gv_signal = async_dispatcher_connect(
                self.hass, _gv_signal(self._group_uid), self._on_group_volume_fanned
            )

    @callback
    def _on_group_volume_fanned(self, level: int) -> None:
        """Receive the coordinator's fresh group volume and update instantly."""
        if not self._is_grouped():
            return
        if self._value != level:
            self._value = level
            self.async_write_ha_state()

    @callback
    def _on_any_activity(self, *_: object) -> None:
        """
        Activity detected. Re-evaluate topology and role.
        Coordinators and ungrouped players will trigger their own refresh.
        Members will simply update their listeners and wait.
        """
        self._rebind_group_listener()

        if self._is_coordinator() or not self._is_grouped():
            self.hass.async_create_task(self._async_refresh_from_device())
