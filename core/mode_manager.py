# -*- coding: utf-8 -*-
"""M0 任务3: 员工模式管理器 — ModeManager

职责:
  - 注册/列举/切换 ArmorPack (armor_packs/*.json)
  - 切换信号序列: mode_changing -> transform clip -> mode_changed -> idle clip
  - 权限收敛钩子: 切换前通知外部(工具白名单/技能切片/知识目录)
  - Qt 信号解耦: 与 UI 层通过 pyqtSignal 通信, 不直接操作窗口

设计约束: 不修改 core/ 既有模块; ModeManager 是新增独立模块,
通过 SpriteAnimator.on_clip_finished 回调感知变身片段结束.
"""
import json
import logging
from pathlib import Path

try:
    from PyQt5.QtCore import QObject, pyqtSignal
    _HAS_QT = True
except ImportError:  # 无 Qt 环境(纯逻辑测试)时降级为可工作的哑信号
    _HAS_QT = False

    class QObject:
        def __init__(self, *a, **kw):
            pass

    class pyqtSignal:
        def __init__(self, *a, **kw):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, *a, **kw):
            for slot in self._slots:
                try:
                    slot(*a, **kw)
                except Exception:
                    logger.exception('signal slot error')

logger = logging.getLogger(__name__)

# ArmorPack 必填字段
PACK_REQUIRED = ('id', 'display_name')
# 权限收敛关注字段
PERMISSION_FIELDS = ('tool_whitelist', 'skill_slice', 'knowledge_dirs', 'permissions')


class ArmorPack:
    """员工卡: 一个"部门员工"的人格/权限/动画/语音配置.

    七模式: butler(管家·常驻中枢) + 六位部门员工(scholar/tactician/steward/
    navigator/medic/artisan). summon_clip = 管家召唤本甲的变身片段;
    召回(回到管家) = 该片段倒放, 素材零成本.
    """

    def __init__(self, data, source_path=None):
        if not isinstance(data, dict):
            raise ValueError('ArmorPack data must be dict')
        for field in PACK_REQUIRED:
            if field not in data:
                raise ValueError('ArmorPack missing required field: %s' % field)
        self.id = str(data['id'])
        self.display_name = str(data.get('display_name', self.id))
        self.persona_prompt = str(data.get('persona_prompt', ''))
        self.address = str(data.get('address', '少爷'))    # 对用户的称谓, 可配置
        self.priority = str(data.get('priority', 'thin'))  # deep|thin|hub
        self.tool_whitelist = list(data.get('tool_whitelist', []))
        self.skill_slice = list(data.get('skill_slice', []))
        self.knowledge_dirs = list(data.get('knowledge_dirs', []))
        self.voice_pack = data.get('voice_pack') or None
        self.permissions = dict(data.get('permissions', {}))
        anim = data.get('animation', {}) or {}
        # summon_clip 新命名; transform_clip 兼容旧版任务卡
        self.summon_clip = anim.get('summon_clip') or anim.get('transform_clip') or None
        self.idle_clip = anim.get('idle_clip') or None
        self.armor_id = anim.get('armor_id', self.id)
        self.source_path = str(source_path) if source_path else None
        self._raw = data

    @property
    def is_hub(self):
        """是否常驻中枢(管家)."""
        return self.id == 'butler'

    def permission_snapshot(self):
        """权限快照: 供收敛钩子比对."""
        return {f: getattr(self, f) for f in PERMISSION_FIELDS}

    def __repr__(self):
        return 'ArmorPack(%s: %s)' % (self.id, self.display_name)


