# -*- coding: utf-8 -*-
"""从上游仓库更新本项目，保留本地改动。

用法：
    .venv\\Scripts\\python.exe tools\\update_from_upstream.py            # 检查并更新
    .venv\\Scripts\\python.exe tools\\update_from_upstream.py --check    # 只看有没有新版，不动文件
    .venv\\Scripts\\python.exe tools\\update_from_upstream.py --repo a/b  # 换上游（默认咕嘎官方仓库）

原理：`.upstream.json` 记录「上次同步到的上游提交 + 每个文件的上游 blob sha」。
- 上游新增          → 直接落盘
- 上游改、本地没改  → 覆盖
- 上游改、本地也改  → git merge-file 三方合并，冲突处写标记后原地保留
- 本地独有的文件    → 一律不动
所有被覆盖/合并的文件先备份到 `.upstream_backup/<时间戳>/`。
只用标准库，不依赖 GitPython；需要本机有 git 可执行文件做三方合并。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, '.upstream.json')
DEFAULT_REPO = 'jscjscjscjscjsc/super-guga'
SKIP_DIRS = {'.venv', '__pycache__', '.git', 'runtime', '.mypy_cache', '.pytest_cache'}
SKIP_FILES = {'ai_config.json', '.upstream.json'}
RAW_CHANNELS = [
    'https://raw.githubusercontent.com/{repo}/{ref}/{path}',
    'https://gh-proxy.com/https://raw.githubusercontent.com/{repo}/{ref}/{path}',
    'https://cdn.jsdelivr.net/gh/{repo}@{ref}/{path}',
]
API = 'https://api.github.com'


def log(msg):
    print(msg, flush=True)


def api(path):
    url = API + path
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={
                'Accept': 'application/vnd.github+json', 'User-Agent': 'guga-updater'})
            token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
            if token:
                req.add_header('Authorization', 'Bearer ' + token)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or b'{}')
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError('GitHub API 请求失败: %s (%s)' % (path, last))


def blob_sha_bytes(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def blob_sha_file(path):
    size = os.path.getsize(path)
    h = hashlib.sha1(b'blob %d\0' % size)
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def local_index():
    out = {}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name in SKIP_FILES:
                continue
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, ROOT).replace('\\', '/')
            try:
                out[rel] = blob_sha_file(full)
            except OSError:
                pass
    return out


def tree_of(repo, ref):
    data = api('/repos/%s/git/trees/%s?recursive=1' % (repo, ref))
    if data.get('truncated'):
        log('警告：文件树过大被截断，请手动更新')
    return ({e['path']: e['sha'] for e in data['tree'] if e['type'] == 'blob'}, data['sha'])


def fetch(repo, path, ref, expect, dest):
    if os.path.exists(dest) and blob_sha_file(dest) == expect:
        return 'cached'
    # 本地镜像优先：逐文件走 raw 太慢（大仓库要几十分钟），
    # 可以先 git clone --filter=blob:none 再有选择地 checkout，指到这里。
    cache = os.environ.get('GUGA_UPSTREAM_CACHE', '')
    if cache:
        for side in ('head', 'base'):
            src = os.path.join(cache, side, *path.split('/'))
            if os.path.exists(src) and blob_sha_file(src) == expect:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                return 'mirror-' + side
    last = None
    for attempt in range(4):
        for tpl in RAW_CHANNELS:
            url = tpl.format(repo=repo, ref=ref, path=urllib.parse.quote(path, safe='/'))
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'guga-updater'})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                if blob_sha_bytes(data) != expect:
                    last = '内容校验不符'
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, 'wb') as fh:
                    fh.write(data)
                return 'ok'
            except Exception as exc:  # noqa: BLE001
                last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError('%s 下载失败: %s' % (path, last))


def main():
    args = sys.argv[1:]
    check_only = '--check' in args
    repo = DEFAULT_REPO
    if '--repo' in args:
        repo = args[args.index('--repo') + 1]

    if not os.path.exists(MANIFEST):
        log('缺少 .upstream.json，无法判断基线。请手动更新一次后重建清单。')
        return 2
    manifest = json.load(open(MANIFEST, encoding='utf-8'))
    base_commit = manifest['commit']
    base = manifest['files']

    head_commit = api('/repos/%s/commits/HEAD' % repo)['sha']
    log('本地基线: %s' % base_commit[:8])
    log('上游最新: %s' % head_commit[:8])
    if head_commit == base_commit:
        log('已是最新，无需更新。')
        return 0

    head, head_tree = tree_of(repo, head_commit)
    local = local_index()

    new_files = sorted(p for p in head if p not in base)
    changed = sorted(p for p in head if p in base and head[p] != base[p])
    removed = sorted(p for p in base if p not in head)

    dirty = {p for p in local if local[p] != base.get(p)}
    conflicts = [p for p in changed if p in dirty]
    clean = [p for p in changed if p not in dirty]
    local_only = sorted(p for p in local if p not in base)

    log('')
    log('上游新增 %d / 上游修改 %d（本地也改过 %d）/ 上游删除 %d'
        % (len(new_files), len(changed), len(conflicts), len(removed)))
    log('本地独有文件 %d 个，不会被改动' % len(local_only))
    for p in conflicts:
        log('  * 需合并 %s' % p)
    if check_only:
        log('\n--check 模式，未改动任何文件。')
        return 0

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_dir = os.path.join(ROOT, '.upstream_backup', stamp)
    stage = os.path.join(backup_dir, 'stage')

    def backup(rel):
        src = os.path.join(ROOT, *rel.split('/'))
        if not os.path.exists(src):
            return
        dst = os.path.join(backup_dir, 'files', *rel.split('/'))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    # 每个任务带上取文件的 ref：冲突文件的 base 版本必须按基线提交取，
    # 否则拿到的永远是新版内容，sha 校验必然不符（旧版就在这里整体中止）。
    jobs = [(p, head[p], os.path.join(stage, 'head', *p.split('/')), head_commit)
            for p in new_files + clean]
    jobs += [(p, head[p], os.path.join(stage, 'head', *p.split('/')), head_commit)
             for p in conflicts]
    jobs += [(p, base[p], os.path.join(stage, 'base', *p.split('/')), base_commit)
             for p in conflicts]
    log('下载 %d 个文件...' % len(jobs))
    fails = []

    def run(job):
        path, sha, dest, ref = job
        try:
            return fetch(repo, path, ref, sha, dest)
        except Exception as exc:  # noqa: BLE001
            fails.append(str(exc))
            return 'fail'

    with ThreadPoolExecutor(10) as pool:
        list(pool.map(run, jobs))
    if fails:
        log('下载失败 %d 个，已中止（未改动任何文件）：' % len(fails))
        for msg in fails[:6]:
            log('  ' + msg)
        return 1

    for rel in new_files + clean:
        backup(rel)
        dst = os.path.join(ROOT, *rel.split('/'))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(stage, 'head', *rel.split('/')), dst)
    log('已写入：新增 %d，覆盖 %d' % (len(new_files), len(clean)))

    unresolved = 0
    for rel in conflicts:
        ours = os.path.join(ROOT, *rel.split('/'))
        basef = os.path.join(stage, 'base', *rel.split('/'))
        theirs = os.path.join(stage, 'head', *rel.split('/'))
        backup(rel)
        try:
            proc = subprocess.run(['git', 'merge-file', '-p', '--diff3', ours, basef, theirs],
                                  capture_output=True)
            data = proc.stdout
            code = proc.returncode
        except OSError:
            data = None
            code = -1
        if data is None:
            shutil.copy2(theirs, ours + '.upstream-new')
            log('  未找到 git，%s 保留本地版本，上游版本存为 *.upstream-new' % rel)
            unresolved += 1
            continue
        with open(ours, 'wb') as fh:
            fh.write(data)
        if code > 0:
            unresolved += code
            log('  合并 %s：%d 处冲突，已写冲突标记，请人工确认' % (rel, code))
        else:
            log('  合并 %s：自动完成' % rel)

    manifest = {'commit': head_commit, 'tree': head_tree, 'repo': repo,
                'updated_at': datetime.now().isoformat(timespec='seconds'),
                'files': head}
    with open(MANIFEST, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    log('')
    log('完成。备份目录：%s' % os.path.relpath(backup_dir, ROOT))
    if unresolved:
        log('有 %d 处冲突待人工处理（搜索 <<<<<<< 即可定位）。' % unresolved)
        return 1
    log('无冲突。建议接着跑：tools\\run_tests.py 与 tools\\verify_environment.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
