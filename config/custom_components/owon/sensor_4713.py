"""PC4713 sensor definitions for the structured MQTT protocol.

Real-time measurement data mirrors the PC4713 WebApp MeasurementsPage
(device.measure.energy): all protocol data dictionary keys are exposed,
with unit conversions applied per the protocol data dictionary.

Exception: the line-to-line voltages (volt_ab/bc/ca/ll) are intentionally
not exposed - the PC4713 firmware does not report them and the WebApp
MeasurementsPage keeps them hidden as well.
"""

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactiveEnergy,
    UnitOfReactivePower,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, MANUFACTURER, SIGNAL_DEVICE_UPDATE

_LOGGER = logging.getLogger(__name__)

MODEL_4713 = "PC4713"

# evt bitmap bits (PC4713 protocol 3.12, "Meter Event Flags")
METER_EVENT_BITS: dict[int, str] = {
    0: "phase_A_sequence_abnormal",
    1: "phase_B_sequence_abnormal",
    2: "phase_C_sequence_abnormal",
}


def parse_meter_event(raw: Any) -> str:
    """Convert the evt bitmap into a readable label."""
    try:
        bitmap = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    if bitmap <= 0:
        return "normal"
    events = [code for bit, code in METER_EVENT_BITS.items() if bitmap & (1 << bit)]
    return ", ".join(events) if events else f"unknown({bitmap})"


def meter_fallback_name(device_id: str) -> str:
    """Protocol default device name: METER_{last 6 characters of the SN}."""
    return f"METER_{device_id[-6:]}"


@dataclass(frozen=True, kw_only=True)
class Owon4713SensorEntityDescription(SensorEntityDescription):
    """Describe a PC4713 sensor entity."""

    data_key: str  # key inside the 4713 energy data dict (protocol key)
    deviceinfo_key: str | None = None  # if set, read from manager.device_info
    scale: float = 1.0
    is_string: bool = False


_PHASE_SUFFIXES: tuple[str, ...] = ("total", "a", "b", "c")
# protocol data-key id per phase: total -> "_t", phases -> "_a/_b/_c"
_PHASE_KEY_IDS: dict[str, str] = {"total": "t", "a": "a", "b": "b", "c": "c"}


def _phase_descriptions(
    data_prefix: str,
    translation_prefix: str,
    unit: Any,
    device_class: SensorDeviceClass | None,
    state_class: SensorStateClass,
    scale: float,
    phases: tuple[str, ...] = _PHASE_SUFFIXES,
    key_ids: dict[str, str] | None = None,
) -> tuple[Owon4713SensorEntityDescription, ...]:
    """Build one sensor description per phase for a data-key family."""
    ids = key_ids if key_ids is not None else _PHASE_KEY_IDS
    return tuple(
        Owon4713SensorEntityDescription(
            key=f"4713_{data_prefix}_{suffix}",
            data_key=f"{data_prefix}_{ids[suffix]}",
            translation_key=f"{translation_prefix}_{suffix}",
            native_unit_of_measurement=unit,
            device_class=device_class,
            state_class=state_class,
            scale=scale,
        )
        for suffix in phases
    )


# --------------------------------------------------------------------------- #
# Instantaneous measurements
# --------------------------------------------------------------------------- #

CURRENT_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = _phase_descriptions(
    "cur",
    "current",
    UnitOfElectricCurrent.AMPERE,
    SensorDeviceClass.CURRENT,
    SensorStateClass.MEASUREMENT,
    scale=0.001,
)

PHASE_VOLTAGE_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "volt",
        "voltage",
        UnitOfElectricPotential.VOLT,
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        scale=0.1,
        phases=("a", "b", "c"),
        key_ids={"a": "an", "b": "bn", "c": "cn"},
    )
)

VOLTAGE_AVG_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    Owon4713SensorEntityDescription(
        key="4713_volt_ln",
        data_key="volt_ln",
        translation_key="voltage_avg",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        scale=0.1,
    ),
)

FREQUENCY_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    Owon4713SensorEntityDescription(
        key="4713_freq",
        data_key="freq",
        translation_key="frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        scale=0.01,
    ),
)

ACTIVE_POWER_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "pwr",
        "active_power",
        UnitOfPower.KILO_WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        scale=0.001,
    )
)

APPARENT_POWER_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "sva",
        "apparent_power",
        UnitOfApparentPower.KILO_VOLT_AMPERE,
        SensorDeviceClass.APPARENT_POWER,
        SensorStateClass.MEASUREMENT,
        scale=0.001,
    )
)

REACTIVE_POWER_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "var",
        "reactive_power",
        UnitOfReactivePower.KILO_VOLT_AMPERE_REACTIVE,
        SensorDeviceClass.REACTIVE_POWER,
        SensorStateClass.MEASUREMENT,
        scale=0.001,
    )
)

POWER_FACTOR_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "pf",
        "power_factor",
        None,
        SensorDeviceClass.POWER_FACTOR,
        SensorStateClass.MEASUREMENT,
        scale=0.01,
    )
)

# --------------------------------------------------------------------------- #
# Cumulative energies (Wh/VAh/varh -> kWh/kVAh/kvarh)
# --------------------------------------------------------------------------- #

ACTIVE_IMPORT_ENERGY_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "wh_imp",
        "active_import_energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        scale=0.001,
    )
)

ACTIVE_EXPORT_ENERGY_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "wh_exp",
        "active_export_energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        scale=0.001,
    )
)

