"""PyMaster 教学站与本机咕嘎之间的桥。

作用：让咕嘎（桌宠智能体）能真正"进到" PyMaster 页面里做事——打开章节、
翻知识点、打开练习、写代码、跑代码、读报错，并把页面上的知识点/错题
整理成可供大模型讲解的事实包。查词走站点自带的术语表，不猜。

设计边界（与本项目其它模块一致）：
  * 只操作自己启动的专用浏览器，不接管用户现有标签；
  * 只允许 PyMaster 这一个站点及其静态资源域，跳别处会被拒绝；
  * 页面里的文字一律当资料，不当指令（返回值带 untrusted_page_data）；
  * 固定动作 + 固定 JS，不接受外部传入的任意脚本。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = 'https://www.workbuddy.link/p/uTIQLihsxWIj6pIM7Cx8lc'
STATIC_HOST = 'workbuddy-space-static.codebuddy.work'
PROFILE = ROOT / 'runtime' / 'pymaster-profile'
GLOSSARY_CACHE = ROOT / 'runtime' / 'pymaster-glossary.json'
BUNDLED_GLOSSARY = ROOT / 'data' / 'pymaster-glossary.json'
IDLE_SECONDS = 600


def available():
    """能否跑起来：需要 playwright + 一个可用浏览器。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, '未安装 playwright'
    return True, ''


DEFAULT_SPEAK = True


def settings():
    """管理台里保存的 PyMaster 偏好；读不到就用内置默认值。

    对应 ai_config.json 的 pymaster 段：
      url           教学站地址（换了发布链接只改这里）
      speak_lookup  查词结果是否直接朗读
      headless      是否用后台浏览器（不弹窗口）
    """
    raw = {}
    try:
        from core.model_settings import read_raw
        raw = read_raw() or {}
    except Exception:  # noqa: BLE001 - 配置读不到不影响默认值
        raw = {}
    block = raw.get('pymaster') if isinstance(raw.get('pymaster'), dict) else {}
    url = str(block.get('url') or SITE).strip() or SITE
    speak = block.get('speak_lookup')
    headless = block.get('headless')
    # 环境变量优先，方便命令行/自检临时切换，不用改配置文件
    env = os.environ.get('GUGA_PYMASTER_HEADLESS', '').strip().lower()
    if env in ('1', 'true', 'yes'):
        headless = True
    elif env in ('0', 'false', 'no'):
        headless = False
    return dict(url=url,
                speak_lookup=DEFAULT_SPEAK if speak is None else bool(speak),
                headless=bool(headless))


def _browser_launch(context_factory, profile, headless):
    """优先用 playwright 自带 chromium，失败再退回本机 Edge/Chrome。

    国内机器常常只装了 Edge，playwright 的 chromium 下载又容易被墙；
    退到 msedge 不影响页面操作能力，用户也不用额外下载 130MB。
    """
    errors = []
    for channel in (os.environ.get('GUGA_BROWSER_CHANNEL') or '', 'msedge', 'chrome'):
        kwargs = dict(headless=headless, viewport=dict(width=1440, height=900), accept_downloads=True)
        if channel:
            kwargs['channel'] = channel
        try:
            return context_factory(str(profile), **kwargs)
        except Exception as exc:  # noqa: BLE001 - 逐个通道尝试
            errors.append((channel or 'bundled-chromium') + ': ' + str(exc)[:160])
    raise RuntimeError('没有可用浏览器（' + '；'.join(errors) + '）')


