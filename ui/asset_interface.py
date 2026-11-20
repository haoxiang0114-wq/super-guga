# -*- coding: utf-8 -*-
"""M0 任务2: 员工动画资产接口 — 三级回退解析

resolve_clip(clip_name) 解析顺序:
  1. resources/armor/<active_armor>/<clip>/frames   (员工专属)
  2. resources/armor/base/<clip>/frames             (管家中枢)
  3. resources/actions/<ns>-motion/<clip>/frames    (默认动作, 从现有 manifest 查 fps)

素材替换零代码改动: 真实员工素材帧放入 armor/<id>/<clip>/frames/ 并在
armor-manifest.json 登记后, 表现层自动命中第一级, 无需改任何 .py 文件.
"""
import json
from pathlib import Path


class ClipSource:
    """解析结果: 帧目录 + 播放参数 + 命中级别标记."""
    __slots__ = ('frames_dir', 'fps', 'weight', 'source', 'armor_id',
                 'clip_name', 'cache_spec', 'one_shot', 'reversed')

    def __init__(self, frames_dir, fps, weight=1.0, source='actions',
                 armor_id='butler', clip_name='', cache_spec=None,
                 one_shot=False, reversed=False):
        self.frames_dir = str(frames_dir)
        self.fps = float(fps)
        self.weight = float(weight)
        self.source = source          # 'armor' | 'butler' | 'actions'
        self.armor_id = armor_id
        self.clip_name = clip_name
        self.cache_spec = cache_spec
        self.one_shot = bool(one_shot)
        self.reversed = bool(reversed)  # M0.1: 召回 = summon 倒放

    def __repr__(self):
        return ('ClipSource(%s/%s%s -> %s, fps=%.1f, source=%s)'
                % (self.armor_id, self.clip_name,
                   '(rev)' if self.reversed else '',
                   self.frames_dir, self.fps, self.source))


