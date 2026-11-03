"""Available Powerpacks and explicitly requested Pay later checkout."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store as HAStore
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, RefreshTokenInvalidError
from .const import CONF_ACCOUNT_ID, CONF_REFRESH_TOKEN, DOMAIN
from .shopping_models import (
    Offer, ShoppingError, parse_offers, available_fingerprint, cart_request,
    checkout_plan, fingerprint_for_purchase,
)

_LOGGER = logging.getLogger(__name__)
SHOP_URL = "https://powerpacks.powershop.nz"
USER_AGENT = "powershopnz-homeassistant/1.1 (+https://github.com/bedesv/powershopnz)"


class ShopHTTPError(ShoppingError):
    def __init__(self, status: int, code: str | None = None):
        self.status, self.code = status, code
        super().__init__(f"Powershop shop returned HTTP {status}")


class ShopAPI:
    """Use the integration's existing session; never retry a mutation."""
    def __init__(self, client, account: str, persist_token):
        self.client, self.account, self.persist_token = client, account, persist_token

    async def request(self, method: str, path: str, payload=None):
        allowed = {("GET", "/v3/powerpacks"), ("GET", "/v1/carts/current"),
                   ("PATCH", "/v1/carts/current"), ("POST", "/v1/carts/current/pay-later")}
        if (method, path) not in allowed or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.account):
            raise ShoppingError("Unsupported shopping request")
        try:
            token = await self.client._get_current_token()
            session = await self.client._connect()
            async with session.request(
                method, SHOP_URL + path, json=payload,
                headers={"Authorization": f"Bearer {token}", "X-Account-Number": self.account,
                         "User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise AuthError("Powershop shop authentication expired")
                if not 200 <= response.status < 300:
                    code = None
                    # Only known validation codes influence recovery. Never log response bodies.
                    if response.status in (409, 422):
                        try:
                            data = await response.json(content_type=None)
                            candidate = data.get("error", {}).get("code")
                            if candidate in ("CART_CHANGED", "AMOUNT_MISMATCH", "CART_VERSION_CONFLICT"):
                                code = candidate
                        except (ValueError, TypeError, AttributeError):
                            pass
                    raise ShopHTTPError(response.status, code)
                try:
                    return await response.json(content_type=None)
                except (ValueError, TypeError) as err:
                    raise ShoppingError("Powershop shop returned an invalid response") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ShoppingError("Powershop shop could not be reached") from err
        finally:
            self.persist_token()


class DurableShoppingStore(HAStore):
    """Surface write failures that HA's general-purpose Store otherwise logs only."""
    async def _async_write_data(self, data):
        try:
            await super()._async_write_data(data)
        except Exception:
            self._shopping_write_failed = True
            raise

    async def async_save(self, data):
        if self.hass.state in (CoreState.stopping, CoreState.final_write, CoreState.stopped):
            raise ShoppingError("Home Assistant is stopping; purchase state cannot be saved")
        self._shopping_write_failed = False
        await super().async_save(data)
        if self._shopping_write_failed:
            raise ShoppingError("Purchase state could not be saved; checkout was stopped")


class AccountShoppingState:
    """One lock and durable purchase state per account, shared across properties."""
    def __init__(self, hass, account):
        self.lock = asyncio.Lock()
        self.purchasing = False
        self.store = DurableShoppingStore(hass, 1, f"{DOMAIN}_shopping_{account}", private=True, atomic_writes=True)
        self.data: dict[str, Any] = {}
        self.loaded = False

    async def load(self):
        if not self.loaded:
            data = await self.store.async_load()
            if data is not None and (not isinstance(data, dict) or
                (data.get("pending") is not None and not isinstance(data["pending"], dict))):
                raise ShoppingError("Saved purchase state is invalid; check it before purchasing")
            self.data = data or {}
            self.loaded = True

    async def save(self, data):
        await self.store.async_save(data)
        self.data = data


class PowerpackShoppingCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client, refresh_owned=None):
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} shopping", update_interval=timedelta(minutes=5))
        self.entry = entry
        self.account = entry.data[CONF_ACCOUNT_ID]
        accounts = hass.data.setdefault(DOMAIN, {}).setdefault("shopping_accounts", {})
        self.state = accounts.setdefault(self.account, AccountShoppingState(hass, self.account))
        self.api = ShopAPI(client, self.account, self._persist_token)
        self.refresh_owned = refresh_owned

    def _persist_token(self):
        token = self.api.client.refresh_token
        if token and token != self.entry.data.get(CONF_REFRESH_TOKEN):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_REFRESH_TOKEN: token})

    async def _read_offers(self):
        return parse_offers(await self.api.request("GET", "/v3/powerpacks"))

    async def _snapshot(self, offers):
        availability = {o.template_id: [o.quantity, o.redeemable_from, o.category] for o in offers}
        previous = self.state.data.get("availability")
        latest = self.state.data.get("latest_release")
        revision = self.state.data.get("release_revision", 0)
        if isinstance(previous, dict) and any(
            key not in previous or value[0] > previous[key][0] or value[1:] != previous[key][1:]
            for key, value in availability.items()
        ):
            latest = datetime.now(timezone.utc).isoformat()
            revision += 1
        if previous != availability:
            await self.state.save({**self.state.data, "availability": availability, "latest_release": latest, "release_revision": revision})
        return {"offers": offers, "fingerprint": available_fingerprint(offers),
                "latest_release": latest, "release_revision": revision, "pending": bool(self.state.data.get("pending")),
                "last_purchase": self.state.data.get("last_purchase")}

    async def _async_update_data(self):
        try:
            async with self.state.lock:
                await self.state.load()
                return await self._snapshot(await self._read_offers())
        except (AuthError, RefreshTokenInvalidError) as err:
            raise ConfigEntryAuthFailed("Powershop sign-in needs to be renewed") from err
        except (ShoppingError, OSError, ValueError) as err:
            raise UpdateFailed(str(err)) from err

    @property
    def can_purchase(self):
        return bool(self.last_update_success and self.data and self.data["offers"]
                    and not self.state.purchasing and not self.state.data.get("pending"))

    def _notify_unknown(self, message):
        persistent_notification.async_create(
            self.hass, message, title="Powershop purchase needs checking",
            notification_id=f"{DOMAIN}_purchase_{self.account}")

    @staticmethod
    def _recoverable_cart_change(cart):
        try:
            data = cart["data"]
            flags = data["errors"]
            items = data["items"]
            keys = ("itemRemoved", "itemResized", "quantityAdjusted", "versionMismatch")
            if not isinstance(flags, dict) or not isinstance(items, list) or any(type(flags.get(k)) is not bool for k in keys):
                return False
            if flags["itemRemoved"]:
                return False
            changed = any(flags[k] for k in keys[1:])
            for item in items:
                if not isinstance(item, dict) or any(type(item.get(k)) is not bool for k in keys[:3]) or item["itemRemoved"]:
                    return False
                changed = changed or item["itemResized"] or item["quantityAdjusted"]
            return changed
        except (KeyError, TypeError):
            return False

    async def _stage_cart(self):
        for attempt in range(2):
            offers = await self._read_offers()
            if not offers:
                raise ShoppingError("No eligible Powerpacks are available")
            cart = await self.api.request("GET", "/v1/carts/current")
            try:
                patch = cart_request(cart, offers)
            except ShoppingError:
                if attempt == 0 and self._recoverable_cart_change(cart):
                    continue
                raise
            try:
                # An existing, exact cart needs no write.
                plan = checkout_plan(cart, offers)
                return offers, cart, plan
            except ShoppingError:
                pass
            try:
                cart = await self.api.request("PATCH", "/v1/carts/current", patch)
                plan = checkout_plan(cart, offers)
                return offers, cart, plan
            except ShopHTTPError as err:
                if attempt == 0 and err.status == 409:
                    continue
                raise
            except ShoppingError:
                if attempt == 0:
                    continue
                raise
        raise ShoppingError("Powershop cart changed; try again after the offers refresh")

    async def async_purchase_all(self):
        """One intentional button press; purchase all remaining approved quantities."""
        if self.state.purchasing:
            raise HomeAssistantError("A Powershop purchase is already in progress")
        self.state.purchasing = True
        self.async_update_listeners()
        success = False
        try:
            async with self.state.lock:
                await self.state.load()
                if self.state.data.get("pending"):
                    raise ShoppingError("A previous purchase has an unknown outcome; check Powershop before trying again")
                offers, cart, plan = await self._stage_cart()
                fingerprint = fingerprint_for_purchase(self.account, cart, offers)
                # Re-read immediately before committing, including changes made from another device.
                fresh_offers = await self._read_offers()
                fresh_cart = await self.api.request("GET", "/v1/carts/current")
                fresh_plan = checkout_plan(fresh_cart, fresh_offers)
                if (fingerprint_for_purchase(self.account, fresh_cart, fresh_offers) != fingerprint
                    or fresh_plan["amount"] != plan["amount"] or fresh_plan["version"] != plan["version"]):
                    raise ShoppingError("The Powerpack quote changed; refresh and press the button again")
                last = self.state.data.get("last_purchase") or {}
                if last.get("fingerprint") == fingerprint:
                    raise ShoppingError("This Powerpack quote has already been purchased")
                pending = {"payload": plan, "fingerprint": fingerprint,
                           "created_at": datetime.now(timezone.utc).isoformat(),
                           "packs": [offer.attributes() for offer in offers]}
                await self.state.save({**self.state.data, "pending": pending})
                if self.hass.state in (CoreState.stopping, CoreState.final_write, CoreState.stopped):
                    raise ShoppingError("Home Assistant is stopping; checkout was stopped")
                try:
                    result = await self.api.request("POST", "/v1/carts/current/pay-later", plan)
                    order_id = result["data"]["orderId"]
                    if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", order_id):
                        raise ShoppingError("Powershop returned an invalid purchase receipt")
                except ShopHTTPError as err:
                    if err.status == 422 and err.code in ("CART_CHANGED", "AMOUNT_MISMATCH"):
                        await self.state.save({**self.state.data, "pending": None})
                        raise ShoppingError("Powershop rejected the changed quote; refresh before trying again") from err
                    self._notify_unknown("The purchase result is uncertain. Check your Powershop purchase history before retrying. Further purchases are blocked until this saved attempt is resolved.")
                    raise ShoppingError("Purchase outcome is unknown; check Powershop before retrying") from err
                except (Exception, asyncio.CancelledError) as err:
                    if isinstance(err, AuthError):
                        self.entry.async_start_reauth(self.hass)
                    self._notify_unknown("The purchase result is uncertain. Check your Powershop purchase history before retrying. Further purchases are blocked until this saved attempt is resolved.")
                    if isinstance(err, asyncio.CancelledError):
                        raise
                    raise ShoppingError("Purchase outcome is unknown; check Powershop before retrying") from err
                receipt = {**pending, "order_id": order_id}
                try:
                    await self.state.save({**self.state.data, "pending": None, "last_purchase": receipt})
                except (Exception, asyncio.CancelledError) as err:
                    self._notify_unknown("Powershop confirmed the purchase, but its receipt could not be saved. Check purchase history before retrying; the pending attempt remains blocked.")
                    if isinstance(err, asyncio.CancelledError):
                        raise
                    raise ShoppingError("Purchase confirmed but receipt could not be saved; check Powershop") from err
                success = True
                # Post-purchase refresh must not turn a confirmed order into a failed purchase.
                try:
                    snapshot = await self._snapshot(await self._read_offers())
                    self.async_set_updated_data(snapshot)
                except Exception:
                    self.async_set_updated_data({"offers": (), "fingerprint": "", "latest_release": self.state.data.get("latest_release"), "release_revision": self.state.data.get("release_revision", 0), "pending": False, "last_purchase": receipt})
        except (AuthError, RefreshTokenInvalidError) as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Powershop sign-in needs to be renewed") from err
        except (ShoppingError, OSError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            self.state.purchasing = False
            self.async_update_listeners()
        if success and self.refresh_owned:
            try:
                await self.refresh_owned()
            except Exception:
                _LOGGER.warning("Powershop purchase confirmed; balance refresh will retry on the next poll")
