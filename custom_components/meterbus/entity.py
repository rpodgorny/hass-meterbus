"""Base entity: one Home Assistant device per meter, all under one bus."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MeterReading
from .const import DOMAIN
from .coordinator import MeterBusCoordinator


class MeterBusEntity(CoordinatorEntity[MeterBusCoordinator]):
    """Common base tying entities to one meter's device registry entry."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MeterBusCoordinator, address: str, key: str
    ) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)
        self.address = address
        # The secondary address is burned into the meter at the factory and
        # encodes its manufacturer, serial, version and medium. It survives
        # everything on the Home Assistant side, so it is the identity — never
        # the config entry, and never a position in a scan.
        self._attr_unique_id = f"{address}_{key}"

        reading = self.reading
        manufacturer = reading.manufacturer if reading else None
        model = (
            f"{reading.medium_name} meter v{reading.version}"
            if reading
            else "M-Bus meter"
        )
        name = (
            f"{reading.medium_name} {reading.identification}"
            if reading
            else f"Meter {address[:8]}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=name,
            manufacturer=manufacturer,
            model=model,
            # The full secondary address, which is what a bus scan, the meter's
            # own label and any other M-Bus tool all agree on.
            serial_number=address,
            # DeviceInfo needs the hub's generated registry id here, not its
            # identifier tuple. The hub is registered before this platform is
            # forwarded, so the lookup is guaranteed to succeed.
            via_device_id=dr.async_get_device_id_by_identifier(
                coordinator.hass,
                (DOMAIN, coordinator.config_entry.entry_id),
                config_entry_id=coordinator.config_entry.entry_id,
            ),
        )

    @property
    def reading(self) -> MeterReading | None:
        """This meter's last successful reply, if the sweep got one."""
        return (self.coordinator.data or {}).get(self.address)

    @property
    def available(self) -> bool:
        """Available only while this meter itself answered the last sweep.

        Gating per meter rather than per bus is the difference between one dead
        meter going unavailable and forty meters showing readings from whenever
        that one last worked.
        """
        return super().available and self.reading is not None
