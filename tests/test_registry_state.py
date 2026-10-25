import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('registry_state', ROOT / 'scripts/registry_state.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class RegistryStateTests(unittest.TestCase):
    def setUp(self):
        self.server, self.repo, self.name, self.tag = gate.read_manifest(ROOT, {})
        self.response = {'server': copy.deepcopy(self.server),
                         '_meta': {gate.OFFICIAL: {'status': 'active'}}}
        self.digest = self.server['packages'][0]['fileSha256']

    def test_exact_manifest_and_asset_skip_publication(self):
        self.assertEqual(gate.evaluate(self.server, self.response, lambda: self.digest),
                         'already-published-verified')

    def test_clobbered_release_asset_fails_before_publication(self):
        with self.assertRaisesRegex(gate.PublicationConflict, 'digest conflict'):
            gate.evaluate(self.server, self.response, lambda: '0' * 64)

    def test_manifest_hash_mismatch_is_not_already_published(self):
        self.response['server']['packages'][0]['fileSha256'] = '0' * 64
        with self.assertRaisesRegex(gate.PublicationConflict, 'manifest differs'):
            gate.evaluate(self.server, self.response, lambda: self.digest)

    def test_other_metadata_mismatch_is_not_already_published(self):
        self.response['server']['description'] = 'changed description'
        with self.assertRaisesRegex(gate.PublicationConflict, 'manifest differs'):
            gate.evaluate(self.server, self.response, lambda: self.digest)

    def test_deleted_and_deprecated_versions_block(self):
        for status in ['deleted', 'deprecated', None]:
            with self.subTest(status=status):
                self.response['_meta'][gate.OFFICIAL]['status'] = status
                with self.assertRaises(gate.PublicationConflict):
                    gate.evaluate(self.server, self.response, lambda: self.digest)

    def test_confirmed_absence_is_the_only_new_publish_state(self):
        self.assertEqual(gate.evaluate(self.server, None, lambda: self.fail()), 'unpublished')

    def test_http_404_means_absence(self):
        with patch.object(gate, 'urlopen', side_effect=HTTPError('url', 404, 'missing', {}, None)):
            self.assertIsNone(gate.read_registry(self.server))

    def test_rate_limit_and_server_errors_do_not_mean_absence(self):
        for status in [401, 403, 429, 500]:
            with self.subTest(status=status), patch.object(gate, 'urlopen', side_effect=HTTPError('url', status, 'blocked', {}, None)):
                with self.assertRaises(gate.PublicationConflict):
                    gate.read_registry(self.server)

    def test_successful_http_with_invalid_json_body_does_not_mean_absence(self):
        for payload in ['null', '[]', '"unknown"']:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = payload
            with self.subTest(payload=payload), patch.object(gate, 'urlopen', return_value=response):
                with self.assertRaises(gate.PublicationConflict):
                    gate.read_registry(self.server)

    def test_malformed_registry_metadata_fails_closed(self):
        for metadata in [None, [], {gate.OFFICIAL: None}]:
            self.response['_meta'] = metadata
            with self.subTest(metadata=metadata), self.assertRaises(gate.PublicationConflict):
                gate.evaluate(self.server, self.response, lambda: self.digest)

    def test_network_failure_does_not_mean_absence(self):
        with patch.object(gate, 'urlopen', side_effect=URLError('offline')):
            with self.assertRaises(gate.PublicationConflict):
                gate.read_registry(self.server)

    def test_tag_and_repository_identity_are_checked(self):
        for env in [{'GITHUB_REF_TYPE': 'tag', 'GITHUB_REF_NAME': 'v9.9.9'},
                    {'GITHUB_REPOSITORY': 'someone/else'}]:
            with self.subTest(env=env), self.assertRaises(gate.PublicationConflict):
                gate.read_manifest(ROOT, env)

    def test_versions_must_match_before_any_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ['server.json', 'package.json', 'manifest.json']:
                (path / name).write_text((ROOT / name).read_text())
            package = json.loads((path / 'package.json').read_text())
            package['version'] = '9.9.9'
            (path / 'package.json').write_text(json.dumps(package))
            with self.assertRaises(gate.PublicationConflict):
                gate.read_manifest(path, {})

    def test_unpublished_release_requires_exact_existing_tag(self):
        ok = subprocess.CompletedProcess([], 0, stdout='same\n')
        with patch.object(gate.subprocess, 'run', return_value=ok):
            gate.verify_release_source(ROOT, self.tag)
        different = subprocess.CompletedProcess([], 0, stdout='different\n')
        with patch.object(gate.subprocess, 'run', side_effect=[ok, different]):
            with self.assertRaises(gate.PublicationConflict):
                gate.verify_release_source(ROOT, self.tag)

    def test_missing_release_asset_fails_closed(self):
        failed = subprocess.CompletedProcess([], 1, stdout='', stderr='missing')
        with patch.object(gate.subprocess, 'run', return_value=failed):
            with self.assertRaises(gate.PublicationConflict):
                gate.release_digest(self.repo, self.name, self.tag)


if __name__ == '__main__':
    unittest.main()
