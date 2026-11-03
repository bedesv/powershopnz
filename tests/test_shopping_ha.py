"""Offline integration tests using Home Assistant's real coordinator/entities."""
import asyncio
import copy
import logging
from datetime import timedelta
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homeassistant.core import HomeAssistant, CoreState
from homeassistant.helpers import frame, entity_registry, device_registry
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.storage import Store as HAStore
from homeassistant.util.file import WriteError
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from custom_components.powershopnz import button, binary_sensor
from custom_components.powershopnz.const import (CONF_ACCOUNT_ID, CONF_PROPERTY_ID, CONF_REFRESH_TOKEN,
                                                CONF_SENSORS_GROUPS, CONF_SENSORS_OPTIONS)
from custom_components.powershopnz.shopping import (AccountShoppingState, PowerpackShoppingCoordinator,
                                                    ShopAPI, ShoppingError, AuthError, DurableShoppingStore)
from custom_components.powershopnz.shopping_entities import (shopping_sensors, PacksAvailableBinarySensor,
                                                               PurchasePacksButton, enabled)

class MemoryStore:
    data = {}
    fail = False
    def __init__(self, hass, version, key, **kwargs): self.key, self.kwargs = key, kwargs
    async def async_load(self): return copy.deepcopy(self.data.get(self.key))
    async def async_save(self, value):
        if self.fail: raise OSError('synthetic storage failure')
        self.data[self.key] = copy.deepcopy(value)

def catalogue(prices=(1000, 2000), quantities=('1', '2')):
    return {'data': [{'type': kind, 'error': None, 'powerpack': {'templateId': key, 'name': name,
                     'type': kind, 'purchaseLimit': qty, 'discountPrice': price, 'redeemableFromDate': None}}
                    for kind, key, name, qty, price in [('Special Pack', 'special', 'Synthetic Special', quantities[0], prices[0]),
                                                          ('Future Pack', 'future', 'Synthetic Future', quantities[1], prices[1])]]}

def cart(items=(), version=3):
    return {'data': {'version': version, 'items': list(items),
                     'errors': {'itemRemoved': False, 'itemResized': False, 'quantityAdjusted': False, 'versionMismatch': False},
                     'costSummary': {'pricing': {'discountTotal': sum(i['quantity'] * i['powerpack']['discountPrice'] for i in items)},
                      'credit': {'available': 0, 'applied': 0, 'remaining': 0},
                      'totalToPay': {'withoutCreditCardFee': sum(i['quantity'] * i['powerpack']['discountPrice'] for i in items)}}}}

def line(key, qty, kind, price):
    return {'templateId': key, 'quantity': qty, 'itemRemoved': False, 'itemResized': False, 'quantityAdjusted': False,
            'powerpack': {'type': kind, 'discountPrice': price, 'quantityAvailable': qty}}

class FakeShop:
    def __init__(self):
        self.offers = catalogue(); self.cart = cart(); self.calls = []; self.post_error = None
        self.post_started = None; self.post_continue = None; self.fail_refresh = False; self.posted = False
    async def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == 'GET' and path == '/v3/powerpacks':
            if self.posted and self.fail_refresh: raise ShoppingError('synthetic refresh failure')
            return copy.deepcopy(self.offers)
        if method == 'GET': return copy.deepcopy(self.cart)
        if method == 'PATCH':
            values = {e['powerpack']['templateId']: e for e in self.offers['data']}
            rows = [line(item['templateId'], item['quantity'], values[item['templateId']]['type'],
                         values[item['templateId']]['powerpack']['discountPrice']) for item in payload['items']]
            self.cart = cart(rows, payload['version'] + 1)
            return copy.deepcopy(self.cart)
        if method == 'POST':
            self.posted = True
            if self.post_started: self.post_started.set()
            if self.post_continue: await self.post_continue.wait()
            if self.post_error: raise self.post_error
            return {'data': {'orderId': 'synthetic-order'}}
        raise AssertionError((method, path))

