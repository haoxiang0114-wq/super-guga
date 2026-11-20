"""Discover and install the companion's declarative skill and workflow packages."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

import requests

from core.guga_paths import BASE


PROJECT = Path(__file__).resolve().parents[1]
REPOSITORY = 'jscjscjscjscjsc/super-guga'
PACKAGE_DIR = 'plugins/community'
API = 'https://api.github.com/repos/' + REPOSITORY
ID = re.compile(r'[a-z][a-z0-9-]{1,62}')
VERSION = re.compile(r'(?:0|[1-9]\d{0,4})(?:\.(?:0|[1-9]\d{0,4})){2}')
COMMIT = re.compile(r'[a-f0-9]{40}')
FEATURES = frozenset(('hearthstone', 'battleground-advisor', 'desktop-ocr', 'companion-memory', 'executor-settings'))
MAX_PACKAGE = 48 * 1024
MAX_PACKAGES = 40


def read_manifest(raw, expected_id):
    if not ID.fullmatch(expected_id) or len(raw) > MAX_PACKAGE:
        raise ValueError('插件标识或大小无效')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('schema') != 1 or data.get('id') != expected_id:
        raise ValueError('插件文件必须使用 schema 1，文件名须与 id 一致')
    kind = data.get('kind')
    version = data.get('version')
    if kind not in ('workflow', 'codex-skill', 'feature') or not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError('插件类型或版本无效')
    normalized = dict(schema=1, id=expected_id, kind=kind, version=version)
    for key, max_length in (('name', 50), ('description', 280)):
        text = data.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > max_length:
            raise ValueError('插件名称或说明无效')
        normalized[key] = text.strip()
    if kind == 'feature':
        if data.get('entry') not in FEATURES:
            raise ValueError('插件引用了未知的内置入口')
        normalized['entry'] = data['entry']
    else:
        body = data.get('body')
        if not isinstance(body, str) or not body.strip() or len(body) > 7200:
            raise ValueError('技能或工作流需要 1—7200 字的正文')
        normalized['body'] = body.strip()
    return normalized


def _version(package):
    return tuple(map(int, package['version'].split('.')))


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _request(url, limit, unavailable=False):
    with requests.get(url, timeout=(4, 5), stream=True, allow_redirects=False,
                      headers={'User-Agent': 'GugaPluginMarket', 'Accept': 'application/vnd.github+json'}) as response:
        if unavailable and response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError('插件仓库返回 HTTP ' + str(response.status_code))
        data = bytearray()
        for block in response.iter_content(8192):
            data.extend(block)
            if len(data) > limit:
                raise ValueError('仓库响应超过允许大小')
        return bytes(data)


class PluginMarket:
    def __init__(self, root=None, bundled=None):
        self.root = Path(root or BASE / 'plugins')
        self.bundled = Path(bundled or PROJECT / PACKAGE_DIR)
        self.installed_path = self.root / 'installed'
        self.cache = self.root / 'catalog-v1.json'
        self.lock = threading.RLock()

    def _packages_at(self, folder):
        packages = {}
        for path in sorted(folder.glob('*.json'))[:MAX_PACKAGES]:
            try:
                if not path.is_symlink() and path.stat().st_size <= MAX_PACKAGE:
                    packages[path.stem] = read_manifest(path.read_bytes(), path.stem)
            except (OSError, ValueError):
                continue
        return packages

    def installed(self):
        return self._packages_at(self.installed_path)

    def available(self):
        packages = self._packages_at(self.bundled)
        try:
            if self.cache.stat().st_size > MAX_PACKAGES * MAX_PACKAGE:
                return packages
            data = json.loads(self.cache.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or not COMMIT.fullmatch(str(data.get('commit'))):
                return packages
            for row in data['packages'][:MAX_PACKAGES]:
                remote = read_manifest(json.dumps(row, ensure_ascii=False).encode(), row['id'])
                old = packages.get(remote['id'])
                if old is None or _version(remote) > _version(old):
                    packages[remote['id']] = remote
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        return packages

    def refresh(self):
        started = time.monotonic()
        head = json.loads(_request(API + '/git/ref/heads/main', 16 * 1024))
        ref = str((head.get('object') or {}).get('sha') or '')
        if not COMMIT.fullmatch(ref):
            raise ValueError('仓库提交号无效')
        listing = _request(f'{API}/contents/{PACKAGE_DIR}?ref={ref}', 128 * 1024, unavailable=True)
        entries = json.loads(listing) if listing else []
        if not isinstance(entries, list):
            raise ValueError('仓库没有返回插件目录')
        packages = []
        invalid = 0
        for row in entries[:MAX_PACKAGES]:
            name = str(row.get('name', ''))
            if (row.get('type') != 'file' or not name.endswith('.json') or not ID.fullmatch(name[:-5])):
                continue
            if time.monotonic() - started > 24:
                raise TimeoutError('刷新插件目录超时，已保留上一次目录')
            try:
                uri = f'https://raw.githubusercontent.com/{REPOSITORY}/{ref}/{PACKAGE_DIR}/{name}'
                raw = _request(uri, MAX_PACKAGE)
                packages.append(read_manifest(raw, name[:-5]))
            except ValueError:
                invalid += 1
        with self.lock:
            _atomic_json(self.cache, dict(commit=ref, packages=packages))
        return dict(count=len(packages), invalid=invalid, revision=ref[:8], published=bool(listing))

    def install(self, ident):
        with self.lock:
            package = self.available().get(ident)
            if not package:
                raise ValueError('未找到该插件，请先刷新市场')
            current = self.installed().get(ident)
            if current and _version(current) >= _version(package):
                return current
            _atomic_json(self.installed_path / (ident + '.json'), package)
            return package
