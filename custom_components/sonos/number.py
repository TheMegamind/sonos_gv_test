"""Entity representing a Sonos number control."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import cast

from soco.exceptions import SoCoException

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SONOS_CREATE_LEVELS, SONOS_SPEAKER_ACTIVITY, SONOS_STATE_UPDATED
from .entity import SonosEntity
from .helpers import SonosConfigEntry, soco_error
from .speaker import SonosSpeaker

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

_LOGGER = logging.getLogger(__name__)


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

        # Standard SoCo‑backed level controls
        for level_type, valid_range in available_features:
            _LOGGER.debug(
                "Creating %s number control on %s", level_type, speaker.zone_name
            )
            entities.append(
                SonosLevelEntity(speaker, config_entry, level_type, valid_range)
            )

        # Group volume slider (0.0–1.0) with native Sonos semantics
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
        """Set a new level."""
        from_number = LEVEL_FROM_NUMBER.get(self.level_type, int)
        setattr(self.soco, self.level_type, from_number(value))

    @property
    def native_value(self) -> float:
        """Return the current value."""
        to_number = LEVEL_TO_NUMBER.get(self.level_type, int)
        return cast(float, to_number(getattr(self.speaker, self.level_type)))


class SonosGroupVolumeEntity(SonosEntity, NumberEntity):
    """Group volume control (0.0–1.0) for the Sonos group of this player.

    Semantics:
      - If grouped: read/write GroupRenderingControl.*GroupVolume on the coordinator.
      - If not grouped: read/write RenderingControl Master volume on this player.
    """

    _attr_translation_key = "group_volume"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1.0
    _attr_native_step = 0.01
    _attr_mode = NumberMode.SLIDER

    def __init__(self, speaker: SonosSpeaker, config_entry: SonosConfigEntry) -> None:
        """Initialize the Sonos group volume number entity."""
        super().__init__(speaker, config_entry)
        self._attr_unique_id = f"{self.soco.uid}-group_volume"
        self._coord_uid: str | None = None
        self._group_uid: str | None = None
        self._unsub_coord: Callable[[], None] | None = None
        self._unsub_group_activity: Callable[[], None] | None = None
        self._unsub_stop: Callable[[], None] | None = None

        # Per-entity cached value (0.0–1.0) — replaces previous shared cache
        self._value: float | None = None
        self._refresh_task = None  # asyncio.Task | None (keep as object to avoid import)

    # ---------- Helpers ----------

    def _current_group_uid(self) -> str:
        return self.soco.group.uid

    def _is_grouped(self) -> bool:
        try:
            return len(self.soco.group.members) > 1
        except Exception:
            return False

    def _cancel_refresh(self) -> None:
        task = self._refresh_task
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass
        self._refresh_task = None

    # ---------- Reading / writing ----------

    @property
    def available(self) -> bool:
        """Available whenever the player is online."""
        return bool(self.speaker.available)

    @property
    def native_value(self) -> float | None:
        """Return our last known value; we refresh on activity."""
        return self._value

    @soco_error()
    def set_native_value(self, value: float) -> None:
        """Set group volume (or player volume if solo) with optimistic update."""
        level = max(0.0, min(1.0, float(value)))
        vol_int = int(round(level * 100))

        try:
            if self._is_grouped():
                self.soco.group.volume = vol_int
                _LOGGER.debug(
                    "GV write: grouped zone=%s coord=%s vol=%s",
                    self.speaker.zone_name,
                    (self.speaker.coordinator or self.speaker).zone_name,
                    vol_int,
                )
            else:
                self.soco.volume = vol_int
                _LOGGER.debug(
                    "GV write: SOLO zone=%s vol=%s", self.speaker.zone_name, vol_int
                )
        except (SoCoException, OSError) as err:
            _LOGGER.debug("GV write failed on %s: %s", self.speaker.zone_name, err)
            return

        # Optimistic: update local value and push UI
        self._value = level
        self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

        # Confirm with a fresh read soon
        self.hass.loop.call_soon_threadsafe(
            self.hass.async_create_task, self._async_refresh_from_device()
        )

    async def _async_fallback_poll(self) -> None:
        """Poll if subscriptions aren’t working."""
        await self._async_refresh_from_device()

    async def _async_refresh_from_device(self) -> None:
        """Fetch current (group or player) volume and update _value."""
        gid_actual = self.soco.group.uid

        def _get() -> int | None:
            try:
                if self._is_grouped():
                    return self.soco.group.volume
                return self.soco.volume
            except (SoCoException, OSError) as err:
                _LOGGER.debug(
                    "GV refresh read failed for %s: %s", self.speaker.zone_name, err
                )
                return None

        vol = await self.hass.async_add_executor_job(_get)
        if vol is None:
            return

        _LOGGER.debug(
            "GV refresh: zone=%s grouped=%s gid=%s vol=%s",
            self.speaker.zone_name,
            self._is_grouped(),
            gid_actual,
            vol,
        )

        new = max(0.0, min(1.0, vol / 100.0))
        if self._value != new:
            self._value = new
            self.async_write_ha_state()

    # ---------- Push wiring for responsiveness ----------

    async def async_added_to_hass(self) -> None:
        """Subscribe to signals and seed value."""
        await super().async_added_to_hass()

        self._coord_uid = (self.speaker.coordinator or self.speaker).uid
        self._group_uid = self._current_group_uid()

        # Any speaker activity (RenderingControl, GroupRenderingControl, ZGT, discovery)
        self._unsub_group_activity = async_dispatcher_connect(
            self.hass, SONOS_SPEAKER_ACTIVITY, self._on_any_activity
        )
        self.async_on_remove(self._unsub_group_activity)

        # Coordinator state updates
        self._bind_coordinator_state(self._coord_uid)

        # Cancel on HA stop
        @callback
        def _on_stop(_event) -> None:
            self._cancel_refresh()

        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _on_stop
        )
        self.async_on_remove(self._unsub_stop)

        # Seed value:
        # - If solo: use current player volume already tracked on speaker
        # - Else: fetch group volume once
        if not self._is_grouped():
            try:
                # speaker.volume is kept up-to-date by RenderingControl events
                vol = int(self.speaker.volume or 0)
                self._value = max(0.0, min(1.0, vol / 100.0))
            except Exception:
                self._value = None
        await self._async_refresh_from_device()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listeners and pending work to avoid teardown hangs."""
        await super().async_will_remove_from_hass()

        if self._unsub_group_activity is not None:
            self._unsub_group_activity()
            self._unsub_group_activity = None
        if self._unsub_coord is not None:
            self._unsub_coord()
            self._unsub_coord = None
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None

        self._cancel_refresh()

    def _bind_coordinator_state(self, coord_uid: str) -> None:
        """(Re)bind a single STATE_UPDATED listener for the given coordinator."""
        if self._unsub_coord is not None:
            self._unsub_coord()
            self._unsub_coord = None
        self._unsub_coord = async_dispatcher_connect(
            self.hass,
            f"{SONOS_STATE_UPDATED}-{coord_uid}",
            self._on_coord_state_updated,
        )
        self.async_on_remove(self._unsub_coord)

    @callback
    def _on_coord_state_updated(self, *_: object) -> None:
        """Coordinator state changed — refresh once."""
        self.hass.async_create_task(self._async_refresh_from_device())

    @callback
    def _on_any_activity(self, *_: object) -> None:
        """Any speaker activity — rebind on coord/group change and refresh."""
        new_coord_uid = (self.speaker.coordinator or self.speaker).uid
        new_group_uid = self._current_group_uid()

        # Coordinator change
        if new_coord_uid != self._coord_uid:
            self._coord_uid = new_coord_uid
            self._bind_coordinator_state(new_coord_uid)

        # Group topology change (join/leave)
        if new_group_uid != self._group_uid:
            self._group_uid = new_group_uid
            # Becoming SOLO: seed from local player volume immediately
            if not self._is_grouped():
                try:
                    vol = int(self.speaker.volume or 0)
                    self._value = max(0.0, min(1.0, vol / 100.0))
                    self.async_write_ha_state()
                except Exception:
                    self._value = None
            else:
                # Joined a group: drop any solo value; will fetch group value below
                self._value = None
                self.async_write_ha_state()

        # Refresh (grouped or solo)
        self.hass.async_create_task(self._async_refresh_from_device())
