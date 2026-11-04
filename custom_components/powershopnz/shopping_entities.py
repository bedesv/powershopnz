"""Entities for account shop availability and intentional purchases."""
from datetime import datetime
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import CONF_PROPERTY_ID, CONF_PROPERTY_ADDRESS, CONF_SENSORS_OPTIONS, DOMAIN


def enabled(entry):
    return entry.options.get(CONF_SENSORS_OPTIONS, {}).get("shopping", True)


class ShoppingEntity(CoordinatorEntity):
    _attr_has_entity_name = True
    def __init__(self, coordinator, entry, key, name, icon):
        super().__init__(coordinator)
        prop = entry.data[CONF_PROPERTY_ID]
        self._attr_unique_id = f"{DOMAIN}_{prop}_shopping_{key}"
        self._attr_name, self._attr_icon = name, icon
        self._attr_device_info = {"identifiers": {(DOMAIN, prop)},
            "name": entry.data.get(CONF_PROPERTY_ADDRESS, "Powershop NZ"),
            "manufacturer": "Powershop NZ", "model": "Account"}

    @property
    def offers(self):
        return (self.coordinator.data or {}).get("offers", ())


class AvailablePacksSensor(ShoppingEntity, SensorEntity):
    def __init__(self, coordinator, entry, category=None):
        self.category = category
        suffix = "available_powerpacks" if category is None else "available_" + category.lower().replace(" ", "_")
        self.entity_id = f"sensor.{DOMAIN}_{suffix}"
        key = "available" if category is None else category.lower().replace(" ", "_")
        name = "Available Powerpacks" if category is None else f"Available {category}"
        super().__init__(coordinator, entry, key, name, "mdi:shopping-outline")
    @property
    def native_value(self):
        return sum(o.quantity for o in self.offers if self.category is None or o.category == self.category)
    @property
    def extra_state_attributes(self):
        selected = [o for o in self.offers if self.category is None or o.category == self.category]
        return {"offers": [o.attributes() for o in selected],
                "offer_count": len(selected), "total_price_nzd": round(sum(o.quantity * o.price_cents for o in selected) / 100, 2)}


class OffersSensor(ShoppingEntity, SensorEntity):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "offers", "Powerpack offers", "mdi:tag-multiple-outline")
        self.entity_id = f"sensor.{DOMAIN}_powerpack_offers"
    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("fingerprint")
    @property
    def extra_state_attributes(self):
        return {"offers": [o.attributes() for o in self.offers]}


class AvailabilityRevisionSensor(ShoppingEntity, SensorEntity):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "release_revision", "Powerpack releases", "mdi:bell-ring-outline")
        self.entity_id = f"sensor.{DOMAIN}_powerpack_releases"
    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("release_revision", 0)
    @property
    def extra_state_attributes(self):
        return {"last_detected": (self.coordinator.data or {}).get("latest_release")}


class LatestReleaseSensor(ShoppingEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "latest_release", "Latest Powerpack availability", "mdi:bell-ring-outline")
        self.entity_id = f"sensor.{DOMAIN}_latest_powerpack_availability"
    @property
    def native_value(self):
        value = (self.coordinator.data or {}).get("latest_release")
        return datetime.fromisoformat(value) if value else None


class PacksAvailableBinarySensor(ShoppingEntity, BinarySensorEntity):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "available_now", "Powerpacks available", "mdi:shopping-outline")
        self.entity_id = f"binary_sensor.{DOMAIN}_powerpacks_available"
    @property
    def is_on(self):
        return bool(self.offers)


class PurchasePacksButton(ShoppingEntity, ButtonEntity):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "purchase", "Buy available Powerpacks (Pay later)", "mdi:cart-arrow-down")
        self.entity_id = f"button.{DOMAIN}_buy_available_powerpacks"
    @property
    def available(self):
        return super().available and self.coordinator.can_purchase
    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        receipt = data.get("last_purchase") or {}
        return {"payment_method": "Pay later", "purchase_in_progress": self.coordinator.state.purchasing,
                "purchase_blocked": bool(self.coordinator.state.data.get("pending")),
                "last_purchase_at": receipt.get("created_at"),
                "last_purchase_total_nzd": (receipt.get("payload") or {}).get("amount", 0) / 100}
    async def async_press(self):
        await self.coordinator.async_purchase_all()


def shopping_sensors(coordinator, entry):
    return [AvailablePacksSensor(coordinator, entry), OffersSensor(coordinator, entry),
            LatestReleaseSensor(coordinator, entry), AvailabilityRevisionSensor(coordinator, entry),
            *(AvailablePacksSensor(coordinator, entry, category) for category in
              ("Staying Power", "Special Pack", "Future Pack", "EV Pack"))]
