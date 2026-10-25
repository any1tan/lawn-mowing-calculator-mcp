#!/usr/bin/env python3
"""Read-only publication gate. Never upload, overwrite, tag or publish here."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

REGISTRY = 'https://registry.modelcontextprotocol.io'
OFFICIAL = 'io.modelcontextprotocol.registry/official'


class PublicationConflict(RuntimeError):
    pass


def read_manifest(root, environ):
    server = json.loads((root / 'server.json').read_text())
    package = json.loads((root / 'package.json').read_text())
    bundle = json.loads((root / 'manifest.json').read_text())
    version = server.get('version', '')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?', version):
        raise PublicationConflict('Invalid release version; no action was taken.')
    if package.get('version') != version or bundle.get('version') != version:
        raise PublicationConflict('server.json, package.json and manifest.json versions differ.')
    if package.get('mcpName') != server.get('name'):
        raise PublicationConflict('package.json mcpName differs from server.json name.')
    repository = server.get('repository', {}).get('url', '')
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)', repository)
    if not match:
        raise PublicationConflict('Expected an exact GitHub repository URL.')
    repo = match.group(1)
    if environ.get('GITHUB_REPOSITORY', repo) != repo:
        raise PublicationConflict('Workflow repository differs from the publication manifest.')
    name = repo.split('/')[1]
    if package.get('name') != name or bundle.get('name') != name:
        raise PublicationConflict('Repository, package and bundle names differ.')
    tag = 'v' + version
    if environ.get('GITHUB_REF_TYPE') == 'tag' and environ.get('GITHUB_REF_NAME') != tag:
        raise PublicationConflict('Triggered tag differs from all publication manifests.')
    packages = server.get('packages', [])
    identifier = f'https://github.com/{repo}/releases/download/{tag}/{name}.mcpb'
    if len(packages) != 1 or packages[0].get('registryType') != 'mcpb':
        raise PublicationConflict('This gate supports exactly one MCPB package.')
    if packages[0].get('identifier') != identifier or packages[0].get('version') != version:
        raise PublicationConflict('MCPB URL/version does not match the exact release.')
    if not re.fullmatch(r'[a-f0-9]{64}', packages[0].get('fileSha256', '')):
        raise PublicationConflict('Expected a lowercase SHA-256 digest in server.json.')
    return server, repo, name, tag


def registry_url(server):
    return (f'{REGISTRY}/v0.1/servers/{quote(server["name"], safe="")}'
            f'/versions/{quote(server["version"], safe="")}?include_deleted=true')


def read_registry(server):
    request = Request(registry_url(server), headers={'Accept': 'application/json'})
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
            if not isinstance(data, dict):
                raise PublicationConflict('Registry returned an invalid response; refusing to publish.')
            return data
    except HTTPError as error:
        if error.code == 404:
            return None
        raise PublicationConflict(f'Registry read returned HTTP {error.code}; refusing to publish.') from error
    except (URLError, TimeoutError, ValueError) as error:
        raise PublicationConflict('Registry read failed; absence has not been established.') from error


def release_digest(repo, name, tag):
    # gh only performs an authenticated read of this repository's exact asset.
    with tempfile.TemporaryDirectory(prefix='mcp-release-check-') as temp:
        result = subprocess.run(
            ['gh', 'release', 'download', tag, '--repo', repo,
             '--pattern', name + '.mcpb', '--dir', temp],
            capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise PublicationConflict('Cannot read the existing release asset; refusing to publish.')
        path = Path(temp) / (name + '.mcpb')
        if not path.is_file():
            raise PublicationConflict('Expected release asset is missing.')
        digest = hashlib.sha256()
        with path.open('rb') as asset:
            for chunk in iter(lambda: asset.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()


def evaluate(server, response, get_asset_digest):
    if response is None:
        return 'unpublished'
    metadata = response.get('_meta')
    official = metadata.get(OFFICIAL) if isinstance(metadata, dict) else None
    status = official.get('status') if isinstance(official, dict) else None
    if status != 'active':
        raise PublicationConflict('Existing registry version is not active; manual review required.')
    registered = response.get('server')
    if registered != server:
        raise PublicationConflict('Existing registry manifest differs from server.json; do not republish this version.')
    expected = registered['packages'][0]['fileSha256']
    actual = get_asset_digest()
    if actual != expected:
        raise PublicationConflict(
            f'Existing release asset digest conflict: registry={expected}, asset={actual}. '
            'Restore the hash-matching original or review a new release; never overwrite or ignore this conflict.')
    return 'already-published-verified'


def verify_release_source(root, tag):
    def rev(ref):
        result = subprocess.run(['git', 'rev-parse', '--verify', ref], cwd=root,
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise PublicationConflict('The release tag must already exist and resolve to this checkout.')
        return result.stdout.strip()
    if rev('HEAD') != rev('refs/tags/' + tag + '^{commit}'):
        raise PublicationConflict('Unpublished version must be built from its exact existing tag commit.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-published', action='store_true')
    args = parser.parse_args()
    root = Path.cwd()
    server, repo, name, tag = read_manifest(root, os.environ)
    response = read_registry(server)
    state = evaluate(server, response, lambda: release_digest(repo, name, tag))
    if state == 'unpublished':
        if args.require_published:
            raise PublicationConflict('Published version was not found during final readback.')
        verify_release_source(root, tag)
    result = {'state': state, 'publish_needed': state == 'unpublished',
              'version': server['version'], 'tag': tag, 'registry_url': registry_url(server)}
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'publish_needed={str(result["publish_needed"]).lower()}\n')
            output.write(f'version={server["version"]}\ntag={tag}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (PublicationConflict, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print('::error::' + str(error), file=sys.stderr)
        sys.exit(1)
