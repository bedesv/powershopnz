"""Pure validation and planning for Powershop Powerpack shopping."""
import datetime
import hashlib
import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

APPROVED = frozenset(('Staying Power', 'Special Pack', 'Future Pack', 'EV Pack'))
CART_FLAGS = ('itemRemoved', 'itemResized', 'quantityAdjusted', 'versionMismatch')
ITEM_FLAGS = ('itemRemoved', 'itemResized', 'quantityAdjusted')

class ShoppingError(Exception):
    """Shopping data is incomplete or cannot be acted on safely."""

def _text(value, limit=200):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit and
            all(unicodedata.category(ch)[0] != 'C' for ch in value))

def _int(value):
    return type(value) is int and value >= 0

def _price(value, quantity):
    if not _int(value):
        return False
    try:
        return math.isfinite(value / 100) and math.isfinite((value * quantity) / 100)
    except OverflowError:
        return False

def _date(value):
    if value is None:
        return True
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?', value):
        return False
    try:
        datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False

@dataclass(frozen=True)
class Offer:
    template_id: str
    name: str
    category: str
    quantity: int
    price_cents: int
    redeemable_from: Optional[str]

    def attributes(self):
        return {'template_id': self.template_id, 'name': self.name, 'type': self.category,
                'quantity': self.quantity, 'unit_price_nzd': self.price_cents / 100,
                'total_price_nzd': self.price_cents * self.quantity / 100,
                'redeemable_from': self.redeemable_from}

def _offers(offers):
    if not isinstance(offers, (tuple, list)):
        raise ShoppingError('Invalid offers')
    seen = set()
    for offer in offers:
        if (not isinstance(offer, Offer) or not _text(offer.template_id) or not _text(offer.name) or
            not isinstance(offer.category, str) or offer.category not in APPROVED or
            not _int(offer.quantity) or not 1 <= offer.quantity <= 999999 or
            not _price(offer.price_cents, offer.quantity) or not _date(offer.redeemable_from) or offer.template_id in seen):
            raise ShoppingError('Invalid or duplicate offer')
        seen.add(offer.template_id)
    return {offer.template_id: offer for offer in offers}

def parse_offers(response) -> Tuple[Offer, ...]:
    if not isinstance(response, dict) or not isinstance(response.get('data'), list):
        raise ShoppingError('Invalid catalogue')
    result = []
    for entry in response['data']:
        if not isinstance(entry, dict) or not isinstance(entry.get('type'), str) or entry['type'] not in APPROVED:
            continue
        if entry.get('error') is not None:
            raise ShoppingError('Approved catalogue entry unavailable')
        pack = entry.get('powerpack')
        if pack is None:
            continue
        if not isinstance(pack, dict):
            raise ShoppingError('Invalid approved catalogue entry')
        limit = pack.get('purchaseLimit')
        if not isinstance(limit, str) or not re.fullmatch(r'[0-9]{1,6}', limit):
            raise ShoppingError('Approved offer has unknown purchase limit')
        quantity = int(limit)
        if quantity == 0:
            continue
        offer = Offer(pack.get('templateId'), pack.get('name'), entry['type'], quantity,
                      pack.get('discountPrice'), pack.get('redeemableFromDate'))
        if pack.get('type') != entry['type']:
            raise ShoppingError('Approved offer category mismatch')
        result.append(offer)
    _offers(result)
    return tuple(result)

def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()

def available_fingerprint(offers):
    _offers(offers)
    return _hash(sorted((o.template_id, o.category, o.quantity, o.redeemable_from or '') for o in offers))

def _cart(cart):
    try:
        data = cart['data']
        items, errors, version = data['items'], data['errors'], data['version']
        if not isinstance(items, list) or not isinstance(errors, dict) or not _int(version):
            raise ValueError()
        if any(errors.get(flag) is not False for flag in CART_FLAGS):
            raise ShoppingError('Cart has changed or has invalid flags')
        ids = set()
        for item in items:
            if (not isinstance(item, dict) or any(item.get(flag) is not False for flag in ITEM_FLAGS) or
                not _text(item.get('templateId')) or item['templateId'] in ids or
                not _int(item.get('quantity')) or item['quantity'] == 0):
                raise ShoppingError('Cart has changed or contains invalid items')
            ids.add(item['templateId'])
        return data, items
    except (KeyError, TypeError, ValueError):
        raise ShoppingError('Invalid cart') from None

def cart_request(cart, offers):
    by_id = _offers(offers)
    if not by_id:
        raise ShoppingError('No eligible offers')
    data, items = _cart(cart)
    if any(item['templateId'] not in by_id for item in items):
        raise ShoppingError('Cart contains unrelated items')
    return {'items': [{'templateId': offer.template_id, 'quantity': offer.quantity} for offer in offers],
            'version': data['version']}

def checkout_plan(cart, offers):
    by_id = _offers(offers)
    if not by_id:
        raise ShoppingError('No eligible offers')
    data, items = _cart(cart)
    if {item['templateId'] for item in items} != set(by_id):
        raise ShoppingError('Cart does not match eligible offers')
    subtotal = 0
    try:
        for item in items:
            offer = by_id[item['templateId']]
            pack = item['powerpack']
            if not isinstance(pack, dict):
                raise ValueError()
            available, price = pack['quantityAvailable'], pack['discountPrice']
            if (item['quantity'] != offer.quantity or not _int(available) or available < item['quantity'] or
                not _int(price) or price != offer.price_cents or pack.get('type') != offer.category):
                raise ShoppingError('Cart offer or quantity does not match')
            subtotal += price * item['quantity']
        costs = data['costSummary']
        discount = costs['pricing']['discountTotal']
        credit = costs['credit']
        available, applied, remaining = credit['available'], credit['applied'], credit['remaining']
        amount = costs['totalToPay']['withoutCreditCardFee']
        if (not all(_int(v) for v in (discount, available, applied, remaining, amount)) or
            discount != subtotal or applied > available or remaining != available - applied or
            amount != max(0, discount - applied)):
            raise ShoppingError('Cart quote or credit is inconsistent')
        if amount == 0:
            raise ShoppingError('Pay later cannot be prepared for a zero amount')
        return {'amount': amount, 'idempotencyKey': str(uuid.uuid4()), 'version': data['version']}
    except (KeyError, TypeError, ValueError):
        raise ShoppingError('Invalid cart quote') from None

def fingerprint_for_purchase(account, cart, offers):
    if not _text(account, 128):
        raise ShoppingError('Invalid account')
    plan = checkout_plan(cart, offers)
    data, items = _cart(cart)
    quote = {'account': account, 'version': plan['version'], 'amount': plan['amount'],
             'items': sorted((i['templateId'], i['quantity'], i['powerpack']['type'],
                              i['powerpack']['discountPrice'], i['powerpack']['quantityAvailable']) for i in items),
             'credit': {k: data['costSummary']['credit'][k] for k in ('available', 'applied', 'remaining')}}
    return _hash(quote)
