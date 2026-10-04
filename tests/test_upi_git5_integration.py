"""Exercise UPI batches against an isolated database and a fake upstream."""
import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from core import db, extract_link_service as service, extract_provider_store as store
from core.upi_git5_client import UpiGit5Error
from core.upi_git5_extract import bind_jobs
from webui.extract_routes import register_extract_routes

CDK = "UPI-CDK-private-test-credential"
PROXY = "http://user:password@proxy.example.test:8080"


class UpiGit5IntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = {k: root / k for k, v in vars(db).items() if k.startswith('_') and isinstance(v, Path)}
        paths.update(_DATA_DIR=root, _LOG_DIR=root / 'logs', _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        self.patch(patch.multiple(db, **paths))
        db._ensure_sqlite()
        self.provider = store.save_provider({'name': 'UPI-GIT5', 'provider_type': 'upi_git5', 'api_base': 'https://upi.example.test'})
        self.cdk = store.save_cdk(self.provider['id'], {'cdk': CDK})
        self.accounts = [{'account_id': i, 'email': f'a{i}@example.test', 'access_token': f'private-account-token-{i}'} for i in (1, 2)]
        for a in self.accounts:
            self.save({'id': a['account_id'], 'email': a['email'], 'access_token': a['access_token'],
                       'current_plan_type': 'free', 'plus_trial_eligible': True})
        self.slots = threading.BoundedSemaphore(1)
        self.patch(patch.object(service, '_QUEUE_SLOTS', self.slots))
        self.executor = self.patch(patch.object(service, '_EXECUTOR'))
        self.client = Mock()
        self.client.create_session.return_value = {'ok': True, 'token': 'private-session', 'expires_at': 9999999999, 'link_cdk': {}}
        self.client.create_batch.return_value = {'ok': True, 'batch_id': 'batch-A', 'batch_size': 2,
            'jobs': [{'index': 1, 'job_id': 'job-2'}, {'index': 0, 'job_id': 'job-1'}]}
        self.client.get_batch_progress.return_value = self.snapshot([self.job(2), self.job(1)])
        self.client.get_batch.return_value = self.client.get_batch_progress.return_value
        self.patch(patch.object(service, '_upi_git5_client', return_value=self.client))
        self.patch(patch.object(service.time, 'sleep'))
        self.patch(patch('requests.sessions.Session.request', side_effect=AssertionError('No live HTTP in integration tests')))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def save(self, account):
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute('INSERT OR REPLACE INTO accounts(id,email,payload) VALUES(?,?,?)',
                         (account['id'], account['email'], json.dumps(account)))

    def job(self, i, status='done', result=True):
        return {'id': f'job-{i}', 'batch_id': 'batch-A', 'status': status, 'percent': 100 if status == 'done' else 20,
                'result': {'url': f'https://payments.example.test/{i}'} if result else None,
                'payment': {'status': 'not_submitted'}}

    def snapshot(self, jobs, running=False):
        return {'batch_id': 'batch-A', 'running': running, 'jobs': jobs}

    def enqueue(self, **kwargs):
        return service.enqueue_account_extract_bulk(accounts=kwargs.pop('accounts', self.accounts),
            provider_id=self.provider['id'], cdk_id=kwargs.pop('cdk_id', self.cdk['id']),
            entry_proxies=kwargs.pop('entry_proxies', [PROXY]), **kwargs)

    def run_worker(self):
        call = self.executor.submit.call_args
        call.args[0](**call.kwargs)

    def assert_slot_released(self):
        self.assertTrue(self.slots.acquire(blocking=False))
        self.slots.release()

    def test_two_accounts_use_one_batch_and_ids_survive_reordered_progress(self):
        self.assertEqual(self.enqueue()['started_count'], 2)
        self.executor.submit.assert_called_once()
        self.client.create_batch.assert_not_called()  # all HTTP stays in background
        self.run_worker()
        body = self.client.create_batch.call_args.kwargs
        self.assertEqual(body['tokens'], [a['access_token'] for a in self.accounts])
        self.assertEqual(body['entry_proxies'], [PROXY])
        self.client.create_batch.assert_called_once()
        for i in (1, 2):
            row = db.get_account(i)
            self.assertEqual(row['extract_link_status'], 'success')
            self.assertEqual(row['extract_link_job_id'], f'job-{i}')
            self.assertEqual(row['extract_link_task_id'], 'batch-A')
            self.assertEqual(row['extract_link_long_url'], f'https://payments.example.test/{i}')
            self.assertEqual(row['extract_link_payment_status'], 'not_submitted')
        self.assert_slot_released()

    def test_single_account_uses_same_batch_path(self):
        a = self.accounts[0]
        result = service.enqueue_account_extract(**a, provider_id=self.provider['id'], cdk_id=self.cdk['id'], entry_proxies=[PROXY])
        self.assertTrue(result['accepted'])
        self.assertEqual(len(self.executor.submit.call_args.kwargs['entries']), 1)
        self.client.create_batch.return_value['jobs'] = [{'index': 0, 'job_id': 'job-1'}]
        self.client.get_batch_progress.return_value = self.snapshot([self.job(1)])
        self.run_worker()
        self.assertEqual(db.get_account(1)['extract_link_status'], 'success')
        self.assert_slot_released()

    def test_expired_session_renews_read_but_never_posts_batch_again(self):
        self.client.get_batch_progress.side_effect = [UpiGit5Error('expired', status=401), self.snapshot([self.job(1), self.job(2)])]
        self.enqueue()
        self.run_worker()
        self.assertEqual(self.client.create_session.call_count, 2)
        self.client.create_batch.assert_called_once()
        self.assertEqual(db.get_account(2)['extract_link_status'], 'success')

    def test_missing_job_in_finished_batch_remains_unknown(self):
        self.client.get_batch_progress.return_value = self.snapshot([self.job(1)])
        self.enqueue()
        self.run_worker()
        self.assertEqual(db.get_account(1)['extract_link_status'], 'success')
        self.assertEqual(db.get_account(2)['extract_link_status'], 'unknown')
        self.assertEqual(db.get_account(2)['extract_link_job_id'], 'job-2')
        self.assert_slot_released()

    def test_finished_job_preserved_when_later_poll_fails(self):
        self.client.get_batch_progress.side_effect = [self.snapshot([self.job(1), self.job(2, 'running')], True),
                                                     UpiGit5Error('forbidden', status=403)]
        self.enqueue()
        self.run_worker()
        self.assertEqual(db.get_account(1)['extract_link_status'], 'success')
        self.assertEqual(db.get_account(2)['extract_link_status'], 'unknown')

    def test_partial_progress_fetches_full_result_before_success(self):
        self.client.get_batch_progress.return_value = self.snapshot([self.job(1, result=False), self.job(2, result=False)])
        self.enqueue()
        self.run_worker()
        self.client.get_batch.assert_called_once_with('private-session', 'batch-A')
        self.assertEqual(db.get_account(1)['extract_link_long_url'], 'https://payments.example.test/1')

    def test_malformed_accepted_response_preserves_batch_for_recovery(self):
        error = UpiGit5Error('accepted malformed', uncertain=True)
        error.batch_id = 'batch-A'
        self.client.create_batch.side_effect = error
        self.enqueue()
        self.run_worker()
        row = db.get_account(1)
        self.assertEqual(row['extract_link_status'], 'unknown')
        self.assertEqual(row['extract_link_task_id'], 'batch-A')
        self.assertNotIn('extract_link_job_id', row)
        self.client.create_batch.assert_called_once()

    def test_uncertain_submit_blocks_duplicate_after_restart_and_stale_timestamp(self):
        self.client.create_batch.side_effect = UpiGit5Error('transport timeout', uncertain=True)
        self.enqueue()
        self.run_worker()
        row = db.get_account(1)
        row['extract_link_started_at'] = '2000-01-01T00:00:00'
        self.save(row)
        db.recover_interrupted_extract_links()
        result = self.enqueue()
        self.assertEqual(result['busy_count'], 2)
        self.assertEqual(result['started_count'], 0)
        self.assert_slot_released()

    def test_busy_accounts_skip_without_changing_other_claims(self):
        row = db.get_account(1)
        row.update(extract_link_status='unknown', extract_link_provider_type='upi_git5')
        self.save(row)
        result = self.enqueue()
        self.assertEqual(result['busy_count'], 1)
        self.assertEqual(result['started_count'], 1)
        self.assertEqual(self.executor.submit.call_args.kwargs['entries'][0]['account_id'], 2)
        self.assertEqual(db.get_account(1)['extract_link_status'], 'unknown')

    def test_invalid_proxies_and_cdk_fail_before_claim_or_queue_acquisition(self):
        for kwargs in ({'entry_proxies': []}, {'entry_proxies': ['']}, {'entry_proxies': ['not a URL']}, {'cdk_id': 999}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((ValueError, LookupError)):
                    self.enqueue(**kwargs)
                self.assertNotIn('extract_link_status', db.get_account(1))
                self.assert_slot_released()

    def test_executor_failure_releases_slots_and_finishes_local_claims(self):
        self.executor.submit.side_effect = RuntimeError('executor unavailable')
        with self.assertRaises(RuntimeError):
            self.enqueue()
        self.assertEqual(db.get_account(1)['extract_link_status'], 'failed')
        self.assert_slot_released()

    def test_queue_full_reports_each_unsubmitted_account(self):
        self.slots.acquire()
        result = self.enqueue()
        self.assertEqual(result['failed_count'], 2)
        self.assertEqual(result['started_count'], 0)
        self.assertNotIn('extract_link_status', db.get_account(1))
        self.slots.release()

    def test_state_uses_allowlist_and_redacts_text(self):
        job = self.job(1)
        job.update(text=f'{CDK} {PROXY} private-session private-account-token-1')
        job['result'].update(token=CDK, private='secret', copy_paste=CDK)
        self.client.get_batch_progress.return_value = self.snapshot([job, self.job(2)])
        self.enqueue()
        self.run_worker()
        saved = db.get_account(1)['extract_link_result_json']
        for value in (CDK, PROXY, 'private-session', 'private-account-token-1'):
            self.assertNotIn(value, saved)

    def test_preserve_terminal_and_expected_batch_guards_are_atomic(self):
        self.enqueue()
        self.run_worker()
        self.assertFalse(db.update_account_extract(1, {'status': 'unknown'}, preserve_terminal=True))
        self.assertFalse(db.update_account_extract(1, {'status': 'failed'}, expected_task_id='other-batch'))
        self.assertEqual(db.get_account(1)['extract_link_status'], 'success')

    def test_new_claim_clears_old_artifacts(self):
        row = db.get_account(1)
        row.update(extract_link_status='failed', extract_link_provider_type='upi_git5',
                   extract_link_job_id='old-job', extract_link_long_url='https://old.example.test', extract_link_payment_status='completed')
        self.save(row)
        self.enqueue()
        row = db.get_account(1)
        self.assertNotIn('extract_link_job_id', row)
        self.assertNotIn('extract_link_long_url', row)
        self.assertNotIn('extract_link_payment_status', row)

    def test_provider_list_and_active_configuration_guard(self):
        self.assertTrue(any(p['provider_type'] == 'upi_git5' for p in service.list_providers()))
        self.enqueue()
        for mutation in (lambda: store.delete_provider(self.provider['id']),
                         lambda: store.save_provider({'api_base': 'https://elsewhere.example.test'}, self.provider['id']),
                         lambda: store.delete_cdk(self.cdk['id']),
                         lambda: store.save_cdk(self.provider['id'], {'cdk': 'different'}, self.cdk['id'])):
            with self.assertRaises(RuntimeError):
                mutation()

    def test_task_actions_use_job_id_saved_cdk_and_binary_qr(self):
        self.enqueue()
        self.run_worker()
        self.client.checkout_progress.return_value = self.job(1, 'cancelled')
        service.task_action(1, 'cancel')
        self.client.cancel_checkout.assert_called_once_with('private-session', 'job-1')
        self.assertEqual(db.get_account(1)['extract_link_status'], 'stopped')
        self.client.checkout_qr.return_value = b'\x89PNG\r\n\x1a\nexample'
        qr = service.task_action(1, 'qr')
        self.assertTrue(qr['image_url_png'].startswith('data:image/png;base64,'))
        self.assertEqual(db.get_account(1)['extract_link_status'], 'stopped')

    def test_one_time_cdk_can_be_reentered_for_refresh(self):
        self.enqueue(cdk_id=None, cdk=CDK)
        self.run_worker()
        self.client.checkout_progress.return_value = self.job(1)
        state = service.task_action(1, 'refresh', cdk=CDK)
        self.assertEqual(state['status'], 'success')
        self.assertNotIn('extract_link_cdk_id', db.get_account(1))
        self.client.checkout_progress.assert_called_once_with('private-session', 'job-1')

    def test_foreign_cdk_rejected_before_upstream_action(self):
        self.enqueue()
        self.run_worker()
        foreign = store.save_provider({'name': 'Other', 'provider_type': 'upi_git5', 'api_base': 'https://other.example.test'})
        other_cdk = store.save_cdk(foreign['id'], {'cdk': 'OTHER'})
        row = db.get_account(1)
        row['extract_link_cdk_id'] = other_cdk['id']
        self.save(row)
        with self.assertRaises(LookupError):
            service.task_action(1, 'cancel')
        self.client.cancel_checkout.assert_not_called()

    def test_task_routes_accept_one_time_cdk_and_refresh_failed_job(self):
        app = Flask(__name__)
        register_extract_routes(app)
        with patch.object(service, 'task_action', return_value={'status': 'failed', 'ok': False}) as action:
            response = app.test_client().post('/api/accounts/1/extract-link/refresh', json={'cdk': CDK})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['ok'])
        self.assertEqual(response.json['status'], 'failed')
        self.assertIn('no-store', response.headers['Cache-Control'])
        action.assert_called_once_with(1, 'refresh', blik_code=None, cdk=CDK)

    def test_account_ui_projection_includes_recovery_metadata_without_secrets(self):
        from webui.app import _compact_account_for_list
        self.enqueue()
        self.run_worker()
        row = _compact_account_for_list(db.get_account(1))
        self.assertEqual(row['extract_link_provider_type'], 'upi_git5')
        self.assertEqual(row['extract_link_job_id'], 'job-1')
        self.assertEqual(row['extract_link_cdk_id'], self.cdk['id'])
        self.assertNotIn('access_token', row)

    def test_bind_jobs_rejects_ambiguous_and_conflicting_mapping(self):
        for jobs in ([{'job_id': 'a'}, {'job_id': 'b'}],
                     [{'index': 0, 'job_id': 'a'}, {'index': 0, 'job_id': 'b'}],
                     [{'index': 0, 'job_id': 'a', 'account_email': 'a2@example.test'}, {'index': 1, 'job_id': 'b'}]):
            with self.assertRaises(UpiGit5Error):
                bind_jobs(self.accounts, jobs)
        bound = bind_jobs(self.accounts, [{'index': 2, 'job_id': 'b'}, {'index': 1, 'job_id': 'a'}])
        self.assertEqual(bound['a']['account_id'], 1)

    def test_http_bulk_filters_accounts_and_schedules_one_upstream_batch(self):
        from webui.app import create_app
        app = create_app(auth_code='test-auth')
        client = app.test_client()
        row = db.get_account(2)
        row['plus_trial_eligible'] = False
        self.save(row)
        response = client.post('/api/accounts/extract-link-bulk', headers={'X-Auth-Code': 'test-auth'}, json={
            'account_ids': [1, 1, 2, 999], 'provider_id': self.provider['id'], 'cdk_id': self.cdk['id'],
            'entry_proxies': [PROXY], 'use_promo': False, 'promo_campaign': 'test-promo', 'payment_provider_id': 'xxsyun'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json['started_count'], 1)
        self.assertEqual(response.json['skipped_count'], 2)
        self.executor.submit.assert_called_once()
        kwargs = self.executor.submit.call_args.kwargs
        self.assertEqual(kwargs['entry_proxies'], [PROXY])
        self.assertFalse(kwargs['use_promo'])
        self.assertEqual(kwargs['promo_campaign'], 'test-promo')
        self.assertEqual(kwargs['payment_provider_id'], 'xxsyun')
        self.assertNotIn(CDK, response.get_data(as_text=True))
        self.assertNotIn(PROXY, response.get_data(as_text=True))

    def test_http_queue_full_is_not_an_empty_success(self):
        from webui.app import create_app
        client = create_app(auth_code='test-auth').test_client()
        self.slots.acquire()
        for path, ids in (('extract-link', {'account_id': 1}), ('extract-link-bulk', {'account_ids': [1, 2]})):
            response = client.post('/api/accounts/' + path, headers={'X-Auth-Code': 'test-auth'}, json={
                **ids, 'provider_id': self.provider['id'], 'cdk_id': self.cdk['id'], 'entry_proxies': [PROXY]})
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json['ok'])
        self.slots.release()
        self.executor.submit.assert_not_called()

    def test_http_qr_requires_authentication(self):
        from webui.app import create_app
        client = create_app(auth_code='test-auth').test_client()
        response = client.post('/api/accounts/1/extract-link/qr', json={'cdk': CDK})
        self.assertEqual(response.status_code, 401)
        self.client.create_session.assert_not_called()

    def test_lumen_bulk_preserves_proxy_and_payment_amount(self):
        provider = store.save_provider({'name': 'Lumen regression', 'provider_type': 'lumen', 'api_base': 'https://lumen.example.test'})
        with patch.object(service, 'enqueue_account_extract', return_value={'accepted': True, 'future': object()}) as enqueue:
            result = service.enqueue_account_extract_bulk(accounts=[{**self.accounts[0], 'proxy_url': PROXY}],
                provider_id=provider['id'], cdk='other-test-cdk', payment_amount=123)
        self.assertEqual(result['started_count'], 1)
        self.assertEqual(enqueue.call_args.kwargs['payment_amount'], 123)
        self.assertEqual(enqueue.call_args.kwargs['proxy_url'], PROXY)
        self.assertNotIn('future', result['started'][0])

    def test_rate_limit_retries_only_read(self):
        self.client.get_batch_progress.side_effect = [UpiGit5Error('rate limited', status=429), self.snapshot([self.job(1), self.job(2)])]
        self.enqueue()
        self.run_worker()
        self.client.create_batch.assert_called_once()
        self.assertEqual(self.client.get_batch_progress.call_count, 2)
        self.assertEqual(db.get_account(1)['extract_link_status'], 'success')

    def test_refresh_recovers_missing_job_id_from_unique_batch_email(self):
        self.client.create_batch.return_value['jobs'] = []
        self.enqueue()
        self.run_worker()
        job = self.job(1)
        job['account_email'] = self.accounts[0]['email']
        self.client.get_batch.return_value = self.snapshot([job])
        self.client.checkout_progress.return_value = job
        service.task_action(1, 'refresh')
        self.assertEqual(db.get_account(1)['extract_link_job_id'], 'job-1')
        self.assertEqual(db.get_account(1)['extract_link_status'], 'success')

    def test_saved_cdk_status_redacts_echoed_codes(self):
        self.client.cdk_status.return_value = {'link_cdk': {'code': CDK, 'remaining_uses': 10}, 'message': CDK}
        result = service.query_cdk(provider_id=self.provider['id'], cdk_id=self.cdk['id'])
        self.assertNotIn(CDK, json.dumps(result))
        self.assertEqual(result['link_cdk']['remaining_uses'], 10)

    def test_http_transport_to_batch_worker_and_saved_result_end_to_end(self):
        import requests
        from core.upi_git5_client import UpiGit5Client
        from webui.app import create_app
        def reply(payload):
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response
        session = {'ok': True, 'token': 'private-session', 'expires_at': 999, 'link_cdk': {}}
        accepted = self.client.create_batch.return_value
        snap = self.snapshot([self.job(2), self.job(1)])
        with patch.object(service, '_upi_git5_client', return_value=UpiGit5Client(self.provider['api_base'])), \
             patch('requests.sessions.Session.request', side_effect=[reply(session), reply(accepted), reply(snap), reply({'ok': True})]) as http:
            client = create_app(auth_code='test-auth').test_client()
            response = client.post('/api/accounts/extract-link-bulk', headers={'X-Auth-Code': 'test-auth'}, json={
                'account_ids': [1, 2], 'provider_id': self.provider['id'], 'cdk_id': self.cdk['id'], 'entry_proxies': [PROXY]})
            self.assertEqual(response.status_code, 202)
            self.run_worker()
            self.assertEqual(db.get_account(1)['extract_link_status'], 'success')
            self.assertEqual(db.get_account(2)['extract_link_long_url'], 'https://payments.example.test/2')
            self.assertEqual([call.args[0] for call in http.call_args_list], ['POST', 'POST', 'GET', 'POST'])
            submits = [call for call in http.call_args_list if call.args[1].endswith('/api/upi-git5/batch')]
            self.assertEqual(len(submits), 1)
            self.assertEqual(submits[0].kwargs['json']['entry_proxies'], [PROXY])
        self.assert_slot_released()