STATE_JS = r"""
() => {
  const clean = (s) => String(s || '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  const path = location.pathname;
  const kind = /chapter-(\d+)/.test(path) ? 'chapter'
    : /playground/.test(path) ? 'playground'
    : /training/.test(path) ? 'training'
    : /stars/.test(path) ? 'stars'
    : /canvas/.test(path) ? 'canvas'
    : /^.*\/(index\.html)?$/.test(path) ? 'dashboard' : 'other';
  const chapterNo = (/chapter-(\d+)/.exec(path) || [])[1] || null;
  const heading = document.querySelector('.chapter-hero h1, .chapter-header h1, .chapter-title, h1');
  const kps = [...document.querySelectorAll('.kp-item')].map((el) => ({
    index: Number(el.dataset.kpIndex),
    title: clean((el.querySelector('.kp-title') || {}).innerText).slice(0, 80),
    open: el.classList.contains('open') || el.classList.contains('active'),
    exercises: el.querySelectorAll('.kp-exercise').length
  }));
  const openItem = document.querySelector('.kp-item.open, .kp-item.active');
  const kpText = openItem ? clean((openItem.querySelector('.kp-body-content') || openItem).innerText).slice(0, 3000) : '';
  const kpExercises = openItem ? [...openItem.querySelectorAll('.kp-exercise')].slice(0, 6).map((el, i) => ({
    index: Number(el.dataset.exIdx || i),
    text: clean(el.innerText).slice(0, 600)
  })) : [];
  const modalEl = document.getElementById('code-ex-modal');
  const outputEl = document.getElementById('code-ex-output');
  const statusEl = document.getElementById('code-ex-status');
  let modal = { open: !!(modalEl && modalEl.classList.contains('active')) };
  if (modal.open) {
    let code = '';
    try { code = (typeof codeMirrorEditor !== 'undefined' && codeMirrorEditor) ? codeMirrorEditor.getValue() : (document.getElementById('code-ex-editor') || {}).value || ''; } catch (e) { code = ''; }
    modal = Object.assign(modal, {
      prompt: clean((document.getElementById('code-ex-prompt') || {}).innerText).slice(0, 800),
      code: String(code || '').slice(0, 4000),
      output: clean(outputEl ? outputEl.innerText : '').slice(0, 3000),
      status: clean(statusEl ? statusEl.innerText : '').slice(0, 200)
    });
  }
  const pgOutput = document.getElementById('output-content');
  const pgStatus = document.getElementById('status-text');
  const playground = /playground/.test(path) ? {
    code: (() => { try { return (typeof editor !== 'undefined' && editor) ? editor.getValue().slice(0, 4000) : (document.getElementById('code-editor') || {}).value || ''; } catch (e) { return ''; } })(),
    output: clean(pgOutput ? pgOutput.innerText : '').slice(0, 3000),
    status: clean(pgStatus ? pgStatus.innerText : '').slice(0, 200)
  } : null;
  const toastEl = document.getElementById('pm-static-toast');
  return {
    kind, chapter: chapterNo ? Number(chapterNo) : null,
    title: clean((heading || {}).innerText).slice(0, 120) || clean(document.title),
    path, kps, kp: openItem ? { index: Number(openItem.dataset.kpIndex), title: kps.find((k) => k.index === Number(openItem.dataset.kpIndex))?.title || '' } : null,
    kp_text: kpText, kp_exercises: kpExercises,
    modal, playground,
    toast: clean(toastEl ? toastEl.innerText : '').slice(0, 200),
    static_demo: !!window.PYMASTER_STATIC
  };
}
"""

CODE_JS = r"""
() => {
  try {
    if (typeof codeMirrorEditor !== 'undefined' && codeMirrorEditor) return codeMirrorEditor.getValue();
    const ta = document.getElementById('code-ex-editor');
    return ta ? ta.value : '';
  } catch (e) { return ''; }
}
"""


def glossary_terms():
    """术语表：优先用缓存，其次线上，最后用随包附带的副本。"""
    for path in (GLOSSARY_CACHE, BUNDLED_GLOSSARY):
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        terms = data.get('terms') if isinstance(data, dict) else data
        if isinstance(terms, list) and terms:
            return terms
    return []


def lookup_term(word, limit=3):
    """在 PyMaster 术语表里查词：先精确，再包含匹配（中英文都试）。"""
    word = str(word or '').strip().strip('.,;:!?()[]{}"\'`，。；：！？（）【】「」')
    if not word:
        return dict(ok=False, error='没有要查的词')
    terms = glossary_terms()
    if not terms:
        return dict(ok=False, error='术语表为空：先运行一次 PyMaster 桥（会把术语表缓存到 runtime/）')
    low = word.lower()
    exact, loose = [], []
    for item in terms:
        term = str(item.get('term') or '')
        en = str(item.get('en') or '')
        if low == term.lower() or low == en.lower():
            exact.append(item)
        elif (len(word) >= 2 and (word in term or low in en.lower())) or (term and term in word):
            loose.append(item)
    hits = (exact or loose)[:limit]
    if not hits:
        return dict(ok=False, word=word, error='PyMaster 术语表里没有这一条', total=len(terms))
    return dict(ok=True, word=word, source='pymaster-glossary', total=len(terms),
                matches=[dict(term=h.get('term'), en=h.get('en'), desc=h.get('desc')) for h in hits])


