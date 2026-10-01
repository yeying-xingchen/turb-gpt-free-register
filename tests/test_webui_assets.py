import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, Response

from webui.assets import init_ui_assets


class UiAssetTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        init_ui_assets(self.app)
        self.app.add_url_rule('/page', endpoint='page', view_func=lambda: Response('hello ' * 1000, mimetype='text/html'))
        self.app.add_url_rule('/json', endpoint='json', view_func=lambda: self.app.json.response({'data': 'x' * 2000}))
        self.client = self.app.test_client()

    def asset_url(self):
        with self.app.test_request_context():
            return self.app.jinja_env.globals['ui_asset_url']('console.js')

    def test_versioned_assets_compress_and_revalidate(self):
        url = self.asset_url()
        plain = self.client.get(url)
        compressed = self.client.get(url, headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(gzip.decompress(compressed.data), plain.data)
        self.assertLess(len(compressed.data), len(plain.data) // 3)
        self.assertIn('immutable', compressed.headers['Cache-Control'])
        self.assertIn('Accept-Encoding', compressed.vary)
        self.assertNotEqual(plain.get_etag(), compressed.get_etag())
        cached = self.client.get(url, headers={
            'Accept-Encoding': 'gzip', 'If-None-Match': compressed.headers['ETag'],
        })
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.data, b'')
        head = self.client.head(url, headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(head.data, b'')
        self.assertEqual(head.content_length, compressed.content_length)

    def test_encoding_negotiation_and_vary(self):
        for url in [self.asset_url(), '/page', '/json']:
            plain = self.client.get(url)
            for encoding in ['identity', 'gzip;q=0, identity;q=1', 'br', '*;q=0']:
                with self.subTest(url=url, encoding=encoding):
                    response = self.client.get(url, headers={'Accept-Encoding': encoding})
                    self.assertNotIn('Content-Encoding', response.headers)
                    self.assertEqual(response.data, plain.data)
                    self.assertIn('Accept-Encoding', response.vary)
            compressed = self.client.get(url, headers={'Accept-Encoding': 'gzip'})
            self.assertEqual(gzip.decompress(compressed.data), plain.data)

    def test_only_current_version_can_be_cached_immutably(self):
        for url in ['/assets/console.js', '/assets/console.js?v=obsolete']:
            response = self.client.get(url)
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
        self.assertEqual(self.client.get('/assets/auth.py').status_code, 404)

    def test_asset_edit_invalidates_url_and_compressed_cache(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'console.js'
            path.write_text('first ' * 1000)
            with patch('webui.assets._ASSET_DIR', Path(td)):
                first_url = self.asset_url()
                first = self.client.get(first_url, headers={'Accept-Encoding': 'gzip'})
                path.write_text('second version ' * 1000)
                next_url = self.asset_url()
                self.assertNotEqual(first_url, next_url)
                response = self.client.get(next_url, headers={'Accept-Encoding': 'gzip'})
                self.assertEqual(gzip.decompress(response.data), path.read_bytes())
                self.assertNotEqual(first.get_etag(), response.get_etag())

    def test_modern_template_references_deferred_versioned_assets(self):
        self.app.jinja_loader.searchpath.append(str(Path(__file__).resolve().parents[1] / 'webui/templates'))
        with self.app.test_request_context():
            html = self.app.jinja_env.get_template('index.html').render()
        self.assertRegex(html, r'<script defer src="/assets/console.js\?v=[a-f0-9]+">')
        self.assertRegex(html, r'<link rel="stylesheet" href="/assets/console.css\?v=[a-f0-9]+">')
        self.assertNotIn('async function loadAccounts()', html)
        self.assertIn('accountsBodyV2', html)


if __name__ == '__main__':
    unittest.main()
