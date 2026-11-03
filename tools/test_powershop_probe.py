import io
import copy
import uuid
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

import powershop_probe as p

class Response:
    status = 200
    def __init__(self, value): self.data = io.BytesIO(json.dumps(value).encode())
    def read(self, *args): return self.data.read(*args)
    def __enter__(self): return self
    def __exit__(self, *args): pass

class Opener:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    def open(self, request, timeout):
        self.requests.append(request)
        return Response(self.responses.pop(0))

class TransportTests(unittest.TestCase):
    def test_auth_discovery_and_account_isolation(self):
        opener = Opener([{}, {'customToken': 'secret-custom'}, {'idToken': 'secret-id', 'refreshToken': 'secret-refresh'},
                         {'data': {'viewer': {'accounts': [{'number': 'one'}, {'number': 'two'}]}}}, {'data': []}])
        t = p.Transport(opener)
        t.authenticate('person@example.test', lambda: '123456')
        self.assertEqual(t.discover_accounts(), 2)
        with self.assertRaises(p.ProbeError): t.select_account(2)
        with self.assertRaises(p.ProbeError): t.catalogue()
        t.select_account(1)
        t.catalogue()
        self.assertEqual(opener.requests[-1].get_header('X-account-number'), 'two')
        self.assertTrue(all(r.get_header('User-agent') == p.USER_AGENT for r in opener.requests))
        auth = [json.loads(r.data) for r in opener.requests[:3]]
        self.assertEqual(auth[0]['journeyId'], auth[1]['journeyId'])
        self.assertEqual(auth[0]['redirectUrl'], 'https://app.powershop.nz')
        self.assertEqual(auth[1]['otp'], '123456')
        self.assertEqual(auth[2]['token'], 'secret-custom')

    def test_unsafe_operations_refused_before_network(self):
        opener = Opener([])
        t = p.Transport(opener)
        for method, url in [('POST', p.SHOP + '/v1/carts/current/pay-later'),
                            ('POST', p.SHOP + '/v1/powerpacks/id/pay-now'),
                            ('PATCH', p.SHOP + '/v1/carts/current'),
                            ('GET', p.SHOP + '/v3/powerpacks?account=other'),
                            ('GET', 'https://evil.test/v3/powerpacks'),
                            ('POST', p.GRAPHQL)]:
            with self.subTest(method=method, url=url), self.assertRaises(p.ProbeError):
                t._request(method, url, {'query': 'mutation { buy }'})
        self.assertFalse(opener.requests)

    def test_redirects_refused_even_same_origin(self):
        handler = p.NoRedirect()
        with self.assertRaises(p.ProbeError):
            handler.redirect_request(None, None, 302, '', {}, 'https://evil.test/')
        with self.assertRaises(p.ProbeError):
            handler.redirect_request(None, None, 302, '', {}, p.SHOP + '/v3/powerpacks')

    def test_cursor_and_query_validation(self):
        self.assertTrue(p.valid_pagination_query('pageSize=100&cursor=a%2Fb%3D'))
        for value in ('pageSize=100&cursor=', 'pageSize=100&cursor=x&cursor=y',
                      'pageSize=100&account=x', 'pageSize=101', 'pageSize=100&cursor=%0A'):
            self.assertFalse(p.valid_pagination_query(value))
        with self.assertRaises(p.ProbeError): p.Transport(Opener([])).purchased_page('\n')

    def test_failed_reauthentication_clears_previous_state(self):
        t = p.Transport(Opener([{}, {}]))
        t._token, t._accounts, t._account = 'old', ('one',), 'one'
        with self.assertRaises(p.ProbeError): t.authenticate('user@example.test', lambda: '123')
        self.assertIsNone(t._token)
        self.assertEqual(t._accounts, ())
        self.assertIsNone(t._account)
        with self.assertRaises(p.ProbeError): t.catalogue()

    def test_http_error_does_not_expose_body(self):
        class Failed:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 403, 'private message', {}, io.BytesIO(b'secret body'))
        with self.assertRaises(p.ProbeError) as error:
            p.Transport(Failed())._request('POST', p.AUTH + '/cf/email-connector', {})
        self.assertEqual(str(error.exception), 'requesting email code: service returned HTTP 403')

    def test_each_operation_error_is_labeled_and_redacted(self):
        class Failed:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 403, 'private message', {}, io.BytesIO(b'secret body'))
        t = p.Transport(Failed())
        t._token, t._accounts, t._account = 'secret-token', ('private-account',), 'private-account'
        cases = [(p.AUTH + '/cf/email-connector', 'POST', {}, 'requesting email code'),
                 (p.AUTH + '/cf/email-otp-authenticator', 'POST', {'otp': 'secret-otp'}, 'verifying email code'),
                 (p.FIREBASE, 'POST', {'token': 'secret-token'}, 'Firebase sign-in'),
                 (p.GRAPHQL, 'POST', {'query': p.ACCOUNTS_QUERY}, 'discovering accounts'),
                 (p.SHOP + '/v3/powerpacks', 'GET', None, 'fetching available packs'),
                 (p.SHOP + '/v3/purchased-powerpacks?pageSize=100&cursor=secret-cursor', 'GET', None, 'fetching purchased history'),
                 (p.SHOP + '/v1/carts/current', 'GET', None, 'reading cart')]
        for url, method, body, label in cases:
            with self.subTest(label=label), self.assertRaises(p.ProbeError) as error:
                t._request(method, url, body)
            message = str(error.exception)
            self.assertEqual(message, label + ': service returned HTTP 403')
            for secret in ('secret', 'private', 'https:', '403 body'):
                self.assertNotIn(secret, message)

    def test_bad_auth_never_prints_response(self):
        t = p.Transport(Opener([{}, {'customToken': ''}]))
        with self.assertRaises(p.ProbeError) as error:
            t.authenticate('person@example.test', lambda: 'secret-otp')
        self.assertNotIn('secret-otp', str(error.exception))

