#!/usr/bin/env python3
"""Powershop preview and explicitly confirmed Pay later purchase."""
import argparse
import copy
import datetime
import getpass
import hashlib
import fcntl
import json
import os
import stat
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal
from collections import namedtuple

KEY = 'AIzaSyCYCKXQhGmo7haJxAAyO_7mIPrV7jtxsK8'
AUTH = 'https://auth.powershop.nz'
SHOP = 'https://powerpacks.powershop.nz'
FIREBASE = 'https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=' + KEY
GRAPHQL = 'https://api.powershop.nz/v1/graphql/'
REFRESH = 'https://securetoken.googleapis.com/v1/token?key=' + KEY
PURCHASE = SHOP + '/v1/carts/current/pay-later'
CACHE_LIMIT = 16384
# Fixed, read-only account discovery; never accepts a caller-supplied query.
ACCOUNTS_QUERY = 'query accountsList($allowedBrandCodes: [BrandChoices]) { viewer { accounts(allowedBrandCodes: $allowedBrandCodes) { number } } }'
APPROVED = frozenset(('Staying Power', 'Special Pack', 'Future Pack', 'EV Pack'))
USER_AGENT = 'powershopnz-dry-run/0.1 (+https://github.com/bedesv/powershopnz)'
MAX_PAGES = 100
MAX_RECORDS = 10000

class ProbeError(Exception):
    pass

class RevokedRefresh(ProbeError):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError('Redirect refused')

