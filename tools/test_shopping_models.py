import copy
import importlib.util
import json
import sys
import unittest
import uuid
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'custom_components' / 'powershopnz' / 'shopping_models.py'
spec = importlib.util.spec_from_file_location('shopping_models_test', str(path))
models = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = models
spec.loader.exec_module(models)

class ShoppingModelsTests(unittest.TestCase):
    def offer(self, category='Special Pack', key='special', quantity='1', price=1234, date=None):
        return {'type': category, 'error': None, 'powerpack': {'type': category, 'templateId': key,
                'name': 'Synthetic pack', 'purchaseLimit': quantity, 'discountPrice': price,
                'redeemableFromDate': date}}

    def entries(self):
        return [self.offer('Staying Power', 'stay'), self.offer('Special Pack', 'special', '3'),
                self.offer('Future Pack', 'future', '2'), self.offer('EV Pack', 'ev', '2')]

    def cart(self, offers):
        items = [{'templateId': o.template_id, 'quantity': o.quantity, 'itemRemoved': False,
                  'itemResized': False, 'quantityAdjusted': False,
                  'powerpack': {'type': o.category, 'discountPrice': o.price_cents, 'quantityAvailable': o.quantity}}
                 for o in offers]
        total = sum(o.price_cents * o.quantity for o in offers)
        return {'data': {'version': 7, 'items': items,
                'errors': {'itemRemoved': False, 'itemResized': False, 'quantityAdjusted': False, 'versionMismatch': False},
                'costSummary': {'pricing': {'discountTotal': total}, 'credit': {'available': 0, 'applied': 0, 'remaining': 0},
                                'totalToPay': {'withoutCreditCardFee': total}}}}

    def test_all_approved_categories_quantities_and_attributes(self):
        data = self.entries() + [self.offer('Value Pack', 'value'), self.offer('Standard Power', 'standard'), self.offer('Nominated Pack', 'nom')]
        offers = models.parse_offers({'data': data})
        self.assertEqual([(o.category, o.quantity) for o in offers], [('Staying Power', 1), ('Special Pack', 3), ('Future Pack', 2), ('EV Pack', 2)])
        self.assertEqual(offers[1].attributes()['total_price_nzd'], 37.02)
        self.assertEqual(offers[1].attributes()['unit_price_nzd'], 12.34)
        json.dumps([o.attributes() for o in offers], allow_nan=False)

    def test_sold_out_unknown_types_and_partial_errors(self):
        self.assertEqual(models.parse_offers({'data': [None, {}, {'type': None}, {'type': 'EV Pack', 'powerpack': None, 'error': None}, self.offer(quantity='0')]}), ())
        with self.assertRaises(models.ShoppingError): models.parse_offers({'data': [self.offer(), {'type': 'EV Pack', 'powerpack': None, 'error': {'message': 'secret'}}]})
        with self.assertRaises(models.ShoppingError): models.parse_offers({'data': None})

    def test_bad_limit_duplicate_and_schema(self):
        for limit in (None, '', '2left', '-1', '1.0', 2, True, '1234567'):
            with self.subTest(limit=limit), self.assertRaises(models.ShoppingError): models.parse_offers({'data': [self.offer(quantity=limit)]})
        with self.assertRaises(models.ShoppingError): models.parse_offers({'data': [self.offer(), self.offer()]})
        for field, value in [('templateId', ''), ('name', 'bad\nname'), ('discountPrice', True), ('discountPrice', -1), ('type', 'EV Pack'), ('redeemableFromDate', 'bad')]:
            entry = self.offer(); entry['powerpack'][field] = value
            with self.subTest(field=field), self.assertRaises(models.ShoppingError): models.parse_offers({'data': [entry]})

    def test_fingerprint_tracks_availability_not_price_or_name(self):
        entries = self.entries(); offers = models.parse_offers({'data': entries})
        original = models.available_fingerprint(offers)
        changed = copy.deepcopy(entries); changed[0]['powerpack']['discountPrice'] = 2222; changed[0]['powerpack']['name'] = 'New name'
        self.assertEqual(original, models.available_fingerprint(models.parse_offers({'data': changed})))
        for field, value in [('templateId', 'different'), ('purchaseLimit', '2'), ('redeemableFromDate', '2026-12-01')]:
            changed = copy.deepcopy(entries); changed[0]['powerpack'][field] = value
            self.assertNotEqual(original, models.available_fingerprint(models.parse_offers({'data': changed})))
        empty = models.available_fingerprint(())
        ev = models.available_fingerprint(models.parse_offers({'data': [self.offer('EV Pack', 'same-ev')]}))
        self.assertNotEqual(empty, ev)
        self.assertEqual(empty, models.available_fingerprint(models.parse_offers({'data': [self.offer('EV Pack', 'same-ev', '0')]})))
        self.assertEqual(ev, models.available_fingerprint(models.parse_offers({'data': [self.offer('EV Pack', 'same-ev')]})))

    def test_cart_sets_all_desired_quantities_and_rejects_manual_items(self):
        offers = models.parse_offers({'data': self.entries()})
        cart = self.cart(offers[1:2])
        cart['data']['items'][0]['quantity'] = 1
        request = models.cart_request(cart, offers)
        self.assertEqual(request, {'items': [{'templateId': 'stay', 'quantity': 1}, {'templateId': 'special', 'quantity': 3},
                                            {'templateId': 'future', 'quantity': 2}, {'templateId': 'ev', 'quantity': 2}], 'version': 7})
        cart['data']['items'][0]['templateId'] = 'manual-unapproved'
        with self.assertRaises(models.ShoppingError): models.cart_request(cart, offers)
        with self.assertRaises(models.ShoppingError): models.cart_request(self.cart([]), ())

    def test_checkout_exact_quantities_and_credit(self):
        offers = models.parse_offers({'data': self.entries()}); cart = self.cart(offers)
        plan = models.checkout_plan(cart, offers)
        self.assertEqual(plan['amount'], 9872)
        self.assertEqual(plan['version'], 7)
        self.assertEqual(uuid.UUID(plan['idempotencyKey']).version, 4)
        costs = cart['data']['costSummary']; costs['credit'] = {'available': 300, 'applied': 200, 'remaining': 100}; costs['totalToPay']['withoutCreditCardFee'] -= 200
        self.assertEqual(models.checkout_plan(cart, offers)['amount'], 9672)
        first = models.fingerprint_for_purchase('synthetic-account', cart, offers)
        self.assertEqual(first, models.fingerprint_for_purchase('synthetic-account', cart, offers))
        self.assertNotEqual(first, models.fingerprint_for_purchase('other-account', cart, offers))

    def test_checkout_fails_closed_for_changed_items_flags_and_quote(self):
        offers = models.parse_offers({'data': self.entries()}); base = self.cart(offers)
        variants = []
        def change(fn):
            cart = copy.deepcopy(base); fn(cart['data']); variants.append(cart)
        change(lambda d: d['items'].pop())
        change(lambda d: d['items'].append(copy.deepcopy(d['items'][0])))
        change(lambda d: d['items'][1].update(quantity=1))
        change(lambda d: d['items'][1]['powerpack'].update(quantityAvailable=2))
        change(lambda d: d['items'][0]['powerpack'].update(discountPrice=1))
        change(lambda d: d['items'][0]['powerpack'].update(type='Value Pack'))
        change(lambda d: d['items'][0].update(itemRemoved=True))
        change(lambda d: d['errors'].update(versionMismatch=True))
        change(lambda d: d['errors'].pop('itemResized'))
        change(lambda d: d.update(version=True))
        change(lambda d: d['costSummary']['pricing'].update(discountTotal=1))
        change(lambda d: d['costSummary']['credit'].update(applied=True))
        change(lambda d: d['costSummary']['credit'].update(remaining=1))
        change(lambda d: d['costSummary']['totalToPay'].update(withoutCreditCardFee=0))
        for cart in variants:
            with self.subTest(cart=cart), self.assertRaises(models.ShoppingError): models.checkout_plan(cart, offers)
        zero = copy.deepcopy(base); total = zero['data']['costSummary']['pricing']['discountTotal']
        zero['data']['costSummary']['credit'] = {'available': total, 'applied': total, 'remaining': 0}
        zero['data']['costSummary']['totalToPay']['withoutCreditCardFee'] = 0
        with self.assertRaisesRegex(models.ShoppingError, 'zero amount'): models.checkout_plan(zero, offers)

if __name__ == '__main__': unittest.main()
