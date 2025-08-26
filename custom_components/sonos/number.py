"""Entity representing a Sonos number control."""

from __future__ import annotations

from collections.abc import Callable
import logging
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

from .const import SONOS_CREATE_LEVELS, SONOS_SPEAKER_ACTIVITY, SONOS_STATE_UPDATED
from .entity import SonosEntity
from .helpers import SonosConfigEntry, soco_error
from .speaker import SonosSpeaker

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Standard level types (direct SoCo attributes on a player)
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# Group-volume fan-out (no caching; just notify peers with fresh value)
# ---------------------------------------------------------------------
_GV_SIGNAL_BASE = "sonos.group_volume.refreshed"  # suffixed by group_uid

# CHANGE 1: Define a new signal for members to request a refresh from the coordinator
_GV_REFRESH_REQUEST_SIGNAL_BASE = "sonos.group_volume.request_refresh"


def _gv_signal(gid: str) -> str:
    return f"{_GV_SIGNAL_BASE}-{gid}"

# CHANGE 1 (continued): Helper function for the new signal
def _gv_refresh_request_signal(coord_uid: str) -> str:
    return f"{_GV_REFRESH_REQUEST_SIGNAL_BASE}-{coord_uid}"


def _balance_to_number(state: tuple[int, int]) -> float:
    """Represent a balance measure returned by SoCo as a number."""
    left, right = state
    return (right - left) * 100 // max(right, left)


def _balance_from_number(value: float) -> tuple[int, int]:
    """Convert a balance value from -100 to 100 into SoCo format."""
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