class Transport:
    """Only these fixed operations can reach the network; no redirects or retries."""
    def __init__(self, opener=None, purchase_enabled=False):
        self.purchase_enabled = purchase_enabled is True
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self._token = None
        self._accounts = ()
        self._account = None

    def _request(self, method, url, body=None, headers=None):
        # Defense in depth, including against accidental new callers.
        auth_posts = {AUTH + '/cf/email-connector', AUTH + '/cf/email-otp-authenticator', FIREBASE}
        shop_gets = {SHOP + '/v3/powerpacks', SHOP + '/v1/carts/current'}
        p = urllib.parse.urlsplit(url)
        is_purchased = (p.scheme == 'https' and p.netloc == 'powerpacks.powershop.nz' and
                        p.path == '/v3/purchased-powerpacks' and not p.fragment and
                        valid_pagination_query(p.query))
        refresh_allowed = (method == 'POST' and url == REFRESH and isinstance(body, dict) and
                           set(body) == {'grant_type', 'refresh_token'} and body['grant_type'] == 'refresh_token' and valid_token(body['refresh_token']))
        purchase_allowed = (self.purchase_enabled and method == 'POST' and url == PURCHASE and valid_purchase_body(body))
        allowed = purchase_allowed or refresh_allowed or (method == 'POST' and url in auth_posts) or (method == 'POST' and url == GRAPHQL and body == {'query': ACCOUNTS_QUERY}) or (method == 'GET' and (url in shop_gets or is_purchased))
        if not allowed:
            raise ProbeError('Request refused by read-only allowlist')
        operations = {AUTH + '/cf/email-connector': 'requesting email code',
                      AUTH + '/cf/email-otp-authenticator': 'verifying email code',
                      FIREBASE: 'Firebase sign-in', REFRESH: 'refreshing login', GRAPHQL: 'discovering accounts',
                      SHOP + '/v3/powerpacks': 'fetching available packs',
                      SHOP + '/v1/carts/current': 'reading cart', PURCHASE: 'submitting Pay later'}
        operation = 'fetching purchased history' if is_purchased else operations[url]
        if (method == 'GET' or purchase_allowed) and (not self._token or self._account not in self._accounts):
            raise ProbeError('Account not discovered or selected')
        data = (urllib.parse.urlencode(body).encode('utf-8') if refresh_allowed else
                None if body is None else json.dumps(body).encode('utf-8'))
        request_headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
        if data is not None:
            request_headers['Content-Type'] = ('application/x-www-form-urlencoded' if refresh_allowed else 'application/json')
        if url in {AUTH + '/cf/email-connector', AUTH + '/cf/email-otp-authenticator'}:
            request_headers['X-Client-Platform'] = 'web'
        if url == GRAPHQL:
            if not self._token:
                raise ProbeError('Authentication required')
            request_headers['Authorization'] = 'Bearer ' + self._token
        if method == 'GET' or purchase_allowed:
            request_headers.update({'Authorization': 'Bearer ' + self._token, 'X-Account-Number': self._account})
        # Never accept arbitrary caller-supplied headers.
        if headers is not None:
            raise ProbeError('Custom headers refused')
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                if not 200 <= resp.status < 300:
                    raise ProbeError('{}: service returned HTTP {}'.format(operation, resp.status))
                if url == AUTH + '/cf/email-connector':
                    return None  # Endpoint need not return JSON.
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            status = exc.code
            revoked = False
            if refresh_allowed and status == 400:
                try:
                    raw = exc.read(CACHE_LIMIT + 1)
                    if len(raw) <= CACHE_LIMIT:
                        details = json.loads(raw)
                        revoked = isinstance(details, dict) and isinstance(details.get('error'), dict) and details['error'].get('message') in {'INVALID_REFRESH_TOKEN', 'TOKEN_EXPIRED', 'USER_DISABLED', 'USER_NOT_FOUND'}
                except (ValueError, UnicodeError, OSError, TypeError):
                    pass
            exc.close()
            if revoked:
                raise RevokedRefresh('refreshing login: saved login is no longer valid') from None
            raise ProbeError('{}: service returned HTTP {}'.format(operation, status)) from exc
        except ProbeError as exc:
            if str(exc) == 'Redirect refused':
                raise ProbeError('{}: redirect refused'.format(operation)) from exc
            raise
        except (urllib.error.URLError, ValueError, UnicodeError, OSError) as exc:
            raise ProbeError('{}: request failed or returned invalid JSON'.format(operation)) from exc

    def authenticate(self, email, otp_reader):
        self._token = None
        self._accounts = ()
        self._account = None
        journey = str(uuid.uuid4())
        common = {'email': email, 'brand': 'powershop', 'journeyId': journey}
        self._request('POST', AUTH + '/cf/email-connector', dict(common, redirectUrl='https://app.powershop.nz', otpEnabled=True))
        otp = otp_reader()
        result = self._request('POST', AUTH + '/cf/email-otp-authenticator', dict(common, otp=otp))
        custom = result.get('customToken') if isinstance(result, dict) else None
        if not isinstance(custom, str) or not custom:
            raise ProbeError('verifying email code: unexpected response')
        result = self._request('POST', FIREBASE, {'token': custom, 'returnSecureToken': True})
        token = result.get('idToken') if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise ProbeError('Firebase sign-in: unexpected response')
        refresh = result.get('refreshToken')
        if not valid_token(refresh):
            raise ProbeError('Firebase sign-in: unexpected response')
        self._token = token
        return refresh

    def refresh_login(self, refresh_token):
        self._token, self._accounts, self._account = None, (), None
        result = self._request('POST', REFRESH, {'grant_type': 'refresh_token', 'refresh_token': refresh_token})
        if not isinstance(result, dict) or not valid_token(result.get('id_token')) or not valid_token(result.get('refresh_token')):
            raise ProbeError('refreshing login: unexpected response')
        self._token = result['id_token']
        return result['refresh_token']

    def discover_accounts(self):
        result = self._request('POST', GRAPHQL, {'query': ACCOUNTS_QUERY})
        try:
            entries = result['data']['viewer']['accounts']
            if not isinstance(entries, list) or not entries:
                raise ValueError()
            accounts = [x['number'] for x in entries]
            if any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', n) for n in accounts) or len(set(accounts)) != len(accounts):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ProbeError('discovering accounts: unexpected response')
        self._accounts = tuple(accounts)
        self._account = None
        return len(accounts)

    def select_account(self, index):
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(self._accounts):
            raise ProbeError('Invalid account selection')
        self._account = self._accounts[index]

    def catalogue(self):
        return self._request('GET', SHOP + '/v3/powerpacks')

    def cart(self):
        return self._request('GET', SHOP + '/v1/carts/current')

    def purchased_page(self, cursor=None):
        params = {'pageSize': '100'}
        if cursor is not None:
            if not valid_cursor(cursor):
                raise ProbeError('Invalid pagination cursor')
            params['cursor'] = cursor
        return self._request('GET', SHOP + '/v3/purchased-powerpacks?' + urllib.parse.urlencode(params))