class ShoppingHATests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        MemoryStore.data = {}; MemoryStore.fail = False
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        frame.async_setup(self.hass)
        self.patch = patch('custom_components.powershopnz.shopping.DurableShoppingStore', MemoryStore); self.patch.start()
        self.entry = SimpleNamespace(data={CONF_ACCOUNT_ID: 'acct-test', CONF_PROPERTY_ID: 'property-test', CONF_REFRESH_TOKEN: 'token'}, options={}, async_start_reauth=Mock())
        self.client = SimpleNamespace(refresh_token='token')
        self.coordinator = PowerpackShoppingCoordinator(self.hass, self.entry, self.client)
        self.assertEqual(self.coordinator.state.store.kwargs, {'private': True, 'atomic_writes': True})
        self.shop = FakeShop(); self.coordinator.api = self.shop
        self.notification = patch.object(self.coordinator, '_notify_unknown'); self.notification.start()

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        self.notification.stop(); self.patch.stop(); self.temp.cleanup()

    async def refresh(self):
        value = await self.coordinator._async_update_data()
        self.coordinator.async_set_updated_data(value)
        return value

    async def test_availability_persists_and_ignores_price(self):
        first = await self.refresh(); self.assertIsNone(first['latest_release'])
        self.assertEqual(first['release_revision'], 0)
        self.shop.offers = catalogue(prices=(1100, 2100))
        price_only = await self.refresh()
        self.assertEqual(first['fingerprint'], price_only['fingerprint'])
        self.assertIsNone(price_only['latest_release'])
        self.assertEqual(price_only['release_revision'], 0)
        self.shop.offers = catalogue(quantities=('1', '3'))
        added = await self.refresh(); self.assertIsNotNone(added['latest_release'])
        self.assertEqual(added['release_revision'], 1)
        self.assertIsNotNone(shopping_sensors(self.coordinator, self.entry)[2].native_value)
        self.hass.data.clear()
        reloaded = PowerpackShoppingCoordinator(self.hass, self.entry, self.client); reloaded.api = self.shop
        value = await reloaded._async_update_data()
        self.assertEqual(value['latest_release'], added['latest_release'])
        self.assertEqual(value['release_revision'], 1)
        self.shop.offers = catalogue(quantities=('0', '3'))
        removed = await reloaded._async_update_data()
        self.assertEqual(removed['release_revision'], 1)
        self.shop.offers = catalogue(quantities=('1', '3'))
        readded = await reloaded._async_update_data()
        self.assertEqual(readded['release_revision'], 2)
        await reloaded.async_shutdown()

    async def test_button_patches_sets_all_and_saves_receipt(self):
        await self.refresh()
        self.shop.cart = cart([line('future', 1, 'Future Pack', 2000)])
        button_entity = PurchasePacksButton(self.coordinator, self.entry)
        self.assertTrue(button_entity.available)
        await button_entity.async_press()
        patches = [c for c in self.shop.calls if c[0] == 'PATCH']; posts = [c for c in self.shop.calls if c[0] == 'POST']
        self.assertEqual(patches[0][2]['items'], [{'templateId': 'special', 'quantity': 1}, {'templateId': 'future', 'quantity': 2}])
        self.assertEqual(len(posts), 1); self.assertEqual(posts[0][2]['amount'], 5000)
        saved = next(iter(MemoryStore.data.values()))
        self.assertIsNone(saved['pending']); self.assertEqual(saved['last_purchase']['order_id'], 'synthetic-order')

    async def test_manual_unrelated_item_rejected_without_mutation(self):
        self.shop.cart = cart([line('manual-unapproved', 1, 'Value Pack', 500)])
        with self.assertRaises(HomeAssistantError): await self.coordinator.async_purchase_all()
        self.assertFalse([c for c in self.shop.calls if c[0] in ('POST', 'PATCH')])

    async def test_duplicate_click_and_shared_account_lock(self):
        second = PowerpackShoppingCoordinator(self.hass, self.entry, self.client); second.api = self.shop
        self.assertIs(self.coordinator.state, second.state)
        self.shop.post_started = asyncio.Event(); self.shop.post_continue = asyncio.Event()
        first = asyncio.create_task(self.coordinator.async_purchase_all())
        await asyncio.wait_for(self.shop.post_started.wait(), timeout=2)
        with self.assertRaises(HomeAssistantError): await second.async_purchase_all()
        self.shop.post_continue.set(); await first
        self.assertEqual(len([c for c in self.shop.calls if c[0] == 'POST']), 1)
        await second.async_shutdown()

    async def test_timeout_pending_blocks_reinitialised_coordinator(self):
        self.shop.post_error = ShoppingError('synthetic timeout')
        with self.assertRaisesRegex(HomeAssistantError, 'unknown'): await self.coordinator.async_purchase_all()
        self.assertIsNotNone(next(iter(MemoryStore.data.values()))['pending'])
        self.hass.data.clear()
        second = PowerpackShoppingCoordinator(self.hass, self.entry, self.client); second.api = self.shop
        with self.assertRaisesRegex(HomeAssistantError, 'unknown'): await second.async_purchase_all()
        self.assertEqual(len([c for c in self.shop.calls if c[0] == 'POST']), 1)
        await second.async_shutdown()

    async def test_stopping_home_assistant_blocks_checkout(self):
        for state in (CoreState.stopping, CoreState.final_write, CoreState.stopped):
            with self.subTest(state=state):
                self.coordinator.state.store = DurableShoppingStore(self.hass, 1, 'synthetic-test')
                self.coordinator.state.loaded = True
                self.hass.state = state
                with self.assertRaises(HomeAssistantError): await self.coordinator.async_purchase_all()
                self.assertFalse([call for call in self.shop.calls if call[0] == 'POST'])
        self.hass.state = CoreState.not_running

    async def test_real_store_surfaces_suppressed_write_error(self):
        store = DurableShoppingStore(self.hass, 1, 'synthetic-test')
        with patch.object(HAStore, '_async_write_data', new=AsyncMock(side_effect=WriteError('synthetic'))):
            with self.assertRaisesRegex(ShoppingError, 'could not be saved'):
                await store.async_save({'pending': {'synthetic': True}})

    async def test_persistence_failure_prevents_post(self):
        MemoryStore.fail = True
        with self.assertRaises(HomeAssistantError): await self.coordinator.async_purchase_all()
        self.assertEqual(len([c for c in self.shop.calls if c[0] == 'POST']), 0)

    async def test_confirmed_purchase_survives_refresh_failures(self):
        self.shop.fail_refresh = True
        self.coordinator.refresh_owned = AsyncMock(side_effect=RuntimeError('failure'))
        await self.coordinator.async_purchase_all()
        self.assertEqual(next(iter(MemoryStore.data.values()))['last_purchase']['order_id'], 'synthetic-order')
        self.assertEqual(len([c for c in self.shop.calls if c[0] == 'POST']), 1)

    async def test_auth_failure_refresh_requires_reauth(self):
        self.shop.request = AsyncMock(side_effect=AuthError('synthetic'))
        with self.assertRaises(ConfigEntryAuthFailed): await self.coordinator._async_update_data()

    async def test_recoverable_cart_change_one_reread_then_stage(self):
        original = self.shop.request
        count = 0
        async def request(method, path, payload=None):
            nonlocal count
            value = await original(method, path, payload)
            if method == 'GET' and path == '/v1/carts/current':
                count += 1
                if count == 1: value['data']['errors']['versionMismatch'] = True
            return value
        self.shop.request = request
        offers, result, plan = await self.coordinator._stage_cart()
        self.assertEqual(count, 2)
        self.assertEqual(plan['amount'], 5000)
        self.assertEqual(len([c for c in self.shop.calls if c[0] == 'PATCH']), 1)
        self.assertFalse([c for c in self.shop.calls if c[0] == 'POST'])

    async def test_persistent_changed_or_removed_cart_never_mutates(self):
        for flag in ('versionMismatch', 'itemRemoved'):
            with self.subTest(flag=flag):
                self.shop = FakeShop(); self.coordinator.api = self.shop
                self.shop.cart['data']['errors'][flag] = True
                with self.assertRaises(HomeAssistantError): await self.coordinator.async_purchase_all()
                reads = [c for c in self.shop.calls if c[:2] == ('GET', '/v1/carts/current')]
                self.assertEqual(len(reads), 2 if flag == 'versionMismatch' else 1)
                self.assertFalse([c for c in self.shop.calls if c[0] in ('POST', 'PATCH')])

    async def test_multiple_properties_register_distinct_entity_ids(self):
        await self.refresh()
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass, load_empty=True)
        await entity_registry.async_load(self.hass, load_empty=True)
        platform = EntityPlatform(hass=self.hass, logger=logging.getLogger(__name__), domain='sensor',
                                  platform_name='powershopnz', platform=None, scan_interval=timedelta(seconds=30),
                                  entity_namespace=None)
        second_entry = SimpleNamespace(data={**self.entry.data, CONF_PROPERTY_ID: 'property-two'}, options={})
        first = shopping_sensors(self.coordinator, self.entry)[0]
        second = shopping_sensors(self.coordinator, second_entry)[0]
        # This isolated platform has no config entry; device linking is outside this test.
        first._attr_device_info = None
        second._attr_device_info = None
        await platform.async_add_entities([first, second])
        self.assertEqual(len(platform.entities), 2)
        self.assertNotEqual(first.entity_id, second.entity_id)
        self.assertEqual(first.entity_id, 'sensor.powershopnz_available_powerpacks')
        await platform.async_reset()

    async def test_platform_setup_respects_options(self):
        self.entry.runtime_data = SimpleNamespace(shopping=self.coordinator)
        add_button, add_binary = Mock(), Mock()
        await button.async_setup_entry(self.hass, self.entry, add_button)
        await binary_sensor.async_setup_entry(self.hass, self.entry, add_binary)
        self.assertEqual(len(add_button.call_args.args[0]), 1)
        self.assertEqual(len(add_binary.call_args.args[0]), 1)
        self.entry.options = {CONF_SENSORS_OPTIONS: {'shopping': False}}
        add_button.reset_mock(); add_binary.reset_mock()
        await button.async_setup_entry(self.hass, self.entry, add_button)
        await binary_sensor.async_setup_entry(self.hass, self.entry, add_binary)
        add_button.assert_not_called(); add_binary.assert_not_called()

    async def test_auth_failure_during_checkout_triggers_reauth_and_keeps_pending(self):
        self.shop.post_error = AuthError('synthetic')
        with self.assertRaises(HomeAssistantError): await self.coordinator.async_purchase_all()
        self.entry.async_start_reauth.assert_called_once_with(self.hass)
        self.assertIsNotNone(next(iter(MemoryStore.data.values()))['pending'])
        self.assertEqual(len([c for c in self.shop.calls if c[0] == 'POST']), 1)

    async def test_button_auth_failure_requires_reauth(self):
        self.shop.request = AsyncMock(side_effect=AuthError('synthetic'))
        with self.assertRaises(HomeAssistantError): await self.coordinator.async_purchase_all()
        self.entry.async_start_reauth.assert_called_once_with(self.hass)

    async def test_entities_counts_unique_ids_and_option_default(self):
        data = await self.refresh()
        sensors = shopping_sensors(self.coordinator, self.entry)
        self.assertEqual(len(sensors), 8)
        self.assertEqual(len({s.unique_id for s in sensors}), 8)
        self.assertEqual(sensors[0].entity_id, 'sensor.powershopnz_available_powerpacks')
        self.assertEqual(sensors[5].entity_id, 'sensor.powershopnz_available_special_pack')
        self.assertEqual(sensors[6].entity_id, 'sensor.powershopnz_available_future_pack')
        self.assertEqual(PacksAvailableBinarySensor(self.coordinator, self.entry).entity_id, 'binary_sensor.powershopnz_powerpacks_available')
        self.assertEqual(PurchasePacksButton(self.coordinator, self.entry).entity_id, 'button.powershopnz_buy_available_powerpacks')
        self.assertEqual(sensors[3].entity_id, 'sensor.powershopnz_powerpack_releases')
        self.assertEqual(sensors[3].native_value, 0)
        self.assertEqual(sensors[0].native_value, 3)
        self.assertEqual(sensors[5].native_value, 1)
        self.assertEqual(sensors[6].native_value, 2)
        self.assertTrue(PacksAvailableBinarySensor(self.coordinator, self.entry).is_on)
        self.assertTrue(PurchasePacksButton(self.coordinator, self.entry).available)
        self.assertIsNone(sensors[2].native_value)
        self.assertTrue(enabled(self.entry)); self.assertTrue(CONF_SENSORS_GROUPS['shopping'])
        self.entry.options = {CONF_SENSORS_OPTIONS: {'shopping': False}}
        self.assertFalse(enabled(self.entry))
        self.coordinator.state.data['pending'] = {'x': 1}
        self.assertFalse(PurchasePacksButton(self.coordinator, self.entry).available)

class ShopAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_post_timeout_not_retried(self):
        session = SimpleNamespace(request=Mock(side_effect=asyncio.TimeoutError()))
        client = SimpleNamespace(_get_current_token=AsyncMock(return_value='synthetic-id'), _connect=AsyncMock(return_value=session))
        api = ShopAPI(client, 'acct-test', Mock())
        with self.assertRaises(ShoppingError): await api.request('POST', '/v1/carts/current/pay-later', {'amount': 5000})
        self.assertEqual(session.request.call_count, 1)
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])

if __name__ == '__main__': unittest.main()
