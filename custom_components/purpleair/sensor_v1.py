"""Sensor entities for v1 API data for Home Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .model import PurpleAirConfigEntry, PurpleAirDomainData
from .purple_air_api.v1.model import NormalizedApiData
from .sensor_descriptions import (
    SIMPLE_SENSOR_DESCRIPTIONS,
    AqiSensorDescription,
    PASensorDescription,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .purple_air_api.v1.model import SensorReading

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_schedule_add_entities: AddEntitiesCallback,
) -> None:
    """Create associated v1 sensors for Home Assistant."""
    config = PurpleAirConfigEntry(**config_entry.data)
    domain_data: PurpleAirDomainData = hass.data[DOMAIN]
    coordinator = domain_data.coordinator_v1

    if not coordinator:
        _LOGGER.error("Attempting to set up v1 sensors with invalid configuration")
        return

    # we will expose only one AQI sensor here for future 2021.12 option flow
    entities: list[Entity] = [PurpleAirAqiSensor(config, coordinator)]

    entities.extend(
        PurpleAirSimpleSensor(config, coordinator, sensor_description)
        for sensor_description in SIMPLE_SENSOR_DESCRIPTIONS
    )

    async_schedule_add_entities(entities, False)


class PASensorBase(
    CoordinatorEntity[DataUpdateCoordinator[dict[str, NormalizedApiData]]]
):
    """Provides the base for PurpleAir sensors."""

    _attr_attribution = "Data provided by PurpleAir"

    pa_sensor_id: str
    pa_sensor_name: str

    def __init__(
        self,
        config: PurpleAirConfigEntry,
        coordinator: DataUpdateCoordinator[dict[str, NormalizedApiData]],
        entity_description: PASensorDescription,
    ) -> None:
        """Initialize the base sensor."""

        super().__init__(coordinator)

        self.entity_description = entity_description
        self.pa_sensor_id = config.pa_sensor_id
        self.pa_sensor_name = config.title

        self._attr_name = f"{self.pa_sensor_name} {self.entity_description.name}"
        self._attr_unique_id = f"{self.pa_sensor_id}_{self.entity_description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.pa_sensor_id)},
            "name": self.pa_sensor_name,
            "manufacturer": "PurpleAir",
        }

        self._sensor_attr_name = entity_description.attr_name

    def _get_sensor_data(self) -> SensorReading | None:
        sensor_data = self.coordinator.data.get(self.pa_sensor_id)
        return sensor_data["sensor"] if sensor_data else None

    @property
    def available(self) -> bool:
        """Get the availability of the sensor."""

        return self.native_value is not None

    @property
    def native_value(self) -> Any:
        """Get the native value for the sensor."""

        if not self._sensor_attr_name:
            return None

        data = self._get_sensor_data()
        return getattr(data, self._sensor_attr_name) if data else None


class PurpleAirAqiSensor(PASensorBase, SensorEntity):
    """Provides the AQI value for the configured sensor."""

    def __init__(
        self,
        config: PurpleAirConfigEntry,
        coordinator: DataUpdateCoordinator[dict[str, NormalizedApiData]],
    ) -> None:
        """Initialize the AQI sensor."""

        super().__init__(config, coordinator, AqiSensorDescription)

        self._warn_stale = False

    @property
    def available(self) -> bool:
        """Get the sensor availability."""

        if not (data := self._get_sensor_data()):
            return False

        if data.pm2_5_aqi_epa is None or not data.last_seen:
            return False

        now = dt_util.utcnow()
        diff = now - data.last_seen

        if diff.seconds > 5400:
            if not self._warn_stale:
                _LOGGER.warning(
                    'PurpleAir Sensor "%s" (%s) has not sent data over 90 mins. Last update was %s',
                    self.pa_sensor_name,
                    self.pa_sensor_id,
                    dt_util.as_local(data.last_seen),
                )
                self._warn_stale = True

            return False

        self._warn_stale = False
        return True

    @property
    def extra_state_attributes(self) -> dict | None:
        """Get additional state information about the AQI."""

        if not (data := self._get_sensor_data()):
            return None

        # only for 3.0 base release, these will be split out after to separate entties
        return {
            "last_seen": dt_util.as_local(data.last_seen) if data.last_seen else None,
            "adc": data.analog_input,
            "rssi": data.rssi,
            "status": data.pm2_5_aqi_epa_status,
            "uptime": data.uptime,
        }

    @property
    def native_value(self) -> int | None:
        """Get the AQI value."""

        data = self._get_sensor_data()
        return data.pm2_5_aqi_epa if data else None


class PurpleAirSimpleSensor(PASensorBase, SensorEntity):
    """Provide a sensor representing simple data from the PurpleAir sensor."""