class ModeManager(QObject if _HAS_QT else object):
    """员工(模式)管理器: 管家接收少爷命令, 召唤/召回部门员工.

    信号:
      mode_changing(str, str)  — (old_id, new_id), 切换开始(权限收敛时机)
      mode_changed(str)        — (new_id), 切换完成(动画/语音/知识库更新时机)
      mode_rejected(str, str)  — (attempted_id, reason), 切换被拒绝
    """

    mode_changing = pyqtSignal(str, str)
    mode_changed = pyqtSignal(str)
    mode_rejected = pyqtSignal(str, str)

    HUB_ID = 'butler'  # 管家 = 常驻中枢, 启动默认模式
    RECALL_SUFFIX = '#rev'  # 召回倒放 clip 的注册名后缀

    def __init__(self, packs_dir=None, animator=None, resolver=None,
                 parent=None, default_mode=HUB_ID):
        super().__init__(parent) if _HAS_QT else super().__init__()
        self._packs = {}           # id -> ArmorPack
        self._current = None       # 当前 ArmorPack
        self._animator = None      # SpriteAnimator (可选, 延迟绑定)
        self._resolver = resolver  # ArmorAssetResolver (可选)
        self._permission_hook = None
        self._switching = False    # 防重入
        self._pending_pack = None
        self._pending_clip = None  # 本次切换正在播放的召唤/召回片段注册名
        self._default_mode = default_mode
        if animator:
            self.bind_animator(animator)
        if packs_dir:
            self.load_packs(packs_dir)

    # ==================== 注册/列举 ====================

    def load_packs(self, packs_dir):
        """从目录批量加载 armor_packs/*.json."""
        packs_dir = Path(packs_dir)
        if not packs_dir.is_dir():
            logger.warning('packs_dir not found: %s', packs_dir)
            return 0
        count = 0
        for path in sorted(packs_dir.glob('*.json')):
            try:
                pack = ArmorPack(
                    json.loads(path.read_text(encoding='utf-8')), path)
                self.register(pack)
                count += 1
            except (ValueError, OSError) as exc:
                logger.error('skip invalid pack %s: %s', path.name, exc)
        logger.info('loaded %d armor packs from %s', count, packs_dir)
        # 管家 = 常驻中枢: 启动即驻守(静默, 不发信号不播动画)
        if self._current is None and self._default_mode in self._packs:
            self._current = self._packs[self._default_mode]
        return count

    def register(self, pack):
        if not isinstance(pack, ArmorPack):
            raise TypeError('expected ArmorPack')
        self._packs[pack.id] = pack
        return True

    def list_modes(self):
        """[(id, display_name), ...] 按注册顺序."""
        return [(p.id, p.display_name) for p in self._packs.values()]

    def get_pack(self, mode_id):
        return self._packs.get(mode_id)

    @property
    def current(self):
        return self._current

    @property
    def current_id(self):
        return self._current.id if self._current else None

    # ==================== 绑定 ====================

    def bind_animator(self, animator):
        """绑定 SpriteAnimator; 挂载 on_clip_finished 用于变身片段衔接."""
        self._animator = animator
        animator.on_clip_finished = self._on_clip_finished

    def bind_resolver(self, resolver):
        self._resolver = resolver

    def set_permission_hook(self, hook):
        """权限收敛钩子: hook(old_pack_or_None, new_pack) -> bool.
        返回 False 则拒绝切换."""
        self._permission_hook = hook

    # ==================== 切换 ====================

    def switch(self, mode_id):
        """切换到指定模式(召唤 / 召回).

        召唤(管家→员工): mode_changing → 播 new 的 summon_clip →
          mode_changed → new 的 idle_clip.
        召回(员工→管家): mode_changing → 倒放 old 的 summon_clip(素材零成本)
          → mode_changed → 管家 idle.
        无 summon_clip 时立即完成切换(素材未到位不阻塞).
        返回 True=切换已启动, False=被拒绝.
        """
        if self._switching:
            self.mode_rejected.emit(mode_id, 'switch_in_progress')
            return False
        pack = self._packs.get(mode_id)
        if pack is None:
            self.mode_rejected.emit(mode_id, 'unknown_mode')
            return False
        if self._current and self._current.id == mode_id:
            self.mode_rejected.emit(mode_id, 'already_active')
            return False
        # 权限收敛钩子
        if self._permission_hook:
            try:
                if not self._permission_hook(self._current, pack):
                    self.mode_rejected.emit(mode_id, 'permission_denied')
                    return False
            except Exception:
                logger.exception('permission_hook error')
                self.mode_rejected.emit(mode_id, 'permission_hook_error')
                return False
        old_pack = self._current
        old_id = self.current_id
        self._switching = True
        self._pending_pack = pack
        self.mode_changing.emit(old_id or '', mode_id)
        # resolver 切换解析目标
        if self._resolver:
            self._resolver.set_armor(pack.armor_id)
        # ---- 召回: 回管家时倒放旧甲的 summon 片段 ----
        if pack.is_hub and old_pack and old_pack.summon_clip and self._animator:
            clip_key = old_pack.summon_clip + self.RECALL_SUFFIX
            if self._ensure_clip(old_pack.summon_clip, clip_key, reversed=True):
                self._pending_clip = clip_key
                try:
                    self._animator.play_clip(clip_key, loop=False)
                    return True  # 等待 _on_clip_finished
                except Exception:
                    logger.exception('play recall clip failed, fallback immediate')
        # ---- 召唤: 播新甲的 summon 片段 ----
        if pack.summon_clip and self._animator:
            if self._ensure_clip(pack.summon_clip, pack.summon_clip):
                self._pending_clip = pack.summon_clip
                try:
                    self._animator.play_clip(pack.summon_clip, loop=False)
                    return True  # 等待 _on_clip_finished
                except Exception:
                    logger.exception('play summon_clip failed, fallback immediate')
        # 无召唤片段: 直接完成
        self._finish_switch()
        return True

    def _ensure_clip(self, clip_name, clip_key, reversed=False):
        """经 resolver 把召唤片段注册进 animator; 已注册或注册成功返回 True.

        注意只认 clip_key(倒放版是 <name>#rev, 与正放版是两个不同注册项);
        clip_name 的正放注册不能替代倒放注册, 否则真实 SpriteAnimator
        play_clip(#rev) 会因未注册而静默不播.
        """
        if self._animator is None:
            return False
        if clip_key in self._animator.clips:
            return True
        if self._resolver is None:
            return False
        try:
            return self._resolver.load_into(
                self._animator, clip_name, namespace='idle',
                clip_key=clip_key, reversed=reversed) is not None
        except Exception:
            logger.exception('ensure clip failed: %s', clip_name)
            return False

    def _on_clip_finished(self, clip_name):
        """SpriteAnimator.on_clip_finished 回调: 召唤/召回片段播完 -> 完成切换."""
        if not self._switching or not self._pending_pack:
            return
        if self._pending_clip and clip_name != self._pending_clip:
            return  # 不是本次切换的片段, 忽略
        self._pending_clip = None
        self._finish_switch()

    def _finish_switch(self):
        pack = self._pending_pack
        self._current = pack
        self._pending_pack = None
        self._pending_clip = None
        self._switching = False
        self.mode_changed.emit(pack.id)
        # idle clip(召回回管家时用管家自己的 idle)
        if pack.idle_clip and self._animator:
            try:
                self._animator.play_clip(pack.idle_clip, loop=True)
            except Exception:
                logger.exception('play idle_clip failed')

    # ==================== 便捷 ====================

    def permission_diff(self, old_pack, new_pack):
        """对比两个 pack 的权限快照差异(供收敛钩子决策)."""
        old_snap = old_pack.permission_snapshot() if old_pack else {
            f: [] if f != 'permissions' else {} for f in PERMISSION_FIELDS}
        new_snap = new_pack.permission_snapshot()
        diff = {}
        for field in PERMISSION_FIELDS:
            if old_snap.get(field) != new_snap.get(field):
                diff[field] = {'from': old_snap.get(field), 'to': new_snap.get(field)}
        return diff