class SonosLevelEntity(SonosEntity, NumberEntity):
    """Representation of a Sonos level entity."""
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        speaker: SonosSpeaker,
        config_entry: SonosConfigEntry,
        level_type: str,
        valid_range: tuple[int, int],
    ) -> None:
        """Initialize the level entity."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-{level_type}"
        self._attr_translation_key = level_type
        self.level_type = level_type
        self._attr_native_min_value, self._attr_native_max_value = valid_range

    async def _async_fallback_poll(self) -> None:
        """Poll the value if subscriptions are not working."""
        await self.hass.async_add_executor_job(self.poll_state)

    @soco_error()
    def poll_state(self) -> None:
        """Poll the device for the current state."""
        state = getattr(self.soco, self.level_type)
        setattr(self.speaker, self.level_type, state)

    @soco_error()
    def set_native_value(self, value: float) -> None:
        """Set a new value."""
        from_number = LEVEL_FROM_NUMBER.get(self.level_type, int)
        setattr(self.soco, self.level_type, from_number(value))

    @property
    def native_value(self) -> float:
        """Return the current value."""
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
        self._coord_uid: str | None = None
        self._group_uid: str | None = None
        self._unsub_coord: Callable[[], None] | None = None
        self._unsub_activity: Callable[[], None] | None = None
        self._unsub_stop: Callable[[], None] | None = None
        self._unsub_gv_signal: Callable[[], None] | None = None
        self._refresh_unsub: Callable[[], None] | None = None
        self._unsub_gv_refresh_request: Callable[[], None] | None = None
        self._value: int | None = None

    def _schedule_refresh(self, delay: float = 0.5) -> None:
        """Schedule a debounced refresh of the group volume from the device."""
        if self._refresh_unsub:
            self._refresh_unsub()

        @callback
        def _async_refresh_task() -> None:
            self._refresh_unsub = None
            self.hass.async_create_task(self._async_refresh_from_device())

        self._refresh_unsub = self.hass.loop.call_later(delay, _async_refresh_task)

    def _current_group_uid(self) -> str:
        return self.soco.group.uid

    def _is_grouped(self) -> bool:
        """Return True if the player is in a group with 2+ members."""
        group = getattr(self.soco, "group", None)
        if group is None:
            return False
        members = getattr(group, "members", None)
        return bool(members and len(members) > 1)

    def _coordinator_soco(self):
        return (self.speaker.coordinator or self.speaker).soco

    def _is_coordinator(self) -> bool:
        """Return True if this entity controls the group coordinator."""
        return self._coordinator_soco().uid == self.soco.uid

    @property
    def available(self) -> bool:
        return bool(self.speaker.available)

    @property
    def native_value(self) -> float | None:
        return None if self._value is None else float(self._value)

    @soco_error()
    def set_native_value(self, value: float) -> None:
        """Set group volume (0–100). If not grouped, set player volume."""
        level = int(round(max(0.0, min(100.0, float(value)))))
        if self._is_grouped():
            try:
                coord = self._coordinator_soco()
                coord.group.volume = level
            finally:
                self.hass.loop.call_soon_threadsafe(
                    self.hass.async_create_task, self._async_refresh_from_device()
                )
        else:
            self.soco.volume = level
            self._value = level
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    async def _async_fallback_poll(self) -> None:
        await self._async_refresh_from_device()

    async def _async_refresh_from_device(self) -> None:
        """Read the *native* volume (group or player) and propagate to peers."""
        gid_actual = self.soco.group.uid

        def _get() -> int | None:
            try:
                if self._is_grouped():
                    return int(self.soco.group.volume)
                return int(self.soco.volume)
            except (SoCoException, OSError) as err:
                _LOGGER.debug(
                    "Failed to read volume for %s: %s", self.speaker.zone_name, err
                )
                return None
        vol = await self.hass.async_add_executor_job(_get)
        if vol is None:
            return
        changed = self._value != vol
        self._value = vol
        self.async_write_ha_state()

        if (
            self._is_grouped()
            and gid_actual
            and self._is_coordinator()
            and self._group_uid == gid_actual
        ):
            async_dispatcher_send(self.hass, _gv_signal(gid_actual), vol)
            if changed:
                _LOGGER.debug(
                    "GV refresh: zone=%s grouped=True gid=%s vol=%s (authoritative)",
                    self.speaker.zone_name, gid_actual, vol
                )
        elif changed:
            _LOGGER.debug(
                "GV refresh: zone=%s grouped=%s vol=%s",
                self.speaker.zone_name, bool(gid_actual), vol
            )

    async def async_added_to_hass(self) -> None:
        """Finish setup: bind signals and perform an initial refresh."""
        await super().async_added_to_hass()
        self._coord_uid = (self.speaker.coordinator or self.speaker).uid
        self._group_uid = self._current_group_uid()

        self._unsub_activity = async_dispatcher_connect(
            self.hass, SONOS_SPEAKER_ACTIVITY, self._on_any_activity
        )
        self.async_on_remove(self._unsub_activity)

        self._bind_coordinator_state(self._coord_uid)

        # CHANGE 2: Add listener for the new refresh request signal
        # This entity will action the request if it is a coordinator
        self._unsub_gv_refresh_request = async_dispatcher_connect(
            self.hass,
            _gv_refresh_request_signal(self.soco.uid),
            self._on_gv_refresh_request,
        )
        self.async_on_remove(self._unsub_gv_refresh_request)

        if self._group_uid:
            self._unsub_gv_signal = async_dispatcher_connect(
                self.hass, _gv_signal(self._group_uid), self._on_group_volume_fanned
            )
            self.async_on_remove(self._unsub_gv_signal)

        @callback
        def _on_stop(_event) -> None:
            return

        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _on_stop
        )
        self.async_on_remove(self._unsub_stop)
        await self._async_refresh_from_device()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up signal subscriptions on removal."""
        if self._refresh_unsub:
            self._refresh_unsub()
            self._refresh_unsub = None
        await super().async_will_remove_from_hass()
        if self._unsub_gv_signal:
            self._unsub_gv_signal()
        if self._unsub_coord:
            self._unsub_coord()
        if self._unsub_activity:
            self._unsub_activity()
        if self._unsub_stop:
            self._unsub_stop()
        # Ensure new listener is cleaned up
        if self._unsub_gv_refresh_request:
            self._unsub_gv_refresh_request()

    def _bind_coordinator_state(self, coord_uid: str) -> None:
        if self._unsub_coord is not None:
            self._unsub_coord()
        self._unsub_coord = async_dispatcher_connect(
            self.hass, f"{SONOS_STATE_UPDATED}-{coord_uid}", self._on_coord_state_updated
        )
        self.async_on_remove(self._unsub_coord)

    @callback
    def _on_coord_state_updated(self, *_: object) -> None:
        self._schedule_refresh()

    # CHANGE 2 (continued): Add the handler for the refresh request
    @callback
    def _on_gv_refresh_request(self) -> None:
        """Handle a refresh request from a group member."""
        if self._is_coordinator():
            _LOGGER.debug("GV refresh request received by coordinator %s", self.speaker.zone_name)
            self._schedule_refresh()

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
        """Any speaker activity — rebind if needed, then refresh or request refresh."""
        new_coord_uid = (self.speaker.coordinator or self.speaker).uid
        topology_changed = False
        if new_coord_uid != self._coord_uid:
            topology_changed = True
            self._coord_uid = new_coord_uid
            self._bind_coordinator_state(new_coord_uid)

        new_group_uid = self._current_group_uid()
        if new_group_uid != self._group_uid:
            topology_changed = True
            if self._unsub_gv_signal is not None:
                self._unsub_gv_signal()
            self._group_uid = new_group_uid
            if self._group_uid:
                self._unsub_gv_signal = async_dispatcher_connect(
                    self.hass, _gv_signal(self._group_uid), self._on_group_volume_fanned
                )
                self.async_on_remove(self._unsub_gv_signal)

        # CHANGE 3: The core logic change. Members now request a refresh
        # instead of performing one themselves.
        if self._is_coordinator() or not self._is_grouped():
            # Coordinators and ungrouped players handle their own refreshes
            delay = 1.0 if topology_changed else 0.5
            self._schedule_refresh(delay=delay)
        else:
            # Group members ask the coordinator to perform the refresh
            _LOGGER.debug(
                "GV activity on member %s, requesting refresh from coordinator %s",
                self.speaker.zone_name,
                new_coord_uid,
            )
            async_dispatcher_send(
                self.hass, _gv_refresh_request_signal(new_coord_uid)
            )
