"""Support for interacting with Snapcast clients."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, cast

from snapcast.control.client import Snapclient
from snapcast.control.group import Snapgroup
import voluptuous as vol

from homeassistant.components.media_player import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    entity_platform,
    entity_registry as er,
)
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_LATENCY,
    CLIENT_PREFIX,
    CLIENT_SUFFIX,
    DOMAIN,
    SERVICE_RESTORE,
    SERVICE_SET_LATENCY,
    SERVICE_SNAPSHOT,
)
from .coordinator import SnapcastUpdateCoordinator
from .entity import SnapcastCoordinatorEntity

STREAM_STATUS = {
    "idle": MediaPlayerState.IDLE,
    "playing": MediaPlayerState.PLAYING,
    "unknown": None,
}

_LOGGER = logging.getLogger(__name__)


def register_services() -> None:
    """Register snapcast services."""
    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(SERVICE_SNAPSHOT, None, "snapshot")
    platform.async_register_entity_service(SERVICE_RESTORE, None, "async_restore")
    platform.async_register_entity_service(
        SERVICE_SET_LATENCY,
        {vol.Required(ATTR_LATENCY): cv.positive_int},
        "async_set_latency",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the snapcast config entry."""

    # Fetch coordinator from global data
    coordinator: SnapcastUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # Create an ID for the Snapserver
    host = config_entry.data[CONF_HOST]
    port = config_entry.data[CONF_PORT]
    host_id = f"{host}:{port}"

    register_services()

    _known_client_ids: set[str] = set()

    @callback
    def _check_entities() -> None:
        nonlocal _known_client_ids

        def _update_known_ids(known_ids, ids) -> tuple[str, str]:
            ids_to_add = ids - known_ids
            ids_to_remove = known_ids - ids

            # Update known IDs
            known_ids -= ids_to_remove
            known_ids |= ids_to_add

            return ids_to_add, ids_to_remove

        client_ids = {c.identifier for c in coordinator.server.clients}
        clients_to_add, clients_to_remove = _update_known_ids(
            _known_client_ids, client_ids
        )

        _LOGGER.debug(
            "New clients: %s",
            str([coordinator.server.client(c).friendly_name for c in clients_to_add]),
        )
        _LOGGER.debug(
            "Remove client IDs: %s",
            str([list(clients_to_remove)]),
        )

        # Add new entities
        async_add_entities(
            [
                SnapcastClientDevice(
                    coordinator, coordinator.server.client(client_id), host_id
                )
                for client_id in clients_to_add
            ]
        )

        # Remove stale entities
        entity_registry = er.async_get(hass)
        for client_id in clients_to_remove:
            if entity_id := entity_registry.async_get_entity_id(
                MEDIA_PLAYER_DOMAIN,
                DOMAIN,
                SnapcastClientDevice.get_unique_id(host_id, client_id),
            ):
                entity_registry.async_remove(entity_id)

    coordinator.async_add_listener(_check_entities)

    # Remove any existing entities
    entity_registry = er.async_get(hass)
    entity_registry.async_clear_config_entry(config_entry.entry_id)

    _check_entities()


