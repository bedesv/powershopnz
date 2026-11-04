"""Powerpack shop availability."""
from .shopping_entities import PacksAvailableBinarySensor, enabled

async def async_setup_entry(hass, entry, async_add_entities):
    if enabled(entry):
        async_add_entities([PacksAvailableBinarySensor(entry.runtime_data.shopping, entry)])
