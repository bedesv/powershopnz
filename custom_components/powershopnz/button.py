"""Purchase available Powerpacks with Pay later."""
from .shopping_entities import PurchasePacksButton, enabled

async def async_setup_entry(hass, entry, async_add_entities):
    if enabled(entry):
        async_add_entities([PurchasePacksButton(entry.runtime_data.shopping, entry)])