class SummaryTests(unittest.TestCase):
    def pack(self, category='Special Pack', **changes):
        pack = dict(templateId='template-1', type=category, name='Tuesday Special', purchaseLimit='2', discountPrice=1234,
                    redeemableFromDate='2026-11-03')
        pack.update(changes)
        return {'type': category, 'powerpack': pack, 'error': None}

    def test_approved_categories_and_cents(self):
        entries = [self.pack(c) for c in sorted(p.APPROVED)] + [self.pack('Value Pack'), self.pack('Standard Power')]
        results, unknown = p.catalogue_summary({'data': entries})
        self.assertEqual(len(results), 4)
        self.assertEqual(unknown, 0)
        self.assertEqual(results[0]['unit_price'], '$12.34')
        self.assertEqual(results[0]['quantity'], 2)
        self.assertEqual(p.money(0), '$0.00')
        self.assertIsNone(p.money(True))
        self.assertIsNone(p.money(-1))

    def test_unknown_or_unavailable_fail_closed(self):
        entries = [self.pack(purchaseLimit=None), self.pack(purchaseLimit='2extra'),
                   self.pack(discountPrice=1.2), self.pack(redeemableFromDate=12),
                   self.pack(templateId=''), self.pack(redeemableFromDate='2026-11-01\n\x1b[2J'),
                   {'type': 'EV Pack', 'powerpack': None, 'error': {'message': 'unavailable'}}, None]
        self.assertEqual(p.catalogue_summary({'data': entries}), ([], 8))
        with self.assertRaises(p.ProbeError): p.catalogue_summary({'data': None})

    def test_zero_quantity_and_missing_redemption_date(self):
        result, unknown = p.catalogue_summary({'data': [self.pack(purchaseLimit='0', redeemableFromDate=None)]})
        self.assertEqual(result[0]['quantity'], 0)
        self.assertIsNone(result[0]['redeemable_from'])
        self.assertEqual(unknown, 0)

    def test_cart_details_matching_and_redaction(self):
        def item(template_id='private-template', name='UFO'):
            return {'templateId': template_id, 'quantity': 1, 'itemRemoved': False,
                    'itemResized': False, 'quantityAdjusted': False,
                    'powerpack': {'type': 'Special Pack', 'name': name, 'discountPrice': 3126,
                                  'quantityAvailable': 2}}
        entries = [item(), item('other-private-id'), item(name='unsafe\nname'),
                   dict(item(), itemRemoved=True), {'quantity': True}]
        lines = p.cart_summary({'data': {'items': entries}}, {'private-template'})
        self.assertIn('Special Pack | UFO | quantity 1 | $31.26 each | server quantityAvailable 2 | matches listed offer', lines[0])
        self.assertIn('not among listed offers', lines[1])
        self.assertIn('malformed', lines[2])
        self.assertIn('item removed', lines[3])
        self.assertIn('UFO', lines[3])
        self.assertIn('malformed', lines[4])
        self.assertNotIn('private', str(lines))
        self.assertNotIn('unsafe', str(lines))
        with self.assertRaises(p.ProbeError): p.cart_summary({'data': {'items': None}})

    def test_main_renders_unknown_date_without_identifiers(self):
        entry = self.pack(templateId='private-template', redeemableFromDate=None)
        class Client:
            def authenticate(self, email, otp_reader): pass
            def discover_accounts(self): return 1
            def select_account(self, index): pass
            def catalogue(self): return {'data': [entry]}
            def purchased_page(self, cursor=None):
                return {'data': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
        out = io.StringIO()
        with patch('builtins.input', return_value='user@example.test'), patch.object(p, 'Transport', return_value=Client()), redirect_stdout(out):
            self.assertEqual(p.main([]), 0)
        self.assertIn('redeemable not supplied', out.getvalue())
        self.assertNotIn('private-template', out.getvalue())
        self.assertNotIn('user@example.test', out.getvalue())

    def test_no_network_until_nonempty_email(self):
        with patch('builtins.input', return_value=''), patch.object(p.Transport, '_request', side_effect=AssertionError):
            with patch('sys.stderr', new=io.StringIO()):
                self.assertEqual(p.main([]), 1)

    def test_import_has_no_network(self):
        import importlib.util
        with patch('urllib.request.build_opener', side_effect=AssertionError):
            spec = importlib.util.spec_from_file_location('isolated_probe', p.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

    def test_help_does_not_authenticate(self):
        with patch.object(p.Transport, 'authenticate', side_effect=AssertionError), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result: p.main(['--help'])
        self.assertEqual(result.exception.code, 0)

class HistoryTests(unittest.TestCase):
    def record(self):
        return {'id': 'private-id', 'purchasedAt': '2026-10-01', 'redeemableFrom': '2026-11-01',
                'voucherValue': 1200, 'balance': 500, 'amountPaid': None,
                'template': {'id': 'template', 'name': 'Pack', 'type': 'EV Pack', 'image': ''},
                'displayName': None}

    def page(self, records, more, cursor):
        return {'data': records, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}

    def test_multiple_pages_and_unrecognized_record(self):
        class Client:
            def __init__(self, pages): self.pages, self.cursors = pages, []
            def purchased_page(self, cursor=None):
                self.cursors.append(cursor)
                return self.pages.pop(0)
        client = Client([self.page([self.record()], True, 'next+/='), self.page([{}], False, None)])
        self.assertEqual(p.purchased_summary(client), (2, 1))
        self.assertEqual(client.cursors, [None, 'next+/='])

    def test_missing_invalid_and_repeated_cursor(self):
        class Client:
            def __init__(self, pages): self.pages = pages
            def purchased_page(self, cursor=None): return self.pages.pop(0)
        cases = [[self.page([], True, None)], [self.page([], True, '\n')],
                 [self.page([], True, 'same'), self.page([], True, 'same')],
                 [{'data': [], 'pageInfo': {'hasNextPage': 'true', 'endCursor': 'x'}}],
                 [{'data': [], 'pageInfo': {'hasNextPage': False}}],
                 [{'data': None, 'pageInfo': {}}]]
        for pages in cases:
            with self.subTest(pages=pages), self.assertRaises(p.ProbeError):
                p.purchased_summary(Client(pages))

    def test_page_and_record_bounds(self):
        class Client:
            def __init__(self): self.n = 0
            def purchased_page(self, cursor=None):
                self.n += 1
                return {'data': [], 'pageInfo': {'hasNextPage': True, 'endCursor': str(self.n)}}
        with patch.object(p, 'MAX_PAGES', 2), self.assertRaises(p.ProbeError):
            p.purchased_summary(Client())
        class Large:
            def purchased_page(self, cursor=None): return {'data': [{}] * 101, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
        with self.assertRaises(p.ProbeError): p.purchased_summary(Large())
        class Two:
            def purchased_page(self, cursor=None): return {'data': [{}, {}], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
        with patch.object(p, 'MAX_RECORDS', 1), self.assertRaises(p.ProbeError): p.purchased_summary(Two())

    def test_record_schema_fails_closed(self):
        record = self.record()
        self.assertTrue(p.valid_purchased_record(record))
        for key, value in [('balance', True), ('amountPaid', -1), ('template', {}), ('displayName', 4)]:
            bad = dict(record, **{key: value})
            self.assertFalse(p.valid_purchased_record(bad))
        record.pop('template')
        self.assertFalse(p.valid_purchased_record(record))

class AccountValidationTests(unittest.TestCase):
    def test_malformed_account_ids_are_refused(self):
        for accounts in ([{'number': 'bad\nheader'}], [{'number': 'x'}, {'number': 'x'}], [{'number': None}], [{'number': 'x\ty'}], [{'number': 'x\x00y'}], [{'number': 'x' * 129}]):
            t = p.Transport(Opener([{'data': {'viewer': {'accounts': accounts}}}]))
            t._token = 'token'
            with self.subTest(accounts=accounts), self.assertRaises(p.ProbeError): t.discover_accounts()

class RememberTests(unittest.TestCase):
    def test_cache_permissions_rotation_and_forget(self):
        with tempfile.TemporaryDirectory() as home:
            cache = p.TokenCache(home)
            self.assertIsNone(cache.load())
            cache.save('synthetic-first')
            path = os.path.join(home, '.config', 'powershopnz-probe', 'session.json')
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
            with open(path) as stream:
                self.assertEqual(json.load(stream), {'version': 1, 'refresh_token': 'synthetic-first'})
            cache.save('synthetic-rotated')
            self.assertEqual(cache.load(), 'synthetic-rotated')
            cache.forget()
            self.assertIsNone(cache.load())

    def test_cache_rejects_permissions_corruption_size_and_symlinks(self):
        with tempfile.TemporaryDirectory() as home:
            cache = p.TokenCache(home)
            cache.save('synthetic')
            path = os.path.join(home, '.config', 'powershopnz-probe', 'session.json')
            os.chmod(path, 0o644)
            with self.assertRaises(p.ProbeError): cache.load()
            os.chmod(path, 0o600)
            with open(path, 'w') as stream: stream.write('{')
            with self.assertRaises(p.ProbeError): cache.load()
            cache.forget()
            self.assertIsNone(cache.load())
            cache.save('synthetic')
            with open(path, 'w') as stream: stream.write('x' * (p.CACHE_LIMIT + 1))
            with self.assertRaises(p.ProbeError): cache.load()
            os.unlink(path)
            os.symlink('/dev/null', path)
            with self.assertRaises(p.ProbeError): cache.load()
            os.unlink(path)
            os.rmdir(os.path.dirname(path))
            os.symlink('/tmp', os.path.dirname(path))
            with self.assertRaises(p.ProbeError): cache.load()

    def test_refresh_form_and_strict_allowlist(self):
        opener = Opener([{'id_token': 'new-id', 'refresh_token': 'rotated'}])
        client = p.Transport(opener)
        self.assertEqual(client.refresh_login('old-token'), 'rotated')
        req = opener.requests[0]
        self.assertEqual(req.get_header('Content-type'), 'application/x-www-form-urlencoded')
        self.assertIsNone(req.get_header('Authorization'))
        self.assertEqual(urllib.parse.parse_qs(req.data.decode()), {'grant_type': ['refresh_token'], 'refresh_token': ['old-token']})
        for body in ({'grant_type': 'password', 'refresh_token': 'x'}, {'grant_type': 'refresh_token', 'refresh_token': 'x', 'extra': 'x'}):
            with self.assertRaises(p.ProbeError): client._request('POST', p.REFRESH, body)

    def test_revocation_vs_transient(self):
        class Failed:
            def __init__(self, status, content): self.status, self.content = status, content
            def open(self, req, timeout): raise urllib.error.HTTPError(req.full_url, self.status, 'failure', {}, io.BytesIO(self.content))
        revoked = p.Transport(Failed(400, b'{"error":{"message":"INVALID_REFRESH_TOKEN"}}'))
        with self.assertRaises(p.RevokedRefresh): revoked.refresh_login('synthetic')
        for status, content in [(503, b'{"error":{"message":"INVALID_REFRESH_TOKEN"}}'), (400, b'{"error":{"message":"CONFIGURATION_NOT_FOUND"}}')]:
            client = p.Transport(Failed(status, content))
            with self.assertRaises(p.ProbeError) as error: client.refresh_login('synthetic')
            self.assertNotIsInstance(error.exception, p.RevokedRefresh)

    def test_main_remember_repeat_revoked_transient_and_forget(self):
        class Client:
            def __init__(self, mode): self.mode, self._token = mode, None
            def authenticate(self, email, reader): self._token = 'id'; return 'first-refresh'
            def refresh_login(self, token):
                if self.mode == 'revoked': raise p.RevokedRefresh('revoked')
                if self.mode == 'transient': raise p.ProbeError('refreshing login: failed')
                self._token = 'id'; return 'rotated-refresh'
            def discover_accounts(self): raise p.ProbeError('end test before account request')
        with tempfile.TemporaryDirectory() as home:
            cache = p.TokenCache(home)
            with patch.object(p, 'TokenCache', return_value=cache), patch('builtins.input', return_value='test@example.test') as prompt, patch('sys.stderr', new=io.StringIO()), redirect_stdout(io.StringIO()):
                with patch.object(p, 'Transport', return_value=Client('first')): self.assertEqual(p.main(['--remember-login']), 1)
                self.assertEqual(cache.load(), 'first-refresh')
                self.assertEqual(prompt.call_count, 1)
                prompt.reset_mock()
                with patch.object(p, 'Transport', return_value=Client('repeat')): self.assertEqual(p.main(['--remember-login']), 1)
                self.assertEqual(cache.load(), 'rotated-refresh')
                prompt.assert_not_called()
                with patch.object(p, 'Transport', return_value=Client('transient')): self.assertEqual(p.main(['--remember-login']), 1)
                self.assertEqual(cache.load(), 'rotated-refresh')
                prompt.assert_not_called()
                with patch.object(p, 'Transport', return_value=Client('revoked')): self.assertEqual(p.main(['--remember-login']), 1)
                self.assertEqual(cache.load(), 'first-refresh')
                self.assertEqual(prompt.call_count, 1)
                prompt.reset_mock()
                with patch.object(p, 'Transport', side_effect=AssertionError): self.assertEqual(p.main(['--forget-login']), 0)
                self.assertIsNone(cache.load())
                prompt.assert_not_called()

class PayLaterPlanTests(unittest.TestCase):
    def setUp(self):
        self.offer = {'_template_id': 'sensitive-template', '_price_cents': 3126, 'category': 'Special Pack'}
        self.cart = {'data': {'version': 7,
                    'errors': {'itemRemoved': False, 'itemResized': False, 'quantityAdjusted': False, 'versionMismatch': False},
                    'items': [{'templateId': 'sensitive-template', 'quantity': 1, 'itemRemoved': False,
                               'itemResized': False, 'quantityAdjusted': False,
                               'powerpack': {'type': 'Special Pack', 'name': 'UFO', 'discountPrice': 3126, 'quantityAvailable': 1}}],
                    'costSummary': {'pricing': {'discountTotal': 3126},
                                    'credit': {'available': 0, 'applied': 0, 'remaining': 0},
                                    'totalToPay': {'withoutCreditCardFee': 3126}}}}

    def test_valid_plan_is_local_and_purchase_transport_refuses(self):
        with patch.object(p.Transport, '_request', side_effect=AssertionError):
            plan = p.plan_pay_later(self.cart, [self.offer])
        self.assertEqual((plan.amount, plan.version), (3126, 7))
        self.assertEqual(uuid.UUID(plan.idempotency_key).version, 4)
        self.assertEqual(plan.payload(), {'amount': 3126, 'idempotencyKey': plan.idempotency_key, 'version': 7})
        self.assertNotEqual(plan.idempotency_key, p.plan_pay_later(self.cart, [self.offer]).idempotency_key)
        with self.assertRaises(p.ProbeError):
            p.Transport(Opener([]))._request('POST', p.SHOP + '/v1/carts/current/pay-later',
                {'amount': plan.amount, 'idempotencyKey': plan.idempotency_key, 'version': plan.version})

    def test_positive_credit_and_quote(self):
        self.cart['data']['costSummary']['credit'] = {'available': 500, 'applied': 300, 'remaining': 200}
        self.cart['data']['costSummary']['totalToPay']['withoutCreditCardFee'] = 2826
        self.assertEqual(p.plan_pay_later(self.cart, [self.offer]).amount, 2826)

    def test_invalid_cart_fail_closed(self):
        variants = []
        def change(fn):
            cart = copy.deepcopy(self.cart)
            fn(cart['data'])
            variants.append(cart)
        change(lambda d: d.update(items=[]))
        change(lambda d: d.update(version=True))
        change(lambda d: d['errors'].update(versionMismatch=True))
        change(lambda d: d['errors'].pop('itemRemoved'))
        change(lambda d: d['items'][0].update(itemResized=True))
        change(lambda d: d['items'][0].update(templateId='unlisted'))
        change(lambda d: d['items'].append(copy.deepcopy(d['items'][0])))
        change(lambda d: d['items'][0]['powerpack'].update(type='Standard Power'))
        change(lambda d: d['items'][0].update(quantity=2))
        change(lambda d: d['items'][0].update(quantity=0))
        change(lambda d: d['items'][0]['powerpack'].update(discountPrice=3127))
        change(lambda d: d['costSummary']['pricing'].update(discountTotal=3125))
        change(lambda d: d['costSummary']['credit'].update(remaining=2))
        change(lambda d: d['costSummary']['totalToPay'].update(withoutCreditCardFee=0))
        change(lambda d: d['costSummary']['credit'].pop('applied'))
        for cart in variants:
            with self.subTest(cart=cart), self.assertRaises(p.ProbeError): p.plan_pay_later(cart, [self.offer])
        with self.assertRaises(p.ProbeError): p.plan_pay_later(self.cart, [self.offer, self.offer])
        zero = copy.deepcopy(self.cart)
        zero['data']['costSummary']['credit'] = {'available': 3126, 'applied': 3126, 'remaining': 0}
        zero['data']['costSummary']['totalToPay']['withoutCreditCardFee'] = 0
        with self.assertRaises(p.ProbeError): p.plan_pay_later(zero, [self.offer])

    def test_recoverable_change_rereads_once_and_uses_fresh_values(self):
        initial = copy.deepcopy(self.cart)
        initial['data']['items'][0]['itemResized'] = True
        initial['data']['items'][0]['powerpack']['discountPrice'] = 3047
        initial['data']['costSummary']['pricing']['discountTotal'] = 3047
        initial['data']['costSummary']['totalToPay']['withoutCreditCardFee'] = 3047
        fresh = copy.deepcopy(initial)
        fresh['data']['items'][0]['itemResized'] = False
        class Client:
            def __init__(self): self.catalogues, self.carts = 0, 0
            def authenticate(self, email, otp_reader): pass
            def discover_accounts(self): return 1
            def select_account(self, index): pass
            def catalogue(self):
                self.catalogues += 1
                return {'data': [{'type': 'Special Pack', 'error': None,
                    'powerpack': {'templateId': 'sensitive-template', 'name': 'UFO', 'type': 'Special Pack',
                                  'purchaseLimit': '1', 'discountPrice': 3126 if self.catalogues == 1 else 3047}}]}
            def purchased_page(self, cursor=None): return {'data': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
            def cart(self):
                self.carts += 1
                return initial if self.carts == 1 else fresh
        client, out = Client(), io.StringIO()
        with patch('builtins.input', return_value='synthetic@example.test'), patch.object(p, 'Transport', return_value=client), redirect_stdout(out):
            self.assertEqual(p.main(['--select-pay-later']), 0)
        self.assertEqual((client.catalogues, client.carts), (2, 2))
        self.assertIn('refreshed catalogue and cart once', out.getvalue())
        self.assertIn('amount $30.47', out.getvalue())
        self.assertNotIn('$31.26', out.getvalue())
        self.assertNotIn('sensitive-template', out.getvalue())

    def test_retry_bounded_and_other_failures_are_not_retried(self):
        changed = copy.deepcopy(self.cart)
        changed['data']['errors']['versionMismatch'] = True
        self.assertTrue(p.retryable_cart(changed, [self.offer]))
        with self.assertRaisesRegex(p.ProbeError, 'version mismatch'):
            p.plan_pay_later(changed, [self.offer])
        cases = []
        removed = copy.deepcopy(changed); removed['data']['items'][0]['itemRemoved'] = True; cases.append(removed)
        malformed = copy.deepcopy(changed); malformed['data']['items'][0].pop('quantity'); cases.append(malformed)
        unapproved = copy.deepcopy(changed); unapproved['data']['items'][0]['powerpack']['type'] = 'Standard Power'; cases.append(unapproved)
        unlisted = copy.deepcopy(changed); unlisted['data']['items'][0]['templateId'] = 'unknown'; cases.append(unlisted)
        over = copy.deepcopy(changed); over['data']['items'][0]['quantity'] = 2; cases.append(over)
        quote = copy.deepcopy(changed); quote['data']['costSummary']['pricing']['discountTotal'] = 2; cases.append(quote)
        self.assertTrue(all(not p.retryable_cart(cart, [self.offer]) for cart in cases))
        line = copy.deepcopy(self.cart['data']['items'][0]); line['itemResized'] = True; line['quantityAdjusted'] = True
        text = p.cart_summary({'data': {'items': [line]}}, {'sensitive-template'})[0]
        self.assertIn('item resized, quantity adjusted', text)
        self.assertIn('UFO', text)
        self.assertNotIn('sensitive-template', text)

    def test_persistent_change_only_retries_once(self):
        cart = copy.deepcopy(self.cart)
        cart['data']['errors']['quantityAdjusted'] = True
        class Client:
            def __init__(self): self.catalogues, self.carts = 0, 0
            def authenticate(self, email, otp_reader): pass
            def discover_accounts(self): return 1
            def select_account(self, index): pass
            def catalogue(self):
                self.catalogues += 1
                return {'data': [{'type': 'Special Pack', 'error': None, 'powerpack': {'templateId': 'sensitive-template',
                    'name': 'UFO', 'type': 'Special Pack', 'purchaseLimit': '1', 'discountPrice': 3126}}]}
            def purchased_page(self, cursor=None): return {'data': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
            def cart(self): self.carts += 1; return cart
        client, out, err = Client(), io.StringIO(), io.StringIO()
        with patch('builtins.input', return_value='test@example.test'), patch.object(p, 'Transport', return_value=client), redirect_stdout(out), patch('sys.stderr', err):
            self.assertEqual(p.main(['--select-pay-later']), 1)
        self.assertEqual((client.catalogues, client.carts), (2, 2))
        self.assertIn('quantity adjusted', err.getvalue())
        self.assertNotIn('sensitive-template', out.getvalue() + err.getvalue())

    def test_selection_display_does_not_send_or_expose_ids(self):
        cart = self.cart
        class Client:
            def authenticate(self, email, otp_reader): pass
            def discover_accounts(self): return 1
            def select_account(self, index): pass
            def catalogue(self): return {'data': [{'type': 'Special Pack', 'error': None,
                'powerpack': {'templateId': 'sensitive-template', 'name': 'UFO', 'type': 'Special Pack',
                              'purchaseLimit': '1', 'discountPrice': 3126}}]}
            def purchased_page(self, cursor=None): return {'data': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
            def cart(self): return cart
        out = io.StringIO()
        with patch('builtins.input', return_value='secret@example.test'), patch.object(p, 'Transport', return_value=Client()), redirect_stdout(out):
            self.assertEqual(p.main(['--select-pay-later']), 0)
        text = out.getvalue()
        self.assertIn('Selected payment option: Pay later (local dry-run)', text)
        self.assertIn('POST /v1/carts/current/pay-later | amount $31.26 | cart version 7', text)
        self.assertIn('checkout acceptance not tested', text)
        self.assertNotIn('sensitive-template', text)
        self.assertNotIn('secret@example.test', text)

class PurchaseTests(PayLaterPlanTests):
    def client(self, changed=None, result=None):
        cart, offer = self.cart, self.offer
        class Client:
            _token, _accounts, _account = 'secret-token', ('acct-1',), 'acct-1'
            def __init__(self): self.posts = []
            def catalogue(self):
                return {'data': [{'type': 'Special Pack', 'error': None, 'powerpack': {'templateId': offer['_template_id'],
                    'name': 'UFO', 'type': 'Special Pack', 'purchaseLimit': '1', 'discountPrice': 3126}}]}
            def cart(self):
                result_cart = copy.deepcopy(cart)
                if changed: changed(self, result_cart)
                return result_cart
            def _request(self, method, url, body):
                self.posts.append((method, url, body))
                if isinstance(result, BaseException): raise result
                return result if result is not None else {'data': {'orderId': 'order-1'}}
        return Client()

    def offer_with_name(self): return dict(self.offer, name='UFO')

    def run_purchase(self, client, home, confirmation='PURCHASE 31.26', tty=True):
        plan = p.plan_pay_later(self.cart, [self.offer])
        with patch('sys.stdin.isatty', return_value=tty), patch('builtins.input', return_value=confirmation), redirect_stdout(io.StringIO()):
            return p.execute_purchase(client, self.cart, [self.offer_with_name()], plan, lambda: p.PurchaseJournal(home))

    def test_live_transport_exact_allowlist_and_headers(self):
        opener = Opener([{'data': {'orderId': 'order-1'}}])
        body = {'amount': 3126, 'idempotencyKey': str(uuid.uuid4()), 'version': 7}
        client = p.Transport(opener, purchase_enabled=True)
        with self.assertRaises(p.ProbeError): client._request('POST', p.PURCHASE, body)
        client._token, client._accounts, client._account = 'secret-token', ('acct-1',), 'acct-1'
        self.assertEqual(client._request('POST', p.PURCHASE, body)['data']['orderId'], 'order-1')
        req = opener.requests[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(json.loads(req.data), body)
        self.assertEqual(req.get_header('Authorization'), 'Bearer secret-token')
        self.assertEqual(req.get_header('X-account-number'), 'acct-1')
        self.assertEqual(req.get_header('Content-type'), 'application/json')
        for method, url, payload in [('POST', p.PURCHASE + '?x=1', body), ('PATCH', p.SHOP + '/v1/carts/current', body),
                                     ('POST', p.SHOP + '/v1/carts/current/pay-now', body), ('POST', p.PURCHASE, dict(body, amount=True)),
                                     ('POST', p.PURCHASE, dict(body, idempotencyKey='bad')), ('POST', p.PURCHASE, dict(body, extra=1))]:
            with self.subTest(url=url), self.assertRaises(p.ProbeError): client._request(method, url, payload)
        self.assertEqual(len(opener.requests), 1)
        with self.assertRaises(p.ProbeError): p.Transport(Opener([]))._request('POST', p.PURCHASE, body)

    def test_confirmation_and_non_terminal_do_not_post(self):
        for confirmation, tty in [('no', True), ('PURCHASE 31.27', True), ('PURCHASE 31.26', False)]:
            with tempfile.TemporaryDirectory() as home:
                client = self.client()
                with self.assertRaises(p.ProbeError): self.run_purchase(client, home, confirmation, tty)
                self.assertEqual(client.posts, [])
                with p.PurchaseJournal(home) as journal: self.assertEqual(journal.records, [])
        with tempfile.TemporaryDirectory() as home:
            client = self.client()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=EOFError):
                with self.assertRaises(EOFError): p.execute_purchase(client, self.cart, [self.offer_with_name()], p.plan_pay_later(self.cart, [self.offer]), lambda: p.PurchaseJournal(home))
            self.assertEqual(client.posts, [])

    def test_success_is_recorded_and_same_quote_blocked(self):
        with tempfile.TemporaryDirectory() as home:
            cache = p.TokenCache(home); cache.save('synthetic-refresh')
            client = self.client(); self.run_purchase(client, home)
            self.assertEqual(len(client.posts), 1)
            with p.PurchaseJournal(home) as journal:
                self.assertEqual(journal.records[0]['state'], 'success')
                self.assertEqual(journal.records[0]['orderId'], 'order-1')
                self.assertEqual(journal.records[0]['payload'], client.posts[0][2])
                self.assertTrue(p.valid_attempt_time(journal.records[0]['createdAt']))
                self.assertEqual(journal.records[0]['items'], [{'templateId': 'sensitive-template', 'type': 'Special Pack',
                    'name': 'UFO', 'quantity': 1, 'unitPrice': 3126}])
                self.assertNotIn('secret-token', json.dumps(journal.records))
                self.assertNotIn('synthetic-refresh', json.dumps(journal.records))
            self.assertEqual(cache.load(), 'synthetic-refresh')
            with self.assertRaises(p.ProbeError): self.run_purchase(client, home)
            self.assertEqual(len(client.posts), 1)

    def test_unknown_outcomes_block_next_attempt(self):
        for result in (p.ProbeError('secret 503'), p.ProbeError('timeout'), {'data': {}}, {'data': {'orderId': 'bad\nsecret'}}):
            with self.subTest(result=type(result).__name__), tempfile.TemporaryDirectory() as home:
                client = self.client(result=result)
                with self.assertRaises(p.ProbeError) as error: self.run_purchase(client, home)
                self.assertIn('UNKNOWN', str(error.exception))
                self.assertNotIn('secret', str(error.exception))
                self.assertEqual(len(client.posts), 1)
                with self.assertRaises(p.ProbeError): self.run_purchase(client, home)
                self.assertEqual(len(client.posts), 1)

    def test_journal_rejects_invalid_snapshot_and_timestamp(self):
        with tempfile.TemporaryDirectory() as home:
            client = self.client(); self.run_purchase(client, home)
            path = os.path.join(home, '.config', 'powershopnz-probe', 'purchases.json')
            with open(path) as stream: records = json.load(stream)
            for field, value in [('createdAt', 'not-a-date'), ('items', []),
                                 ('items', [{'templateId': 'x', 'type': 'Special Pack', 'name': 'bad\nname', 'quantity': 1, 'unitPrice': 1}])]:
                broken = copy.deepcopy(records); broken[0][field] = value
                with open(path, 'w') as stream: json.dump(broken, stream)
                with self.assertRaises(p.ProbeError):
                    with p.PurchaseJournal(home): pass

    def test_lock_blocks_concurrent_purchase(self):
        with tempfile.TemporaryDirectory() as home:
            with p.PurchaseJournal(home):
                with self.assertRaises(p.ProbeError):
                    with p.PurchaseJournal(home): pass

    def test_stale_cart_or_account_never_posts(self):
        def price(c, cart): cart['data']['items'][0]['powerpack']['discountPrice'] = 2
        def version(c, cart): cart['data']['version'] += 1
        def quantity(c, cart): cart['data']['items'][0]['quantity'] = 2
        def account(c, cart): c._account = 'other'
        def credit(c, cart): cart['data']['costSummary']['credit']['available'] = 3
        for change in (price, version, quantity, account, credit):
            with self.subTest(change=change.__name__), tempfile.TemporaryDirectory() as home:
                client = self.client(changed=change)
                with self.assertRaises(p.ProbeError): self.run_purchase(client, home)
                self.assertEqual(client.posts, [])

    def test_success_record_write_failure_reports_remote_success(self):
        with tempfile.TemporaryDirectory() as home:
            client = self.client()
            with patch.object(p.PurchaseJournal, 'success', side_effect=p.ProbeError('local failure')):
                with self.assertRaises(p.ProbeError) as error: self.run_purchase(client, home)
            self.assertIn('purchase succeeded', str(error.exception))
            self.assertEqual(len(client.posts), 1)
            with self.assertRaises(p.ProbeError): self.run_purchase(client, home)
            self.assertEqual(len(client.posts), 1)

if __name__ == '__main__': unittest.main()
