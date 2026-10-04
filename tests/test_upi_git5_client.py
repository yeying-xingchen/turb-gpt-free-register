"""Contract tests: synthetic HTTP responses only, never real CDKs or account ATs."""
import base64
import json
import unittest
from unittest.mock import patch

import requests

from core.upi_git5_client import UpiGit5Client, UpiGit5Error, SESSION_HEADER

CDK = 'private-cdk-1234'
SESSION = 'private-session-token'
PROXY = 'http://private-user:private-pass@proxy.test:8080'
TOKENS = ['private-account-token-1', 'private-account-token-2']
PNG = b'\x89PNG\r\n\x1a\n' + b'example-image-content'


def response(payload=None, status=200, content=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = content if content is not None else json.dumps(payload).encode()
    result.headers.update(headers or {})
    return result


def accepted():
    return {'ok': True, 'batch_id': 'batch-A', 'batch_size': 2,
            'jobs': [{'index': 0, 'account_email': 'a1@example.test', 'job_id': 'job-1', 'queue_position': 0},
                     {'index': 1, 'account_email': 'a2@example.test', 'job_id': 'job-2', 'queue_position': 1}]}


class UpiGit5ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = UpiGit5Client('https://upi.example.test/', timeout=8)
        patcher = patch('requests.sessions.Session.request')
        self.http = patcher.start()
        self.addCleanup(patcher.stop)
        self.http.return_value = response(accepted())

    def batch(self, **kwargs):
        return self.client.create_batch(**{'session_token': SESSION, 'tokens': TOKENS,
            'entry_proxies': [PROXY], 'link_cdk': CDK, **kwargs})

    def test_contract_uses_one_post_accepts_different_proxy_count_and_preserves_options(self):
        self.assertEqual(self.batch(use_promo=False, promo_campaign='trial', payment_provider_id='astrascan')['batch_id'], 'batch-A')
        self.http.assert_called_once()
        args, kwargs = self.http.call_args
        self.assertEqual(args, ('POST', 'https://upi.example.test/api/upi-git5/batch'))
        self.assertEqual(kwargs['headers'][SESSION_HEADER], SESSION)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], 8)
        self.assertEqual(kwargs['json'], {'tokens': TOKENS, 'entry_proxies': [PROXY], 'link_cdk': CDK,
                                         'use_promo': False, 'promo_campaign': 'trial', 'payment_provider_id': 'astrascan'})

    def test_optional_defaults_are_left_to_upstream(self):
        self.batch()
        self.assertNotIn('use_promo', self.http.call_args.kwargs['json'])
        self.assertNotIn('payment_provider_id', self.http.call_args.kwargs['json'])

    def test_bad_input_never_reaches_http(self):
        for kwargs in ({'tokens': []}, {'tokens': ['']}, {'tokens': [123]}, {'entry_proxies': ['']},
                       {'entry_proxies': None}, {'link_cdk': 123}, {'link_cdk': ''}, {'session_token': '\r\n'},
                       {'use_promo': 'false'}, {'payment_provider_id': 'other'}, {'promo_campaign': 5}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.batch(**kwargs)
                self.http.assert_not_called()

    def test_submit_timeout_is_uncertain_and_not_retried(self):
        self.http.side_effect = requests.Timeout('debug contains ' + CDK)
        with self.assertRaises(UpiGit5Error) as caught:
            self.batch()
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(CDK, str(caught.exception))
        self.http.assert_called_once()

    def test_malformed_success_keeps_batch_identifier_for_recovery(self):
        for mutate in (lambda p: p.pop('jobs'), lambda p: p.update(batch_size=3), lambda p: p.update(jobs=[]),
                       lambda p: p['jobs'][0].update(job_id=''), lambda p: p['jobs'][0].update(job_id='job-2'),
                       lambda p: p.update(ok='true')):
            payload = accepted()
            mutate(payload)
            self.http.return_value = response(payload)
            with self.subTest(payload=payload), self.assertRaises(UpiGit5Error) as caught:
                self.batch()
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(caught.exception.batch_id, 'batch-A')

    def test_non_json_and_non_object_success_are_uncertain(self):
        for content in (b'<html>gateway</html>', b'[]', b'null'):
            self.http.return_value = response(content=content)
            with self.assertRaises(UpiGit5Error) as caught:
                self.batch()
            self.assertTrue(caught.exception.uncertain)

    def test_errors_redact_all_request_credentials_and_code(self):
        secrets = [CDK, SESSION, PROXY, *TOKENS]
        self.http.return_value = response({'ok': False, 'error': {'message': ' '.join(secrets), 'code': SESSION}}, status=503)
        with self.assertRaises(UpiGit5Error) as caught:
            self.batch()
        for secret in secrets:
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(secret, caught.exception.code or '')
        self.assertTrue(caught.exception.uncertain)

    def test_ok_false_on_success_http_is_rejected(self):
        self.http.return_value = response({'ok': False, 'error': {'message': 'No quota', 'code': 'quota'}})
        with self.assertRaises(UpiGit5Error) as caught:
            self.batch()
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, 'quota')

    def test_rejection_and_rate_limit_are_not_ambiguous_submission(self):
        self.http.return_value = response({'ok': False, 'error': 'busy'}, status=429, headers={'Retry-After': '7'})
        with self.assertRaises(UpiGit5Error) as caught:
            self.batch()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 7)
        self.assertFalse(caught.exception.uncertain)

    def test_read_failure_redacts_session(self):
        self.http.return_value = response({'ok': False, 'error': SESSION}, status=401)
        with self.assertRaises(UpiGit5Error) as caught:
            self.client.get_batch_progress(SESSION, 'batch/A')
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn(SESSION, str(caught.exception))
        self.assertIn('/batch/batch%2FA/progress', self.http.call_args.args[1])

    def test_qr_returns_png_bytes_and_uses_tenant_header(self):
        self.http.return_value = response(content=PNG, headers={'Content-Type': 'image/png'})
        self.assertEqual(self.client.checkout_qr(SESSION, 'job-1'), PNG)
        self.assertEqual(self.http.call_args.kwargs['headers'], {'Accept': 'image/png', SESSION_HEADER: SESSION})
        self.assertEqual(self.http.call_args.kwargs['params'], {'job_id': 'job-1'})

    def test_qr_strict_base64_body_compatibility_and_invalid_image_rejection(self):
        self.http.return_value = response(content=base64.b64encode(PNG))
        self.assertEqual(self.client.checkout_qr(SESSION, 'job-1'), PNG)
        for content in (b'<html>login</html>', b'not an image', json.dumps({'qr': base64.b64encode(PNG).decode()}).encode()):
            self.http.return_value = response(content=content)
            with self.assertRaises(UpiGit5Error):
                self.client.checkout_qr(SESSION, 'job-1')

    def test_cdk_session_contract_and_missing_token(self):
        self.http.return_value = response({'ok': True, 'token': SESSION, 'expires_at': 999, 'link_cdk': {'remaining_uses': 5}})
        self.assertEqual(self.client.create_session(CDK)['token'], SESSION)
        self.assertEqual(self.http.call_args.kwargs['json'], {'code': CDK})
        self.assertNotIn(SESSION_HEADER, self.http.call_args.kwargs['headers'])
        self.http.return_value = response({'ok': True})
        with self.assertRaises(UpiGit5Error):
            self.client.create_session(CDK)

    def test_cancel_and_logout_use_documented_post_paths(self):
        self.http.return_value = response({'ok': True})
        self.client.cancel_checkout(SESSION, 'job-1')
        self.assertTrue(self.http.call_args.args[1].endswith('/api/checkout-cancel'))
        self.assertEqual(self.http.call_args.kwargs['json'], {'job_id': 'job-1'})
        self.client.logout_session(SESSION)
        self.assertTrue(self.http.call_args.args[1].endswith('/api/link-cdk/session/logout'))

    def test_base_rejects_credentials_fragments_and_redirects(self):
        for base in ('https://user:pass@example.test', 'file:///tmp/test', 'https://example.test/?',
                     'https://example.test/#', 'https://example.test:0', 'https://example.test/\\path'):
            with self.assertRaises(ValueError):
                UpiGit5Client(base)
        self.http.return_value = response(status=302, content=b'', headers={'Location': 'https://other.example.test'})
        with self.assertRaises(UpiGit5Error):
            self.batch()
        self.http.assert_called_once()