class PyMasterBridge:
    """父进程一侧：一个专用浏览器 worker，串行执行固定动作。"""

    def __init__(self, profile=None):
        self.process = None
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.profile = Path(profile) if profile else PROFILE
        self.last_used = 0.0

    # ── 进程管理 ─────────────────────────────────────────
    def _launch(self):
        executable = Path(sys.executable)
        if os.name == 'nt' and executable.with_name('python.exe').is_file():
            executable = executable.with_name('python.exe')
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        self.responses = queue.Queue()
        self.process = subprocess.Popen(
            [str(executable), '-m', 'core.pymaster_bridge', '--worker', str(os.getpid())],
            cwd=str(ROOT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        def read(proc):
            try:
                for line in proc.stdout:
                    try:
                        self.responses.put(json.loads(line))
                    except ValueError:
                        pass
            except (OSError, ValueError):
                pass
            finally:
                self.responses.put(None)

        threading.Thread(target=read, args=(self.process,), daemon=True, name='guga-pymaster-replies').start()

    def close(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            try:
                process.stdin.write(json.dumps(dict(id='close', args=dict(action='stop'))) + '\n')
                process.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    import psutil
                    parent = psutil.Process(process.pid)
                    for child in reversed(parent.children(recursive=True)):
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    parent.kill()
                except Exception:  # noqa: BLE001
                    process.kill()
        if process:
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, AttributeError):
                    pass

    def active(self):
        return bool(self.process and self.process.poll() is None)

    # ── 请求 ────────────────────────────────────────────
    def run(self, action, timeout=90, **kwargs):
        action = str(action or 'state').strip().lower()
        if action == 'status':
            return dict(ok=True, active=self.active(), site=settings()['url'], profile=str(self.profile))
        if action == 'stop':
            self.close()
            return dict(ok=True, active=False)
        if action == 'glossary':
            return lookup_term(kwargs.get('term') or kwargs.get('query') or '')
        if not self.lock.acquire(timeout=max(1, min(20, timeout // 6))):
            return dict(ok=False, error='PyMaster 浏览器正在执行上一步，请稍后')
        try:
            if not self.active():
                self.close()
                self._launch()
            request = uuid.uuid4().hex
            payload = dict(id=request, args=dict(action=action, **kwargs))
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            deadline = time.monotonic() + max(15, timeout)
            while time.monotonic() < deadline:
                try:
                    response = self.responses.get(timeout=.2)
                except queue.Empty:
                    continue
                if response is None:
                    break
                if response.get('id') == request:
                    self.last_used = time.monotonic()
                    return response.get('result') or dict(ok=False, error='空回执')
            self.close()
            return dict(ok=False, error='PyMaster 浏览器超时或已退出，已清理；页面上的改动可能已发生，不要直接重放提交')
        except (OSError, ValueError) as exc:
            self.close()
            return dict(ok=False, error='PyMaster 桥通信失败：' + type(exc).__name__ + ': ' + str(exc)[:200])
        finally:
            self.lock.release()

    # ── 给智能体的便捷动作 ────────────────────────────────
    def state(self, **kw):
        return self.run('state', **kw)

    def open(self, page=None, chapter=None):
        args = {}
        if page:
            args['page'] = page
        if chapter:
            args['chapter'] = int(chapter)
        return self.run('open', **args)

    def chapter(self, number):
        return self.run('chapter', chapter=int(number))

    def kp(self, index):
        return self.run('kp', index=int(index))

    def open_exercise(self, kp=None, ex=0):
        args = {'ex': int(ex)}
        if kp is not None:
            args['kp'] = int(kp)
        return self.run('open_exercise', **args)

    def set_code(self, code):
        return self.run('set_code', code=str(code))

    def run_code(self, seconds=75):
        return self.run('run', timeout=int(seconds) + 25, seconds=int(seconds))

    def explain_bundle(self, question=''):
        return self.run('explain', question=str(question)[:500])


# ─────────────────────────────────────────────────────────
# worker：真正持有 Playwright 的那一侧
# ─────────────────────────────────────────────────────────
class PyMasterPage:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self.focus_kp = None
        self.prefs = settings()
        self.site = self.prefs['url']
        self.headless = self.prefs['headless']

    def close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            self.context = None
            self.page = None
            if self.playwright:
                self.playwright.stop()
                self.playwright = None

    def _launch(self):
        from playwright.sync_api import sync_playwright
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.playwright = sync_playwright().start()
        self.context = _browser_launch(self.playwright.chromium.launch_persistent_context, PROFILE, self.headless)
        self.context.set_default_timeout(8000)
        self.context.set_default_navigation_timeout(45000)
        self.context.on('dialog', lambda dialog: dialog.dismiss())
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def _ensure(self):
        if not self.context:
            self._launch()
        return self.page

    def _frame(self, wait=20):
        page = self._ensure()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            frames = [f for f in page.frames if STATIC_HOST in (f.url or '')]
            if frames:
                frame = frames[0]
                try:
                    frame.wait_for_load_state('domcontentloaded', timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                return frame
            page.wait_for_timeout(400)
        raise RuntimeError('页面里没有找到 PyMaster 内容（可能站点结构变了或网络打不开）')

    def _goto_site(self):
        page = self._ensure()
        if STATIC_HOST in (page.url or ''):
            return page
        page.goto(self.site, wait_until='domcontentloaded')
        return page

    @staticmethod
    def _page_file(page):
        return {'dashboard': 'index.html', 'playground': 'playground.html', 'training': 'training.html',
                'stars': 'stars.html', 'canvas': 'canvas.html'}.get(page)

    # ── 动作 ────────────────────────────────────────────
    def action(self, args):
        action = args.get('action')
        if action == 'stop':
            self.close()
            return dict(ok=True, active=False)
        if action == 'status':
            return dict(ok=True, active=bool(self.context), site=self.site, headless=self.headless,
                        url=(self.page.url if self.page else ''))
        if action in ('open', 'chapter', 'state', 'kp', 'next', 'prev', 'open_exercise',
                      'set_code', 'run', 'explain', 'close_modal', 'page'):
            return getattr(self, '_act_' + action)(args)
        raise ValueError('未知 PyMaster 动作：' + str(action))

    def _act_open(self, args):
        self._reset_focus()
        self._goto_site()
        page = self._ensure()
        target = self._page_file(args.get('page'))
        if target:
            frame = self._frame()
            frame.evaluate('(f) => { if (!location.pathname.endsWith(f)) location.href = f; }', target)
        if args.get('chapter'):
            frame = self._frame()
            frame.evaluate('(n) => goToChapter(n)', int(args['chapter']))
        frame = self._frame()
        try:
            frame.wait_for_selector('.chapter-card, .kp-item, #output-content, .qbank-list, #root',
                                    timeout=25000)
        except Exception:  # noqa: BLE001
            pass
        page.bring_to_front()
        result = self._state(frame)
        result.update(ok=True, opened=True, site=self.site)
        return result

    def _act_page(self, args):
        return self._act_open(args)

    def _act_chapter(self, args):
        self._reset_focus()
        number = int(args.get('chapter') or 0)
        if not 1 <= number <= 99:
            raise ValueError('章号应在 1-99 之间')
        frame = self._frame()
        state = self._state(frame)
        if state.get('kind') == 'chapter':
            frame.evaluate('(n) => { location.href = "chapter-" + n + ".html"; }', number)
        else:
            frame.evaluate('(n) => goToChapter(n)', number)
        frame = self._frame()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = self._state(frame)
            if state.get('kind') == 'chapter' and state.get('chapter') == number:
                break
            self._ensure().wait_for_timeout(400)
            frame = self._frame()
        result = self._state(frame)
        result.update(ok=True, chapter=number)
        return result

    def _act_state(self, args):
        frame = self._frame()
        result = self._state(frame)
        result.update(ok=True)
        return result

    def _act_kp(self, args):
        index = int(args.get('index', 0))
        frame = self._frame()
        # 站点的知识点是"点开就堆着不收起"的手风琴，语音说"下一个"时如果都留着，
        # 页面会变成一长条。这里只保留目标那一个展开，并把它滚到屏幕中间。
        ok = frame.evaluate("""(i) => {
          const items = [...document.querySelectorAll('.kp-item')];
          const target = items.find((el) => el.dataset.kpIndex === String(i));
          if (!target) return false;
          const isOpen = (el) => el.classList.contains('open') || el.classList.contains('active');
          items.forEach((el) => {
            if (el !== target && isOpen(el)) { (el.querySelector('.kp-header') || el).click(); }
          });
          if (!isOpen(target)) { (target.querySelector('.kp-header') || target).click(); }
          target.scrollIntoView({block: 'center'});
          return true;
        }""", index)
        if not ok:
            raise ValueError('这一章没有第 %d 个知识点' % index)
        self.focus_kp = index
        self._ensure().wait_for_timeout(450)
        result = self._state(frame)
        result.update(ok=True)
        return result

    def _act_next(self, args):
        return self._shift(args, 1)

    def _act_prev(self, args):
        return self._shift(args, -1)

    def _shift(self, args, delta):
        frame = self._frame()
        state = self._state(frame)
        kps = state.get('kps') or []
        if not kps:
            raise ValueError('当前页面没有知识点列表；先打开某一章')
        indexes = [k['index'] for k in kps]
        current = getattr(self, 'focus_kp', None)
        if current is None:
            current = (state.get('kp') or {}).get('index')
        if current is None:
            return self._act_kp(dict(index=indexes[0] if delta > 0 else indexes[-1]))
        target = current + delta
        if target not in indexes:
            raise ValueError('已经到%s了' % ('最后' if delta > 0 else '最前'))
        return self._act_kp(dict(index=target))

    def _act_open_exercise(self, args):
        frame = self._frame()
        state = self._state(frame)
        if state.get('kind') != 'chapter':
            raise ValueError('只有章节页有课后练习；先打开对应章节')
        kp_index = args.get('kp')
        if kp_index is None:
            kp_index = (state.get('kp') or {}).get('index')
        if kp_index is None:
            kp_index = 0
        ex_index = int(args.get('ex') or 0)
        # 先打开知识点，保证练习卡是展开可见的
        frame.evaluate("""(i) => {
          const el = document.querySelector('.kp-item[data-kp-index="' + i + '"]');
          if (!el) return false;
          if (!(el.classList.contains('open') || el.classList.contains('active'))) {
            (el.querySelector('.kp-header') || el).click();
          }
          el.scrollIntoView({block: 'center'});
          return true;
        }""", int(kp_index))
        self._ensure().wait_for_timeout(400)
        clicked = frame.evaluate("""(args) => {
          const item = document.querySelector('.kp-item[data-kp-index="' + args.kp + '"]');
          if (!item) return {ok: false, reason: 'no-kp'};
          const cards = [...item.querySelectorAll('.kp-exercise')];
          const card = cards[args.ex] || cards[0];
          if (!card) return {ok: false, reason: 'no-exercise'};
          const btn = card.querySelector('.btn-code-ex');
          const prompt = ((card.querySelector('.ex-question') || {}).innerText || '').trim().slice(0, 800);
          const hint = ((card.querySelector('.ex-code-hint') || {}).innerText || '').trim().slice(0, 400);
          if (btn) { btn.click(); }
          return {ok: true, clicked: !!btn, prompt: prompt, hint: hint,
                  modal: !!(document.getElementById('code-ex-modal') || {}).classList &&
                          document.getElementById('code-ex-modal').classList.contains('active')};
        }""", dict(kp=int(kp_index), ex=ex_index))
        if not clicked.get('ok'):
            raise ValueError('这个知识点下没有可作答的代码练习（%s）' % clicked.get('reason'))
        self._ensure().wait_for_timeout(600)
        opened = frame.evaluate("() => (document.getElementById('code-ex-modal') || {}).classList?.contains('active') || false")
        if not opened:
            # 线上站部分按钮的 onclick 被发布管线转义坏了，点不动；直接用页面自己的函数打开，
            # 题干从上方的可见卡片里取，起始代码留空由学生（或咕嘎）来写。
            prompt = (clicked.get('prompt') or '').strip()
            if clicked.get('hint'):
                prompt = (prompt + '\n' + clicked['hint']).strip()
            frame.evaluate("""(a) => {
              if (typeof openCodeEx !== 'function') return false;
              openCodeEx(a.chapter, a.kp, a.ex, a.prompt, '');
              return true;
            }""", dict(chapter=int(state.get('chapter') or 1), kp=int(kp_index), ex=ex_index, prompt=prompt))
            self._ensure().wait_for_timeout(600)
            opened = frame.evaluate("() => (document.getElementById('code-ex-modal') || {}).classList?.contains('active') || false")
        result = self._state(frame)
        result.update(ok=True, exercise_opened=bool(opened), fallback_used=bool(clicked.get('clicked') is False or not opened))
        return result

    def _act_set_code(self, args):
        code = str(args.get('code') or '')
        if len(code) > 20000:
            raise ValueError('代码太长（上限 20000 字符）')
        frame = self._frame()
        written = frame.evaluate("""(code) => {
          const modal = document.getElementById('code-ex-modal');
          if (modal && modal.classList.contains('active')) {
            try {
              if (typeof codeMirrorEditor !== 'undefined' && codeMirrorEditor) { codeMirrorEditor.setValue(code); return 'chapter-editor'; }
            } catch (e) {}
            const ta = document.getElementById('code-ex-editor');
            if (ta) { ta.value = code; ta.dispatchEvent(new Event('input', {bubbles: true})); return 'chapter-textarea'; }
          }
          try {
            if (typeof editor !== 'undefined' && editor && editor.setValue) { editor.setValue(code); return 'playground-editor'; }
          } catch (e) {}
          const pg = document.getElementById('code-editor');
          if (pg) { pg.value = code; pg.dispatchEvent(new Event('input', {bubbles: true})); return 'playground-textarea'; }
          return '';
        }""", code)
        if not written:
            raise ValueError('当前页面没有可写入的代码编辑器；先打开练习或练习场')
        self._ensure().wait_for_timeout(200)
        result = self._state(frame)
        result.update(ok=True, editor=written, chars=len(code))
        return result

    def _act_run(self, args):
        seconds = max(10, min(180, int(args.get('seconds') or 75)))
        frame = self._frame()
        started = frame.evaluate("""() => {
          const modal = document.getElementById('code-ex-modal');
          if (modal && modal.classList.contains('active')) {
            const btn = modal.querySelector('.btn-code-run');
            if (btn) { btn.click(); return 'chapter'; }
          }
          const run = document.getElementById('btn-run');
          if (run) { run.click(); return 'playground'; }
          if (typeof runCode === 'function') { runCode(); return 'playground-fn'; }
          return '';
        }""")
        if not started:
            raise ValueError('当前页面没有找到运行按钮；先打开练习或练习场')
        page = self._ensure()
        deadline = time.monotonic() + seconds
        last = {}
        stable = 0
        while time.monotonic() < deadline:
            page.wait_for_timeout(900)
            state = self._state(frame)
            last = state.get('modal') if state.get('kind') == 'chapter' or (state.get('modal') or {}).get('open') else state.get('playground') or {}
            output = (last.get('output') or '').strip()
            status = (last.get('status') or '').strip()
            busy = ('运行中' in status or '启动内置' in status or '执行中' in status or
                    output in ('运行中...', '正在执行...', ''))
            if not busy:
                stable += 1
                if stable >= 1:
                    break
            else:
                stable = 0
        result = self._state(frame)
        block = result.get('modal') if (result.get('modal') or {}).get('open') else result.get('playground') or {}
        output = (block.get('output') or '')
        status = (block.get('status') or '')
        failure = any(mark in output + status for mark in ('Traceback', 'Error', '❌', '✗', '出错', 'SyntaxError', 'NameError', 'TypeError'))
        elapsed = max(0, int(seconds - max(0, deadline - time.monotonic())))
        return dict(ok=True, ran=True, runtime='playground' if started != 'chapter' else 'chapter',
                    output=output, status=status, failed=failure, elapsed=elapsed,
                    modal_open=bool((result.get('modal') or {}).get('open')), state=result)

    def _act_close_modal(self, args):
        frame = self._frame()
        frame.evaluate("() => { if (typeof closeCodeEx === 'function') closeCodeEx(); }")
        result = self._state(frame)
        result.update(ok=True)
        return result

    def _act_explain(self, args):
        """给大模型的事实包：当前章节/知识点、题目、代码、运行输出、术语命中。"""
        frame = self._frame()
        state = self._state(frame)
        text = state.get('kp_text') or ''
        code = ''
        output = ''
        status = ''
        modal = state.get('modal') or {}
        playground = state.get('playground') or {}
        if modal.get('open'):
            code, output, status = modal.get('code') or '', modal.get('output') or '', modal.get('status') or ''
        elif playground:
            code, output, status = playground.get('code') or '', playground.get('output') or '', playground.get('status') or ''
        keywords = []
        blob = (text + ' ' + code + ' ' + output).lower()
        for item in glossary_terms():
            term = str(item.get('term') or '')
            en = str(item.get('en') or '')
            if not term:
                continue
            if term and term in blob:
                keywords.append(item)
            elif en and en.lower() in blob and len(en) > 2:
                keywords.append(item)
            if len(keywords) >= 6:
                break
        failure = any(mark in (output + status) for mark in ('Traceback', 'Error', '❌', '✗', '出错'))
        return dict(ok=True, page=state.get('kind'), chapter=state.get('chapter'), title=state.get('title'),
                    kp=state.get('kp'), kp_text=text[:2600], exercises=(state.get('kp_exercises') or [])[:3],
                    exercise_prompt=(modal.get('prompt') if modal.get('open') else ''),
                    code=code[:3000], output=output[:2500], status=status[:200], failed=failure,
                    glossary=[dict(term=i.get('term'), en=i.get('en'), desc=i.get('desc')) for i in keywords],
                    question=str(args.get('question') or '')[:500],
                    static_demo=state.get('static_demo'), untrusted_page_data=True)

    def _state(self, frame):
        try:
            state = frame.evaluate(STATE_JS)
        except Exception:  # noqa: BLE001 - 页面正在跳转
            self._ensure().wait_for_timeout(400)
            frame = self._frame()
            state = frame.evaluate(STATE_JS)
        focus = getattr(self, 'focus_kp', None)
        if focus is not None:
            opened = {k['index'] for k in state.get('kps') or [] if k.get('open')}
            if focus in opened and (state.get('kp') or {}).get('index') != focus:
                title = next((k['title'] for k in state.get('kps') or [] if k['index'] == focus), '')
                state['kp'] = dict(index=focus, title=title)
        return state

    def _reset_focus(self):
        self.focus_kp = None


def serve(parent_pid):
    """worker 主循环：一个请求一行 JSON，父进程退出后自动收摊。"""
    try:
        import psutil
        parent_alive = lambda: psutil.pid_exists(parent_pid)  # noqa: E731
    except ImportError:
        parent_alive = lambda: True  # noqa: E731

    page = PyMasterPage()
    lock = threading.Lock()

    def read():
        for line in sys.stdin:
            try:
                requests.put(json.loads(line))
            except ValueError:
                pass
        requests.put(None)

    requests = queue.Queue()
    threading.Thread(target=read, daemon=True).start()
    last = time.monotonic()
    try:
        while parent_alive() and time.monotonic() - last < IDLE_SECONDS:
            try:
                request = requests.get(timeout=1)
            except queue.Empty:
                continue
            if request is None:
                break
            last = time.monotonic()
            with lock:
                try:
                    result = page.action(request.get('args') or {})
                except Exception as exc:  # noqa: BLE001
                    from core.desktop_access import redact
                    result = dict(ok=False, error=redact(type(exc).__name__ + ': ' + str(exc))[:400])
            print(json.dumps(dict(id=request.get('id'), result=result), ensure_ascii=False), flush=True)
    finally:
        page.close()


def main(argv):
    if '--worker' in argv:
        serve(int(argv[argv.index('--worker') + 1]))
        return 0
    # 手工排查用：python -m core.pymaster_bridge state
    import pprint
    action = argv[1] if len(argv) > 1 else 'state'
    bridge = PyMasterBridge()
    try:
        if action == 'glossary':
            pprint.pprint(lookup_term(' '.join(argv[2:]) or '变量'))
        elif action == 'explain':
            pprint.pprint(bridge.explain_bundle(' '.join(argv[2:])))
        else:
            pprint.pprint(bridge.run(action, **{'chapter': int(argv[2])} if action == 'chapter' and len(argv) > 2 else {}))
    finally:
        bridge.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