APPARENT_IMPORT_ENERGY_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "vah_imp",
        "apparent_import_energy",
        "kVAh",
        None,
        SensorStateClass.TOTAL_INCREASING,
        scale=0.001,
    )
)

APPARENT_EXPORT_ENERGY_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    _phase_descriptions(
        "vah_exp",
        "apparent_export_energy",
        "kVAh",
        None,
        SensorStateClass.TOTAL_INCREASING,
        scale=0.001,
    )
)


def _reactive_energy_sensors(
    quadrant: str,
) -> tuple[Owon4713SensorEntityDescription, ...]:
    """Build reactive energy sensors for one quadrant (q1..q4)."""
    return _phase_descriptions(
        f"varh_{quadrant}",
        f"reactive_energy_{quadrant}",
        UnitOfReactiveEnergy.KILO_VOLT_AMPERE_REACTIVE_HOUR,
        None,
        SensorStateClass.TOTAL_INCREASING,
        scale=0.001,
    )


REACTIVE_ENERGY_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    *_reactive_energy_sensors("q1"),
    *_reactive_energy_sensors("q2"),
    *_reactive_energy_sensors("q3"),
    *_reactive_energy_sensors("q4"),
)

# --------------------------------------------------------------------------- #
# Diagnostic sensors
# --------------------------------------------------------------------------- #

DIAG_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    Owon4713SensorEntityDescription(
        key="4713_device_id",
        data_key="device_id",
        deviceinfo_key="device_id",
        translation_key="meter_device_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_string=True,
    ),
    Owon4713SensorEntityDescription(
        key="4713_device_model",
        data_key="device_model",
        deviceinfo_key="model",
        translation_key="meter_device_model",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_string=True,
    ),
    Owon4713SensorEntityDescription(
        key="4713_device_sub_model",
        data_key="device_sub_model",
        deviceinfo_key="subModel",
        translation_key="meter_device_sub_model",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_string=True,
    ),
    Owon4713SensorEntityDescription(
        key="4713_firmware_version",
        data_key="firmware_version",
        deviceinfo_key="fw_version",
        translation_key="meter_firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_string=True,
    ),
    Owon4713SensorEntityDescription(
        key="4713_device_name",
        data_key="device_name",
        deviceinfo_key="name",
        translation_key="device_name",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_string=True,
    ),
    Owon4713SensorEntityDescription(
        key="4713_meter_event",
        data_key="evt",
        translation_key="meter_event",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_string=True,
    ),
)

ALL_4713_SENSORS: tuple[Owon4713SensorEntityDescription, ...] = (
    *CURRENT_4713_SENSORS,
    *PHASE_VOLTAGE_4713_SENSORS,
    *VOLTAGE_AVG_4713_SENSORS,
    *FREQUENCY_4713_SENSORS,
    *ACTIVE_POWER_4713_SENSORS,
    *APPARENT_POWER_4713_SENSORS,
    *REACTIVE_POWER_4713_SENSORS,
    *POWER_FACTOR_4713_SENSORS,
    *ACTIVE_IMPORT_ENERGY_4713_SENSORS,
    *ACTIVE_EXPORT_ENERGY_4713_SENSORS,
    *APPARENT_IMPORT_ENERGY_4713_SENSORS,
    *APPARENT_EXPORT_ENERGY_4713_SENSORS,
    *REACTIVE_ENERGY_4713_SENSORS,
    *DIAG_4713_SENSORS,
)


class Owon4713Sensor(SensorEntity):
    """Representation of a PC4713 meter sensor."""

    entity_description: Owon4713SensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        device_id: str,
        description: Owon4713SensorEntityDescription,
        manager: Any,
    ) -> None:
        """Initialize the sensor."""
        self.entity_description = description
        self._device_id = device_id
        self._manager = manager
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info; the card name is always METER_{full SN}.

        The full SN serves as a stable identifier regardless of any
        device-reported name (that one is shown by the device-name sensor).
        """
        info = self._manager.device_info.get(self._device_id, {})
        fw_version = info.get("fw_version")
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=f"METER_{self._device_id}",
            manufacturer=MANUFACTURER,
            model=MODEL_4713,
            sw_version=str(fw_version) if fw_version else None,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to device updates when added to hass."""

        @callback
        def _update_callback() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DEVICE_UPDATE}_{self._device_id}",
                _update_callback,
            )
        )

    @property
    def available(self) -> bool:
        """Return whether the source device is considered online."""
        return self._manager.is_device_available(self._device_id)

    @property
    def native_value(self) -> Any | None:
        """Return the sensor value from 4713 data or deviceinfo."""
        description = self.entity_description
        if description.deviceinfo_key is not None:
            raw = self._manager.device_info.get(self._device_id, {}).get(
                description.deviceinfo_key
            )
            if description.deviceinfo_key == "name":
                # Protocol default naming until the device reports a real one.
                return str(raw) if raw else meter_fallback_name(self._device_id)
            if raw is None:
                return None
            return str(raw)

        data = self._manager.devices.get(self._device_id, {})
        raw = data.get(description.data_key)
        if raw is None:
            return None

        if description.is_string:
            if description.data_key == "evt":
                return parse_meter_event(raw)
            return str(raw)

        try:
            numeric = float(raw)
        except (ValueError, TypeError):
            return None
        if description.scale != 1.0:
            return round(numeric * description.scale, 3)
        return numeric