def valid_purchase_body(body):
    if not isinstance(body, dict) or set(body) != {'amount', 'idempotencyKey', 'version'}:
        return False
    if type(body['amount']) is not int or body['amount'] < 0 or type(body['version']) is not int or body['version'] < 0:
        return False
    try:
        key = body['idempotencyKey']
        return isinstance(key, str) and str(uuid.UUID(key)) == key
    except (ValueError, TypeError, AttributeError):
        return False

def valid_token(value):
    return isinstance(value, str) and 0 < len(value) <= 8192 and all(33 <= ord(c) < 127 for c in value)

class TokenCache:
    """Private per-user cache; descriptor-relative operations reject symlinks."""
    def __init__(self, home=None):
        self.home = home or os.path.expanduser('~')

    def _directory(self, create):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors = []
        try:
            fd = os.open(self.home, flags)
            descriptors.append(fd)
            if os.fstat(fd).st_uid != os.getuid():
                raise ProbeError('saved login: unsafe home directory')
            for component, private in (('.config', False), ('powershopnz-probe', True)):
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    fd = os.open(component, flags, dir_fd=fd)
                except FileNotFoundError:
                    return None
                descriptors.append(fd)
                st = os.fstat(fd)
                if st.st_uid != os.getuid() or (private and stat.S_IMODE(st.st_mode) != 0o700) or (not private and st.st_mode & 0o022):
                    raise ProbeError('saved login: unsafe directory ownership or permissions')
            return os.dup(fd)
        except OSError:
            raise ProbeError('saved login: cannot safely access cache directory') from None
        finally:
            for fd in descriptors:
                os.close(fd)

    def load(self):
        directory = self._directory(False)
        if directory is None:
            return None
        try:
            try:
                fd = os.open('session.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
            except OSError:
                raise ProbeError('saved login: cannot safely open cache') from None
            try:
                st = os.fstat(fd)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or
                    stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > CACHE_LIMIT):
                    raise ProbeError('saved login: unsafe cache file')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    raw = stream.read(CACHE_LIMIT + 1)
                if len(raw) > CACHE_LIMIT:
                    raise ProbeError('saved login: cache is too large')
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ProbeError('saved login: invalid cache') from None
                if (not isinstance(data, dict) or set(data) != {'version', 'refresh_token'} or
                    type(data['version']) is not int or data['version'] != 1 or not valid_token(data['refresh_token'])):
                    raise ProbeError('saved login: invalid cache')
                return data['refresh_token']
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def save(self, token):
        if not valid_token(token):
            raise ProbeError('saved login: invalid token')
        # Check any existing target before replacing it.
        self.load()
        directory = self._directory(True)
        name = '.session-' + uuid.uuid4().hex
        fd = None
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(json.dumps({'version': 1, 'refresh_token': token}).encode('utf-8'))
                stream.flush()
                os.fsync(fd)
            os.replace(name, 'session.json', src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            raise ProbeError('saved login: unable to save cache') from None
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def forget(self):
        directory = self._directory(False)
        if directory is None:
            return
        try:
            try:
                st = os.stat('session.json', dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or
                stat.S_IMODE(st.st_mode) != 0o600):
                raise ProbeError('saved login: unsafe cache file')
            os.unlink('session.json', dir_fd=directory)
        except OSError:
            raise ProbeError('saved login: unable to remove cache') from None
        finally:
            os.close(directory)

def valid_cursor(cursor):
    return isinstance(cursor, str) and 0 < len(cursor) <= 2048 and all(32 <= ord(c) < 127 for c in cursor)

def valid_pagination_query(query):
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        return (len(pairs) == len(params) and set(params) in ({'pageSize'}, {'pageSize', 'cursor'}) and params['pageSize'] == '100' and ('cursor' not in params or valid_cursor(params['cursor'])))
    except ValueError:
        return False

def money(cents):
    if type(cents) is not int or cents < 0:
        return None
    return '${:.2f}'.format(Decimal(cents) / 100)

def valid_redemption_date(value):
    if value is None:
        return True
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?', value):
        return False
    try:
        datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False

def catalogue_summary(response):
    if not isinstance(response, dict) or not isinstance(response.get('data'), list):
        raise ProbeError('fetching available packs: unexpected response')
    proposals, unknown = [], 0
    for entry in response['data']:
        if not isinstance(entry, dict):
            unknown += 1
            continue
        category = entry.get('type')
        if not isinstance(category, str):
            unknown += 1
            continue
        if category not in APPROVED:
            continue
        pack = entry.get('powerpack')
        if entry.get('error') is not None or not isinstance(pack, dict):
            unknown += 1
            continue
        limit, name, price = pack.get('purchaseLimit'), pack.get('name'), money(pack.get('discountPrice'))
        # Only exact integer limits: do not accept parseInt's partial conversions.
        quantity = int(limit) if isinstance(limit, str) and re.fullmatch(r'[0-9]{1,6}', limit) else None
        date = pack.get('redeemableFromDate')
        template_id = pack.get('templateId')
        if (not isinstance(template_id, str) or not template_id.strip() or
            pack.get('type') != category or not safe_text(name) or
            quantity is None or price is None or not valid_redemption_date(date)):
            unknown += 1
            continue
        proposals.append({'category': category, 'name': name, 'quantity': quantity,
                          'unit_price': price, 'redeemable_from': date, '_template_id': template_id, '_price_cents': pack['discountPrice']})
    return proposals, unknown

def safe_text(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 200
            and all(unicodedata.category(c)[0] != 'C' for c in value))

FLAG_LABELS = {'itemRemoved': 'item removed', 'itemResized': 'item resized',
               'quantityAdjusted': 'quantity adjusted', 'versionMismatch': 'version mismatch'}
LINE_FLAGS = ('itemRemoved', 'itemResized', 'quantityAdjusted')

def cart_flags(response):
    """Return known cart/line changes, or None for malformed flag state."""
    try:
        data = response['data']
        errors, items = data['errors'], data['items']
        if not isinstance(errors, dict) or not isinstance(items, list):
            return None
        found = []
        for flag, label in FLAG_LABELS.items():
            if type(errors.get(flag)) is not bool:
                return None
            if errors[flag]:
                found.append(label)
        for item in items:
            if not isinstance(item, dict):
                return None
            for flag in LINE_FLAGS:
                if type(item.get(flag)) is not bool:
                    return None
                if item[flag] and FLAG_LABELS[flag] not in found:
                    found.append(FLAG_LABELS[flag])
        return found
    except (KeyError, TypeError):
        return None

def cart_summary(response, listed_ids=()):
    try:
        items = response['data']['items']
        if not isinstance(items, list):
            raise TypeError()
    except (KeyError, TypeError):
        raise ProbeError('reading cart: unexpected response')
    lines = []
    for item in items:
        if not isinstance(item, dict):
            lines.append('Cart line: malformed')
            continue
        flags = [FLAG_LABELS[k] for k in LINE_FLAGS if item.get(k) is True]
        malformed_flags = any(type(item.get(k)) is not bool for k in LINE_FLAGS)
        pack, quantity = item.get('powerpack'), item.get('quantity')
        if not isinstance(pack, dict) or type(quantity) is not int or quantity < 0:
            lines.append('Cart line: ' + (', '.join(flags) + '; ' if flags else '') + 'malformed')
            continue
        category, name = pack.get('type'), pack.get('name')
        price, available = money(pack.get('discountPrice')), pack.get('quantityAvailable')
        template_id = item.get('templateId')
        if (not safe_text(category) or not safe_text(name) or price is None or
            not isinstance(template_id, str) or not template_id.strip()):
            lines.append('Cart line: ' + (', '.join(flags) + '; ' if flags else '') + 'malformed')
            continue
        allowance = str(available) if type(available) is int and available >= 0 else 'not supplied or invalid'
        match = 'matches listed offer' if template_id in listed_ids else 'not among listed offers'
        suffix = '; '.join(([', '.join(flags)] if flags else []) + (['malformed flags'] if malformed_flags else []))
        lines.append('Cart: {} | {} | quantity {} | {} each | server quantityAvailable {} | {}{}'.format(
            category, name, quantity, price, allowance, match, ' | ' + suffix if suffix else ''))
    return lines

class PayLaterPlan(namedtuple('PayLaterPlanBase', 'amount idempotency_key version')):
    __slots__ = ()

    def payload(self):
        return {'amount': self.amount, 'idempotencyKey': self.idempotency_key, 'version': self.version}

def plan_pay_later(response, offers):
    """Validate a local checkout proposal; this function cannot send it."""
    def reject():
        raise ProbeError('Pay later preview: cart is incomplete, changed, or inconsistent')
    def nonnegative(value):
        return type(value) is int and value >= 0
    try:
        changes = cart_flags(response)
        if changes is None:
            raise ProbeError('Pay later preview: malformed cart flags')
        if changes:
            raise ProbeError('Pay later preview: ' + ', '.join(changes))
        data = response['data']
        items, version, errors, costs = data['items'], data['version'], data['errors'], data['costSummary']
        if not isinstance(items, list) or not items or not nonnegative(version) or not isinstance(errors, dict):
            reject()
        if any(errors.get(k) is not False for k in ('itemRemoved', 'itemResized', 'quantityAdjusted', 'versionMismatch')):
            reject()
        by_id = {}
        for offer in offers:
            key = offer['_template_id']
            if key in by_id:
                reject()
            by_id[key] = offer
        seen, subtotal = set(), 0
        for item in items:
            if not isinstance(item, dict) or any(item.get(k) is not False for k in ('itemRemoved', 'itemResized', 'quantityAdjusted')):
                reject()
            pack, key, quantity = item['powerpack'], item['templateId'], item['quantity']
            if (not isinstance(pack, dict) or not isinstance(key, str) or not key or key in seen or key not in by_id or
                not nonnegative(quantity) or quantity == 0):
                reject()
            seen.add(key)
            available, price, category = pack['quantityAvailable'], pack['discountPrice'], pack['type']
            offer = by_id[key]
            if (not nonnegative(available) or quantity > available or not nonnegative(price) or
                category not in APPROVED or category != offer['category'] or price != offer['_price_cents'] or
                not safe_text(pack.get('name'))):
                reject()
            subtotal += price * quantity
        pricing, credit, totals = costs['pricing'], costs['credit'], costs['totalToPay']
        discount, available, applied, remaining, amount = (pricing['discountTotal'], credit['available'],
                                                            credit['applied'], credit['remaining'], totals['withoutCreditCardFee'])
        if (not all(nonnegative(x) for x in (discount, available, applied, remaining, amount)) or
            subtotal != discount or applied > available or remaining != available - applied or
            amount != max(0, discount - applied) or amount == 0):
            reject()
        plan = PayLaterPlan(amount, str(uuid.uuid4()), version)
        plan.payload()  # Construct the proposed body locally; this function does not transmit it.
        return plan
    except (KeyError, TypeError, ValueError):
        reject()

def retryable_cart(response, offers):
    """Only refresh known, otherwise valid changes; never accept a removed line."""
    changes = cart_flags(response)
    if not changes or 'item removed' in changes:
        return False
    try:
        candidate = copy.deepcopy(response)
        for flag in FLAG_LABELS:
            candidate['data']['errors'][flag] = False
        adjusted_offers = [dict(offer) for offer in offers]
        by_id = {offer['_template_id']: offer for offer in adjusted_offers}
        if len(by_id) != len(adjusted_offers):
            return False
        for item in candidate['data']['items']:
            for flag in LINE_FLAGS:
                item[flag] = False
            # A resized item can legitimately have changed price since the first catalogue GET.
            by_id[item['templateId']]['_price_cents'] = item['powerpack']['discountPrice']
        plan_pay_later(candidate, adjusted_offers)
        return True
    except (ProbeError, KeyError, TypeError, ValueError):
        return False

def quote_fingerprint(account, cart, offers):
    # Fingerprint the confirmed transaction, excluding unrelated response metadata.
    data = cart['data']
    items = sorted((item['templateId'], item['quantity'], item['powerpack']['type'],
                    item['powerpack']['name'], item['powerpack']['discountPrice'],
                    item['powerpack']['quantityAvailable']) for item in data['items'])
    ids = {item[0] for item in items}
    relevant = sorted((o['_template_id'], o['category'], o['name'], o['_price_cents']) for o in offers
                      if o['_template_id'] in ids)
    costs = data['costSummary']
    quote = {'account': account, 'version': data['version'], 'items': items, 'offers': relevant,
             'discount': costs['pricing']['discountTotal'], 'credit': costs['credit'],
             'amount': costs['totalToPay']['withoutCreditCardFee']}
    encoded = json.dumps(quote, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

def valid_attempt_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        return False
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'templateId', 'type', 'name', 'quantity', 'unitPrice'}:
            return False
        key = item['templateId']
        if (not safe_text(key) or key in seen or not isinstance(item['type'], str) or item['type'] not in APPROVED or
            not safe_text(item['name']) or type(item['quantity']) is not int or not 0 < item['quantity'] <= 1000000 or
            type(item['unitPrice']) is not int or not 0 <= item['unitPrice'] <= 100000000):
            return False
        seen.add(key)
    return True

def attempt_items(cart):
    try:
        items = [{'templateId': item['templateId'], 'type': item['powerpack']['type'],
                  'name': item['powerpack']['name'], 'quantity': item['quantity'],
                  'unitPrice': item['powerpack']['discountPrice']} for item in cart['data']['items']]
        if not valid_attempt_items(items):
            raise ValueError()
        return items
    except (KeyError, TypeError, ValueError):
        raise ProbeError('purchase journal: cannot safely record cart details') from None

def valid_attempt_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        return False
    try:
        datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return True
    except ValueError:
        return False

class PurchaseJournal:
    """Serialize purchases and retain pending/success records in a private file."""
    def __init__(self, home=None):
        self.cache = TokenCache(home)
        self.directory = None
        self.lock = None
        self.records = None

    def __enter__(self):
        try:
            self.directory = self.cache._directory(True)
            self.lock = os.open('purchase.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.directory)
            st = os.fstat(self.lock)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:
                raise ProbeError('purchase journal: unsafe lock file')
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ProbeError('purchase journal: another purchase is in progress') from None
            self.records = self._load()
            if any(record['state'] == 'pending' for record in self.records):
                raise ProbeError('purchase journal: unresolved attempt; check Powershop purchase history before manual resolution')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None

    def _load(self):
        try:
            fd = os.open('purchases.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.directory)
        except FileNotFoundError:
            return []
        except OSError:
            raise ProbeError('purchase journal: cannot safely open file') from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > 1048576:
                raise ProbeError('purchase journal: unsafe file')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(1048577)
            try:
                data = json.loads(raw)
                if len(raw) > 1048576 or not isinstance(data, list):
                    raise ValueError()
                for record in data:
                    if (not isinstance(record, dict) or record.get('state') not in ('pending', 'success') or
                        not isinstance(record.get('account'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record['account']) or
                        not isinstance(record.get('fingerprint'), str) or not re.fullmatch(r'[0-9a-f]{64}', record['fingerprint']) or
                        not valid_purchase_body(record.get('payload')) or
                        not valid_attempt_time(record.get('createdAt')) or not valid_attempt_items(record.get('items')) or
                        (record['state'] == 'success' and not valid_order_id(record.get('orderId')))):
                        raise ValueError()
                return data
            except (TypeError, ValueError, UnicodeError):
                raise ProbeError('purchase journal: invalid file') from None
        finally:
            os.close(fd)

    def _save(self):
        raw = json.dumps(self.records, separators=(',', ':')).encode('utf-8')
        if len(raw) > 1048576:
            raise ProbeError('purchase journal: file limit reached')
        temp = '.purchase-' + uuid.uuid4().hex
        fd = None
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.directory)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            os.replace(temp, 'purchases.json', src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        except OSError:
            raise ProbeError('purchase journal: unable to durably save') from None
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temp, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def begin(self, account, fingerprint, payload, items):
        if any(record['fingerprint'] == fingerprint for record in self.records):
            raise ProbeError('purchase journal: this cart quote was already purchased')
        if not valid_attempt_items(items):
            raise ProbeError('purchase journal: invalid cart details')
        created = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.records.append({'state': 'pending', 'account': account, 'fingerprint': fingerprint,
                             'payload': payload, 'createdAt': created, 'items': items})
        self._save()

    def success(self, order_id):
        self.records[-1] = dict(self.records[-1], state='success', orderId=order_id)
        self._save()

def valid_order_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value))

def execute_purchase(client, cart, offers, plan, journal_factory=PurchaseJournal):
    account = client._account
    if not client._token or account not in client._accounts:
        raise ProbeError('purchase: account not discovered or selected')
    fingerprint = quote_fingerprint(account, cart, offers)
    with journal_factory() as journal:
        if any(record['fingerprint'] == fingerprint for record in journal.records):
            raise ProbeError('purchase journal: this cart quote was already purchased')
        if not sys.stdin.isatty():
            raise ProbeError('purchase: interactive terminal required')
        phrase = 'PURCHASE {:.2f}'.format(Decimal(plan.amount) / 100)
        if input('Type {} to confirm Pay later: '.format(phrase)) != phrase:
            raise ProbeError('purchase: confirmation did not match')
        fresh_offers, _ = catalogue_summary(client.catalogue())
        fresh_cart = client.cart()
        fresh_plan = plan_pay_later(fresh_cart, fresh_offers)
        if (client._account != account or account not in client._accounts or
            quote_fingerprint(account, fresh_cart, fresh_offers) != fingerprint or
            fresh_plan.amount != plan.amount or fresh_plan.version != plan.version):
            raise ProbeError('purchase: quote changed; rerun and confirm the new cart')
        journal.begin(account, fingerprint, plan.payload(), attempt_items(fresh_cart))
        try:
            result = client._request('POST', PURCHASE, plan.payload())
            order = result['data']['orderId']
            if not valid_order_id(order):
                raise ValueError()
        except (ProbeError, KeyError, TypeError, ValueError, OSError, KeyboardInterrupt) as exc:
            status = re.fullmatch(r'submitting Pay later: service returned HTTP ([0-9]{3})', str(exc)) if isinstance(exc, ProbeError) else None
            detail = ' (HTTP {})'.format(status.group(1)) if status else ''
            raise ProbeError('purchase outcome UNKNOWN{}; check Powershop history before any further attempt'.format(detail)) from None
        try:
            journal.success(order)
        except (ProbeError, OSError, KeyboardInterrupt):
            raise ProbeError('purchase succeeded, but recording the receipt failed; check Powershop history and journal before any further attempt') from None
        print('Pay later purchase confirmed; receipt recorded locally.')

def purchased_summary(client):
    """Count validated records without exposing IDs, dates, or raw responses."""
    cursor, seen, count, unknown = None, set(), 0, 0
    for _ in range(MAX_PAGES):
        response = client.purchased_page(cursor)
        if not isinstance(response, dict):
            raise ProbeError('fetching purchased history: unexpected response')
        records, page = response.get('data'), response.get('pageInfo')
        if not isinstance(records, list) or len(records) > 100 or not isinstance(page, dict):
            raise ProbeError('fetching purchased history: unexpected page')
        has_next, end = page.get('hasNextPage'), page.get('endCursor')
        if type(has_next) is not bool or 'endCursor' not in page or (end is not None and not valid_cursor(end)):
            raise ProbeError('fetching purchased history: invalid pagination')
        count += len(records)
        if count > MAX_RECORDS:
            raise ProbeError('fetching purchased history: record limit exceeded')
        for record in records:
            if not valid_purchased_record(record):
                unknown += 1
        if not has_next:
            return count, unknown
        if not valid_cursor(end) or end in seen:
            raise ProbeError('fetching purchased history: missing or repeated cursor')
        seen.add(end)
        cursor = end
    raise ProbeError('fetching purchased history: page limit exceeded')

def valid_purchased_record(record):
    if not isinstance(record, dict):
        return False
    if any(not isinstance(record.get(k), str) or not record[k] for k in ('id', 'purchasedAt', 'redeemableFrom')):
        return False
    if any(type(record.get(k)) is not int or record[k] < 0 for k in ('voucherValue', 'balance')):
        return False
    if 'amountPaid' not in record or (record['amountPaid'] is not None and (type(record['amountPaid']) is not int or record['amountPaid'] < 0)):
        return False
    if 'displayName' not in record or (record['displayName'] is not None and not isinstance(record['displayName'], str)):
        return False
    template = record.get('template')
    if 'template' not in record:
        return False
    if template is not None and (not isinstance(template, dict) or any(not isinstance(template.get(k), str) for k in ('id', 'name', 'type', 'image'))):
        return False
    return True

def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview Powerpacks or explicitly purchase the current cart using Pay later.')
    parser.add_argument('--cart', action='store_true', help='also read the current cart')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--select-pay-later', action='store_true', help='locally preview Pay later for the current cart')
    mode.add_argument('--purchase-pay-later', action='store_true', help='confirm and purchase the current cart using Pay later')
    login = parser.add_mutually_exclusive_group()
    login.add_argument('--remember-login', action='store_true', help='save and reuse a private refresh token')
    login.add_argument('--forget-login', action='store_true', help='remove the saved login and exit')
    args = parser.parse_args(argv)
    try:
        if args.forget_login:
            TokenCache().forget()
            print('Saved login removed if present.')
            return 0
        cache = TokenCache() if args.remember_login else None
        refresh = cache.load() if cache else None
        client = Transport(purchase_enabled=True) if args.purchase_pay_later else Transport()
        if refresh:
            try:
                rotated = client.refresh_login(refresh)
            except RevokedRefresh:
                cache.forget()
                print('Saved login is no longer valid; sign in again.')
            else:
                cache.save(rotated)
        if not refresh or not client._token:
            email = input('Powershop email: ').strip()
            if not email:
                raise ProbeError('Email is required')
            token = client.authenticate(email, lambda: getpass.getpass('Email OTP: '))
            if cache:
                cache.save(token)
        count = client.discover_accounts()
        if count == 1:
            choice = 1
        else:
            print('{} accounts found.'.format(count))
            raw = input('Select account number (1-{}): '.format(count))
            if not raw.isdecimal():
                raise ProbeError('Invalid account selection')
            choice = int(raw)
        client.select_account(choice - 1)
        proposals, unknown = catalogue_summary(client.catalogue())
        history_count, history_unknown = purchased_summary(client)
        cart = client.cart() if args.cart or args.select_pay_later or args.purchase_pay_later else None
        refreshed = False
        if (args.select_pay_later or args.purchase_pay_later) and retryable_cart(cart, proposals):
            proposals, unknown = catalogue_summary(client.catalogue())
            cart = client.cart()
            refreshed = True
        if refreshed:
            print('Cart changed; refreshed catalogue and cart once using read-only requests.')
        print('Purchase limit is shown as returned; its remaining-allowance meaning is unverified.')
        for p in proposals:
            display = dict(p, redeemable_from=p['redeemable_from'] or 'not supplied')
            print('{category}: {name} | quantity {quantity} | {unit_price} each | redeemable {redeemable_from}'.format(**display))
        print('Approved entries with unknown eligibility or malformed data: {}'.format(unknown))
        print('Purchased pack history: {} records; {} with unrecognized fields.'.format(history_count, history_unknown))
        if cart is not None:
            lines = cart_summary(cart, {p['_template_id'] for p in proposals})
            print('Current cart: {} lines'.format(len(lines)))
            changes = cart_flags(cart)
            if changes is None:
                print('Cart state: malformed flags')
            elif changes:
                print('Cart state: ' + ', '.join(changes))
            for line in lines:
                print(line)
            if args.select_pay_later or args.purchase_pay_later:
                plan = plan_pay_later(cart, proposals)
                if args.purchase_pay_later:
                    print('Selected payment option: Pay later | total {}'.format(money(plan.amount)))
                    execute_purchase(client, cart, proposals, plan)
                else:
                    print('Selected payment option: Pay later (local dry-run)')
                    print('Proposed request: POST /v1/carts/current/pay-later | amount {} | cart version {}'.format(money(plan.amount), plan.version))
                    print('Based on the Powershop app contract; checkout acceptance not tested.')
        if not args.purchase_pay_later:
            print('Preview only. No purchases or cart changes were made.')
        return 0
    except OSError:
        print('Preview stopped: saved login or local input failed', file=sys.stderr)
        return 1
    except (ProbeError, EOFError, KeyboardInterrupt) as exc:
        print('Preview stopped: {}'.format(str(exc) or 'cancelled'), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
