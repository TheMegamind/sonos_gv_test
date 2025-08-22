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
    async_dispatcher_send,
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

# -----------------------------------------------------------------------------
# Per-HASS storage (avoids cross-test leaks)
# -----------------------------------------------------------------------------
_CACHE_KEY = "sonos_number.group_cache"          # dict[group_uid, float]
_SCHEDULED_KEY = "sonos_number.group_scheduled"  # set[group_uid]
_TASKS_KEY = "sonos_number.group_tasks"          # dict[group_uid, asyncio.Task]
_SIGNAL_BASE = "sonos-group-volume-updated"      # suffixed by group_uid


def _group_signal(group_uid: str) -> str:
    return f"{_SIGNAL_BASE}-{group_uid}"


def _group_cache(hass: HomeAssistant) -> dict[str, float]:
    return hass.data.setdefault(_CACHE_KEY, {})


def _group_scheduled(hass: HomeAssistant) -> set[str]:
    return hass.data.setdefault(_SCHEDULED_KEY, set())


def _group_tasks(hass: HomeAssistant) -> dict[str, object]:
    # value is an asyncio.Task, but we keep it as object to avoid importing asyncio
    return hass.data.setdefault(_TASKS_KEY, {})


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

        # Group volume slider (0.0–1.0)
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
    """Group volume control (0.0–1.0) for the Sonos group of this player."""

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
        self._unsub_group_signal: Callable[[], None] | None = None
        self._unsub_stop: Callable[[], None] | None = None

    # ---------- Helpers ----------

    def _current_group_uid(self) -> str:
        # Any member can read the group's UID through SoCo
        return self.soco.group.uid

    def _cancel_task(self, gid: str | None) -> None:
        """Cancel and remove any in-flight refresh task for the group."""
        if gid is None:
            return
        tasks = _group_tasks(self.hass)
        task = tasks.pop(gid, None)
        if task is not None:
            try:
                # Task may already be done
                task.cancel()
            except Exception:  # pragma: no cover - very defensive
                pass

    # ---------- Reading / writing ----------

    @property
    def available(self) -> bool:
        """Available whenever the player is online."""
        return bool(self.speaker.available)

    @property
    def native_value(self) -> float | None:
        """Return the group volume from the shared cache (0.0–1.0)."""
        if self._group_uid is None:
            return None
        return _group_cache(self.hass).get(self._group_uid)

    @soco_error()
    def set_native_value(self, value: float) -> None:
        """Set the group volume (0.0–1.0) with optimistic update."""
        level = max(0.0, min(1.0, float(value)))
        # Write to device
        self.soco.group.volume = int(round(level * 100))
        # Optimistic cache + broadcast (thread-safe hop)
        if self._group_uid is not None:
            cache = _group_cache(self.hass)
            cache[self._group_uid] = level
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, _group_signal(self._group_uid)
            )
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    async def _async_fallback_poll(self) -> None:
        """Poll if subscriptions aren’t working."""
        await self._schedule_group_refresh_once()

    async def _async_refresh_from_device(self) -> None:
        """Fetch current group volume from SoCo and update shared cache."""
    
        # (Optional but very helpful while iterating)
        gid_actual = self.soco.group.uid
        coord = (self.speaker.coordinator or self.speaker).soco
        _LOGGER.debug(
            "GV refresh: entity=%s gid_actual=%s coord_ip=%s me_ip=%s",
            self.entity_id, gid_actual, coord.ip_address, self.soco.ip_address
        )
    
        def _get() -> int | None:
            try:
                return self.soco.group.volume
            except (SoCoException, OSError) as err:
                _LOGGER.debug(
                    "Failed to read group volume for %s: %s",
                    self.speaker.zone_name, err,
                )
                return None
    
        vol = await self.hass.async_add_executor_job(_get)
        if vol is None:
            return
    
        new = max(0.0, min(1.0, vol / 100.0))
    
        # Only ever write to the *current* group’s cache to avoid cross‑contamination.
        cache_gid = gid_actual
        cache = _group_cache(self.hass)
    
        # If we got scheduled before a topology change, avoid touching stale group cache
        if self._group_uid and self._group_uid != gid_actual:
            _LOGGER.debug(
                "GV refresh: stale self._group_uid=%s, actual=%s — writing only to actual",
                self._group_uid, gid_actual
            )
    
        if cache.get(cache_gid) != new:
            cache[cache_gid] = new
            async_dispatcher_send(self.hass, _group_signal(cache_gid))
    
        # Always write our own state
        self.async_write_ha_state()


    async def _schedule_group_refresh_once(self) -> None:
        """Coalesce to a single refresh per group on the next loop tick.

        Uses loop.call_soon so it fires under frozen time as well.
        """
        if self._group_uid is None:
            return

        gid = self._group_uid
        scheduled = _group_scheduled(self.hass)
        if gid in scheduled:
            return
        scheduled.add(gid)

        def _runner() -> None:
            # Clear scheduled flag first, then create task (and track it)
            scheduled.discard(gid)
            task = self.hass.async_create_task(self._async_refresh_from_device())
            _group_tasks(self.hass)[gid] = task

        # Next loop tick; not time-based, so it fires with freezegun
        self.hass.loop.call_soon(_runner)

    # ---------- Push wiring for responsiveness ----------

    async def async_added_to_hass(self) -> None:
        """Subscribe to signals for live updates with minimal polling."""
        await super().async_added_to_hass()

        # Track current coordinator & group and listen for activity
        self._coord_uid = (self.speaker.coordinator or self.speaker).uid
        self._group_uid = self._current_group_uid()

        # Global activity (topology/volume changes anywhere)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SONOS_SPEAKER_ACTIVITY, self._on_local_activity
            )
        )
        # Coordinator state updates
        self._bind_coordinator_state(self._coord_uid)

        # Shared cache updates for our group
        if self._group_uid:
            self._unsub_group_signal = async_dispatcher_connect(
                self.hass, _group_signal(self._group_uid), self._push_state
            )
            self.async_on_remove(self._unsub_group_signal)

        # Cancel on HA stop (safety)
        @callback
        def _on_stop(_event) -> None:
            self._cancel_task(self._group_uid)

        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _on_stop
        )
        self.async_on_remove(self._unsub_stop)

        # Initial read populates cache
        await self._schedule_group_refresh_once()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listeners and pending work to avoid teardown hangs."""
        await super().async_will_remove_from_hass()

        if self._unsub_group_signal is not None:
            self._unsub_group_signal()
            self._unsub_group_signal = None
        if self._unsub_coord is not None:
            self._unsub_coord()
            self._unsub_coord = None
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None

        # Cancel in-flight refresh task and clear scheduled flag for our group
        self._cancel_task(self._group_uid)
        if self._group_uid is not None:
            _group_scheduled(self.hass).discard(self._group_uid)

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
    def _push_state(self) -> None:
        """Write current state (used when shared cache changes)."""
        self.async_write_ha_state()

    @callback
    def _on_coord_state_updated(self, *_: object) -> None:
        """Coordinator state changed — schedule one refresh for the group."""
        self.hass.async_create_task(self._schedule_group_refresh_once())

    @callback
    def _on_local_activity(self, *_: object) -> None:
        """Any speaker activity — rebind if coordinator/group changed, then refresh once."""
        new_coord_uid = (self.speaker.coordinator or self.speaker).uid
        if new_coord_uid != self._coord_uid:
            self._coord_uid = new_coord_uid
            self._bind_coordinator_state(new_coord_uid)

        new_group_uid = self._current_group_uid()
        if new_group_uid != self._group_uid:
            # Unsubscribe from old group signal and cancel its task
            if self._unsub_group_signal is not None:
                self._unsub_group_signal()
                self._unsub_group_signal = None
            self._cancel_task(self._group_uid)
            if self._group_uid is not None:
                _group_scheduled(self.hass).discard(self._group_uid)

            self._group_uid = new_group_uid

            if self._group_uid:
                self._unsub_group_signal = async_dispatcher_connect(
                    self.hass, _group_signal(self._group_uid), self._push_state
                )
                self.async_on_remove(self._unsub_group_signal)

        # Schedule a single refresh for the (possibly new) group
        self.hass.async_create_task(self._schedule_group_refresh_once())