class SnapcastClientDevice(SnapcastCoordinatorEntity, MediaPlayerEntity):
    """Representation of a Snapcast client device."""

    _attr_should_poll = False
    _attr_media_content_type = MediaType.MUSIC
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.GROUPING  # Clients support grouping
    )

    def __init__(
        self,
        coordinator: SnapcastUpdateCoordinator,
        client: Snapclient,
        host_id: str,
    ) -> None:
        """Initialize the base device."""
        SnapcastCoordinatorEntity.__init__(self, coordinator)

        self._client = client
        self._host_id = host_id
        self._attr_unique_id = self.get_unique_id(host_id, client.identifier)

    @classmethod
    def get_unique_id(cls, host, id) -> str:
        """Get a unique ID for a client."""
        return f"{CLIENT_PREFIX}{host}_{id}"

    @property
    def _current_group(self) -> Snapgroup:
        """Return the group the client is associated with."""
        return self._client.group

    async def async_added_to_hass(self) -> None:
        """Subscribe to events."""
        await super().async_added_to_hass()
        self._client.set_callback(self.schedule_update_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Disconnect object when removed."""
        self._client.set_callback(None)

    @property
    def identifier(self) -> str:
        """Return the snapcast identifier."""
        return self._client.identifier

    @property
    def available(self) -> bool:
        """Check device availability."""
        return super().available and self._client.connected

    @property
    def name(self) -> str:
        """Return the name of the device."""
        return f"{self._client.friendly_name} {CLIENT_SUFFIX}"

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the state of the player."""
        if self._client.connected:
            if self.is_volume_muted or self._current_group.muted:
                return MediaPlayerState.IDLE
            return STREAM_STATUS.get(self._current_group.stream_status)
        return MediaPlayerState.STANDBY

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes."""
        state_attrs = {}
        if self.latency is not None:
            state_attrs["latency"] = self.latency
        return state_attrs

    @property
    def source(self) -> str | None:
        """Return the current input source."""
        return self._current_group.stream

    @property
    def source_list(self) -> list[str]:
        """List of available input sources."""
        return list(self._current_group.streams_by_name().keys())

    async def async_select_source(self, source: str) -> None:
        """Set input source."""
        streams = self._current_group.streams_by_name()
        if source in streams:
            await self._current_group.set_stream(streams[source].identifier)
            self.async_write_ha_state()

    @property
    def is_volume_muted(self) -> bool:
        """Volume muted."""
        return self._client.muted

    async def async_mute_volume(self, mute: bool) -> None:
        """Send the mute command."""
        await self._client.set_muted(mute)
        self.async_write_ha_state()

    @property
    def volume_level(self) -> float | None:
        """Return the volume level."""
        return self._client.volume / 100

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume level."""
        await self._client.set_volume(round(volume * 100))
        self.async_write_ha_state()

    def snapshot(self) -> None:
        """Snapshot the group state."""
        self._client.snapshot()

    async def async_restore(self) -> None:
        """Restore the group state."""
        await self._client.restore()
        self.async_write_ha_state()

    @property
    def latency(self) -> float | None:
        """Latency for Client."""
        return self._client.latency

    async def async_set_latency(self, latency) -> None:
        """Set the latency of the client."""
        await self._client.set_latency(latency)
        self.async_write_ha_state()

    @property
    def group_members(self) -> list[str] | None:
        """List of player entities which are currently grouped together for synchronous playback."""
        entity_registry = er.async_get(self.hass)
        return [
            entity_id
            for client_id in self._current_group.clients
            if (
                entity_id := entity_registry.async_get_entity_id(
                    MEDIA_PLAYER_DOMAIN,
                    DOMAIN,
                    SnapcastClientDevice.get_unique_id(self._host_id, client_id),
                )
            )
        ]

    async def async_join_players(self, group_members: list[str]) -> None:
        """Join `group_members` as a player group with the current player."""
        component: EntityComponent[MediaPlayerEntity] = self.hass.data[
            MEDIA_PLAYER_DOMAIN
        ]

        client_ids = [
            cast(SnapcastClientDevice, client).identifier
            for member in group_members
            if (client := component.get_entity(member))
        ]

        for identifier in client_ids:
            await self._current_group.add_client(identifier)

        self.async_write_ha_state()

    async def async_unjoin_player(self) -> None:
        """Remove this player from any group."""
        await self._current_group.remove_client(self._client.identifier)
        self.async_write_ha_state()

    def _get_metadata(self, key, default=None) -> Any:
        """Get metadata from the current stream."""
        if metadata := self.coordinator.server.stream(
            self._current_group.stream
        ).metadata:
            return metadata.get(key, default)

        return default

    @property
    def media_title(self) -> str | None:
        """Title of current playing media."""
        return self._get_metadata("title")

    @property
    def media_image_url(self) -> str | None:
        """Image url of current playing media."""
        return self._get_metadata("artUrl")

    @property
    def media_artist(self) -> str | None:
        """Artist of current playing media, music track only."""
        return self._get_metadata("artist", [None])[0]

    @property
    def media_album_name(self) -> str | None:
        """Album name of current playing media, music track only."""
        return self._get_metadata("album")

    @property
    def media_album_artist(self) -> str | None:
        """Album artist of current playing media, music track only."""
        return self._get_metadata("albumArtist", [None])[0]

    @property
    def media_track(self) -> int | None:
        """Track number of current playing media, music track only."""
        if value := self._get_metadata("trackNumber") is not None:
            return int(value)

        return None

    @property
    def media_duration(self) -> int | None:
        """Duration of current playing media in seconds."""
        if value := self._get_metadata("duration") is not None:
            return int(value)

        return None

    @property
    def media_position(self) -> int | None:
        """Position of current playing media in seconds."""
        # Position is part of properties object, not metadata object
        if properties := self.coordinator.server.stream(
            self._current_group.stream
        ).properties:
            if value := properties.get("position", None) is not None:
                return int(value)

        return None
