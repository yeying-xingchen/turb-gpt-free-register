import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class UiLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        paths = {
            name: root / name for name in (
                '_ACCOUNTS_JSON', '_OUTLOOK_JSON', '_GENERIC_API_EMAIL_JSON', '_DOMAIN_EMAIL_JSON',
                '_JOBS_JSON', '_LEGACY_ACCOUNTS_JSON', '_LEGACY_OUTLOOK_JSON', '_LEGACY_JOBS_JSON',
                '_LEGACY_SQLITE', '_CODEX_DIR', '_CODEX_AGENT_DIR', '_LEGACY_CODEX_EXPORT_STATE',
            )
        }
        storage = patch.multiple(db, **paths, _DATA_DIR=root, _LOG_DIR=root / 'logs',
                                 _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        storage.start()
        self.addCleanup(storage.stop)

    def test_point_lookups_preserve_decoration_and_email_matching(self):
        db._save_collection('accounts', [
            {'id': 1, 'email': 'User@example.com', 'note': 'first'},
            {'id': 2, 'email': 'user@example.com', 'note': 'second'},
            {'id': 3, 'email': 'Üser@example.com', 'archived': True},
            {'id': 4, 'email': '', 'plan_check_status': 'running'},
        ])
        with patch.object(db, '_load_accounts', side_effect=AssertionError('full account load')):
            self.assertEqual(db.get_account('2')['note'], 'second')
            self.assertIsNone(db.get_account(999))
            self.assertEqual(db.get_account_by_email('USER@example.com')['id'], 1)
            self.assertEqual(db.get_account_by_email('üser@example.com')['id'], 3)
            self.assertIsNone(db.get_account_by_email(' User@example.com'))
            self.assertIsNone(db.get_account_by_email('absent@example.com'))
            self.assertEqual(db.get_account_by_email(None)['id'], 4)
            self.assertTrue(db.get_account(4)['plan_check_stale'])
            self.assertIn('copy_line', db.get_account(1))

    def test_retry_chain_uses_latest_success_and_excludes_self(self):
        db._save_collection('jobs', [
            {'id': 1, 'status': 'failed'},
            {'id': 2, 'root_job_id': 1, 'status': 'success'},
            {'id': 3, 'root_job_id': '1', 'status': 'success'},
            {'id': 4, 'root_job_id': 1, 'status': 'failed'},
            {'id': 5, 'root_job_id': 99, 'status': 'success'},
            {'id': 6, 'root_job_id': 6, 'status': 'success'},
            {'id': 7, 'status': 'success'},
        ])
        with patch.object(db, '_load_jobs', side_effect=AssertionError('full job load')):
            self.assertEqual(db.get_job('1')['status'], 'failed')
            self.assertIsNone(db.get_job(999))
            self.assertEqual(db.get_successful_retry_for_job(1)['id'], 3)
            self.assertEqual(db.get_successful_retry_for_job(4)['id'], 3)
            self.assertEqual(db.get_successful_retry_for_job(3)['id'], 2)
            self.assertIsNone(db.get_successful_retry_for_job(6))
            self.assertIsNone(db.get_successful_retry_for_job(7))
            self.assertIsNone(db.get_successful_retry_for_job(999))

    def test_job_indexes_are_added_to_an_existing_database(self):
        db._save_collection('jobs', [{'id': 1, 'status': 'failed'}])
        with db._sqlite_conn() as conn:
            conn.execute('DROP INDEX idx_registration_jobs_id')
            conn.execute('DROP INDEX idx_registration_jobs_successful_retry')
        db._SQLITE_READY = False
        self.assertEqual(db.get_job(1)['id'], 1)
        with db._sqlite_conn() as conn:
            indexes = {row['name'] for row in conn.execute('PRAGMA index_list(registration_jobs)')}
        self.assertIn('idx_registration_jobs_id', indexes)
        self.assertIn('idx_registration_jobs_successful_retry', indexes)


if __name__ == '__main__':
    unittest.main()