class ArmorAssetResolver:
    """员工资产三级回退解析器.

    与 SpriteAnimator 解耦: 只负责路径解析与 manifest 读取,
    由调用方(或 load_into)把 ClipSource 交给动画器加载.
    """
    # 七模式: 管家(常驻中枢) + 六位部门员工
    ARMOR_IDS = ('butler', 'scholar', 'tactician', 'steward',
                 'navigator', 'medic', 'artisan')
    HUB_ID = 'butler'

    def __init__(self, res_dir, armor_id='butler'):
        self._res = Path(res_dir)
        self._armor_dir = self._res / 'armor'
        self._actions_dir = self._res / 'actions'
        self._armor_id = armor_id if armor_id in self.ARMOR_IDS else self.HUB_ID
        self._armor_manifest = self._load_armor_manifest()
        self._actions_manifests = {}   # namespace -> manifest dict / None

    # ==================== 公开接口 ====================

    @property
    def armor_id(self):
        return self._armor_id

    def set_armor(self, armor_id):
        """切换当前员工(只改解析目标, 不触发动画)."""
        if armor_id not in self.ARMOR_IDS:
            return False
        self._armor_id = armor_id
        return True

    def resolve_clip(self, clip_name, namespace='idle', reversed=False):
        """三级回退解析. 返回 ClipSource 或 None(三级都未命中).

        reversed=True: 帧序列倒放(召回动画 = summon 视频倒放, 素材零成本).
        """
        clip_name = str(clip_name).strip()
        if not clip_name:
            return None
        # 1. armor/<active>/<clip>  — 员工专属
        hit = self._try_armor(self._armor_id, clip_name, source='armor')
        if hit:
            hit.reversed = bool(reversed)
            return hit
        # 2. armor/butler/<clip>  — 管家(常驻中枢)兜底
        if self._armor_id != self.HUB_ID:
            hit = self._try_armor(self.HUB_ID, clip_name, source='butler')
            if hit:
                hit.reversed = bool(reversed)
                return hit
        # 3. actions/<ns>-motion/<clip>  — 默认动作
        hit = self._try_actions(clip_name, namespace)
        if hit:
            hit.reversed = bool(reversed)
        return hit

    def load_into(self, animator, clip_name, namespace='idle', clip_key=None,
                  reversed=False):
        """解析 clip 并注册到 SpriteAnimator.clips.

        clip_key: 注册名, 缺省 '<namespace>:<clip_name>';
        reversed=True 时注册名建议传 '<name>#rev' 以区分正放版本.
        返回 ClipSource(成功) 或 None(解析失败或加载被审核拒绝).
        """
        src = self.resolve_clip(clip_name, namespace, reversed=reversed)
        if src is None:
            return None
        clip_key = clip_key or ('%s:%s' % (namespace, clip_name))
        animator._load_action_frames(clip_key, src.frames_dir, fps=src.fps,
                                     lazy=True, cache_spec=src.cache_spec)
        if clip_key in animator.clips:
            clip = animator.clips[clip_key]
            if src.one_shot:
                clip['one_shot_only'] = True
                clip['loop'] = False
            if src.reversed:
                seq = clip.get('frames')
                if seq is not None and hasattr(seq, 'paths'):
                    seq.paths = seq.paths[::-1]  # 倒放: 帧序反转(勿用内置 reversed(), 参数名遮蔽)
                    try:
                        seq.cache.clear()  # 丢弃已按正序缓存的帧
                    except Exception:
                        pass
            return src
        return None

    # ==================== 内部 ====================

    def _load_armor_manifest(self):
        path = self._armor_dir / 'armor-manifest.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'armor_clips': {}}

    def _try_armor(self, armor_id, clip_name, source):
        """armor 级: 目录存在即命中(manifest 未登记时用默认参数)."""
        frames_dir = self._armor_dir / armor_id / clip_name / 'frames'
        if not frames_dir.is_dir():
            return None
        spec = (self._armor_manifest.get('armor_clips', {})
                .get(armor_id, {}).get(clip_name)) or {}
        return ClipSource(
            frames_dir,
            float(spec.get('native_fps', 24.0)),
            float(spec.get('weight', 1.0)),
            source, armor_id, clip_name,
            spec.get('cache'),
            bool(spec.get('one_shot', False)),
        )

    def _try_actions(self, clip_name, namespace):
        """actions 级: 从现有 manifest.json 查 fps/frames."""
        manifest = self._actions_manifest(namespace)
        if manifest is None:
            return None
        base = self._actions_dir / ('%s-motion' % namespace)
        if namespace == 'idle':
            for spec in manifest.get('clips', []):
                if str(spec.get('name', '')).strip() == clip_name:
                    return ClipSource(base / spec['frames'],
                                      float(spec['native_fps']),
                                      float(spec.get('weight', 1.0)),
                                      'actions', 'butler', clip_name,
                                      spec.get('cache'))
        else:  # task 等命名空间走 states 字典: 按状态名或帧目录名匹配
            states = manifest.get('states', {})
            spec = states.get(clip_name)  # 按状态名 (如 'thinking')
            if spec:
                return ClipSource(base / spec['frames'],
                                  float(spec['native_fps']),
                                  1.0, 'actions', 'butler', clip_name,
                                  spec.get('cache'))
            for state_name, st in states.items():  # 按帧目录名 (如 'look-up-step')
                if str(st.get('frames', '')).split('/')[0] == clip_name:
                    return ClipSource(base / st['frames'],
                                      float(st['native_fps']),
                                      1.0, 'actions', 'butler', state_name,
                                      st.get('cache'))
        return None

    def _actions_manifest(self, namespace):
        key = '%s-motion' % namespace
        if key not in self._actions_manifests:
            path = self._actions_dir / key / 'manifest.json'
            try:
                self._actions_manifests[key] = json.loads(
                    path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                self._actions_manifests[key] = None
        return self._actions_manifests[key]


# ==================== 模块级便捷函数 ====================

_resolver = None


def get_resolver(res_dir=None):
    """模块级单例; 首次调用必须传 res_dir."""
    global _resolver
    if _resolver is None or (res_dir and str(_resolver._res) != str(res_dir)):
        if res_dir is None:
            raise ValueError('首次调用必须提供 res_dir')
        _resolver = ArmorAssetResolver(res_dir)
    return _resolver


def resolve_clip(clip_name, namespace='idle', res_dir=None):
    """便捷入口: resolve_clip('doze') 按当前员工走三级回退."""
    return get_resolver(res_dir).resolve_clip(clip_name, namespace)