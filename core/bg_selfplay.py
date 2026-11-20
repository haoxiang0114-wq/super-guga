"""Optional offline eight-seat laboratory; never imported by the pet runtime.

External HSBRSIM is loaded only from an explicit, revision-checked checkout.
This is a restricted simulator profile, NOT a current-live-game rules engine.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import time

ENGINE_REVISION = "d8de313a6927378e32ee923d0c5843fb0f19d9f4"
PROFILE = "hsrl2-rich-spell-position-v2"
KINDS = ("END", "BUY", "PLAY", "BUY_SPELL", "PLAY_SPELL", "SELL",
         "REFRESH", "FREEZE", "UPGRADE")
ACTION_DIM = len(KINDS)
STATE_DIM = 12
VALUE_DIM = 17
IDENTITY_DIM = 10
FEATURE_DIM = ACTION_DIM + STATE_DIM + VALUE_DIM + IDENTITY_DIM


def load_engine(path):
    root = Path(path).resolve()
    from core.bg_version import verify_same_patch
    version=verify_same_patch(root)
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=10
    ).strip()
    if revision != ENGINE_REVISION:
        raise ValueError(f"Engine revision mismatch: expected {ENGINE_REVISION}, got {revision}")
    changes = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD", "--", "hsrl2", "data"],
        text=True, timeout=10
    ).strip()
    if changes:
        raise ValueError("Engine code has unrecorded local changes; refusing to train")
    untracked = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "--", "hsrl2", "data"],
        text=True, timeout=10).strip()
    if untracked:
        raise ValueError('Engine has untracked rule or data files; refusing unrecorded training')
    manifest_path=root/'data/version-manifest.json'
    if not manifest_path.is_file():
        raise ValueError('Current-version data manifest is missing; refusing unverified data')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('patch')!=version['patch']:
        raise ValueError('Data manifest patch does not match engine data')
    sys.path.insert(0, str(root))
    import hsrl2
    if Path(hsrl2.__file__).resolve().parent != root / "hsrl2":
        raise ValueError("A different hsrl2 installation is already imported")
    from hsrl2.db import CardDB
    from hsrl2.game import Game
    from hsrl2.hero import Hero
    from hsrl2.minion import Minion
    from hsrl2.spell import Spell
    from hsrl2.tags import GameTag
    from hsrl2.scripts import REGISTRY

    class LabDB(CardDB):
        def pool_minions(self):
            return list(super().pool_minions())

    db = LabDB.load(root / "data")
    effect_words=("After", "Whenever", "Battlecry", "Deathrattle", "Avenge", "Spellcraft",
                  "end of", "start of", "Rally", "Activate", "Choose One")
    effect_cards=[d for d in db.pool_minions()+db.pool_spells()
                  if any(word in (d.text or '') for word in effect_words)]
    missing=[d.id for d in effect_cards if d.id not in REGISTRY]
    coverage=dict(pool_minions=len(db.pool_minions()),pool_spells=len(db.pool_spells()),
                  effect_cards=len(effect_cards),scripted=len(effect_cards)-len(missing),
                  unsupported_effects=len(missing),missing_ids=missing[:100],
                  mode='current-data with vanilla fallback for unscripted effects')
    return dict(Game=Game, Hero=Hero, Minion=Minion, Spell=Spell, Tag=GameTag, db=db,
                revision=revision, patch=version['patch'], root=root, coverage=coverage)


@dataclass(frozen=True)
class Choice:
    kind: str
    index: int = -1
    position: int = -1


def placement_from_groups(groups):
    """Last surviving group first; same-round eliminations share average rank."""
    ranks = {}
    next_rank = 1
    for group in groups:
        rank = next_rank + (len(group) - 1) / 2
        for seat in group:
            ranks[seat] = rank
        next_rank += len(group)
    if set(ranks) != set(range(8)):
        raise ValueError("Placement groups must contain each of eight seats exactly once")
    if sum(len(g) for g in groups) != 8:
        raise ValueError("Duplicate seat in placement groups")
    return [ranks[i] for i in range(8)]


def top4_credit(placements, seat):
    """Fractional top-four credit when a same-round tie crosses fourth place."""
    rank = placements[seat]
    count = placements.count(rank)
    first = rank - (count - 1) / 2
    return max(0., min(float(count), 5 - first)) / count


class Arena:
    def __init__(self, engine, *, turn_limit=45, action_limit=60, seconds=120):
        self.e = engine
        self.turn_limit = turn_limit
        self.action_limit = action_limit
        self.seconds = seconds

    def actions(self, game, hero):
        E, T = self.e, self.e["Tag"]
        actions = [Choice("END")]
        if hero.get(T.TURN_SKIPPED, False):
            return actions
        if not hero.hand_full():
            for i, card in enumerate(hero.tavern):
                cost = card.get(T.COST, 3 if isinstance(card, E["Minion"]) else 1)
                if hero.gold < cost:
                    continue
                if isinstance(card, E["Minion"]):
                    actions.append(Choice("BUY", i))
                elif isinstance(card, E["Spell"]):
                    card_def = game.db.get(card.card_id)
                    if (card_def and (card_def.unplayable or
                                      (card_def.health_cost and hero.health + hero.armor <= cost))):
                        continue
                    actions.append(Choice("BUY_SPELL", i))
        actions += [Choice("SELL", i) for i in range(len(hero.board))]
        for i, card in enumerate(hero.hand):
            d = game.db.get(card.card_id)
            if card.has(T.LOCKED_IN_HAND) or (d and d.unplayable):
                continue
            if isinstance(card, E["Minion"]) and not hero.board_full():
                positions = {0, len(hero.board)}
                for position in sorted(positions):
                    actions.append(Choice("PLAY", i, position))
            elif isinstance(card, E["Spell"]):
                targets = self.spell_targets(game, hero, card)
                if getattr(card.scripts, "needs_target", False) and not targets:
                    continue
                actions.append(Choice("PLAY_SPELL", i))
        if hero.gold >= 1 or hero.get(T.FREE_REFRESH_REMAINING, 0) > 0:
            actions.append(Choice("REFRESH"))
        if hero.tavern and not hero.has(T.FROZEN):
            actions.append(Choice("FREEZE"))
        if hero.tavern_tier < 6 and hero.gold >= max(0, hero.upgrade_cost):
            actions.append(Choice("UPGRADE"))
        return actions

    @staticmethod
    def spell_targets(game, hero, spell):
        script = getattr(spell, "scripts", None)
        candidate_fn = getattr(script, "target_candidates", None)
        if callable(candidate_fn):
            return [x for x in candidate_fn(spell, game)
                    if getattr(x, "dead", False) is not True]
        return [m for m in hero.board if not m.dead]

    def features(self, game, hero, actions):
        T = self.e["Tag"]
        strength = lambda m: (getattr(m, "atk", 0) + getattr(m, "health", 0)
                              + (5 if m.has(T.DIVINE_SHIELD) else 0))
        weak = min((strength(m) for m in hero.board), default=0)
        best = max((strength(m) for m in hero.tavern if isinstance(m, self.e["Minion"])), default=0)
        races = [m.race for m in hero.board if m.race.name not in ("ALL", "NONE")]
        dominant = max(set(races), key=races.count) if races else None
        dominant_share = races.count(dominant)/len(races) if dominant is not None else 0.
        shop_minions = sum(isinstance(x, self.e["Minion"]) for x in hero.tavern)
        shop_spells = sum(isinstance(x, self.e["Spell"]) for x in hero.tavern)
        state = [game.turn / 20, hero.gold / 10, (hero.health + hero.armor) / 40,
                 hero.tavern_tier / 6, hero.upgrade_cost / 10, len(hero.board) / 7,
                 len(hero.hand) / 10, sum(strength(m) for m in hero.board) / 200,
                 shop_minions / 7, shop_spells / 7, dominant_share,
                 float(hero.has(T.FROZEN))]
        keyword_tags = (T.DIVINE_SHIELD, T.TAUNT, T.REBORN, T.WINDFURY,
                        T.VENOMOUS, T.CLEAVE, T.BATTLECRY, T.DEATHRATTLE)
        rows = []
        for action in actions:
            card = None
            if action.kind in ("BUY", "BUY_SPELL", "SELL", "PLAY", "PLAY_SPELL"):
                zone = {"BUY": hero.tavern, "BUY_SPELL": hero.tavern,
                        "SELL": hero.board, "PLAY": hero.hand,
                        "PLAY_SPELL": hero.hand}[action.kind]
                card = zone[action.index]
            values = [0.] * VALUE_DIM
            if card is not None:
                game.db.get(card.card_id)
                values = [getattr(card, "atk", 0) / 30, getattr(card, "health", 0) / 30,
                          getattr(card, "tech_level", 1) / 6,
                          *[float(card.has(tag)) for tag in keyword_tags],
                          sum(m.card_id == card.card_id for m in hero.board + hero.hand) / 3,
                          (strength(card) - weak) / 30, (best - strength(card)) / 30,
                          (action.position / 7 if action.position >= 0 else 0.),
                          float(dominant is not None and getattr(card, "race", None) is not None and
                                getattr(card, "race").name in ("ALL", dominant.name)),
                          (getattr(card, "tech_level", 1) - hero.tavern_tier) / 6]
            bits = hashlib.blake2b(str(card.card_id if card is not None else action.kind).encode(),
                                   digest_size=8).digest()
            identity = [float((int.from_bytes(bits, 'big') >> i) & 1) for i in range(IDENTITY_DIM)]
            rows.append([float(action.kind == k) for k in KINDS] + state + values + identity)
        assert all(len(row) == FEATURE_DIM for row in rows)
        return rows

    def heuristic(self, game, hero, actions):
        T = self.e["Tag"]
        rows = self.features(game, hero, actions)
        scores = []
        for action, row in zip(actions, rows):
            stat = (row[ACTION_DIM+STATE_DIM] + row[ACTION_DIM+STATE_DIM+1]) * 30 \
                   + row[ACTION_DIM+STATE_DIM+3] * 5
            if action.kind == "PLAY":
                score = 100 + stat
            elif action.kind == "BUY":
                score = (30 + stat) if len(hero.board) + len(hero.hand) < 7 else -20
            elif action.kind == "PLAY_SPELL":
                score = 55 + row[ACTION_DIM+STATE_DIM+2] * 8
            elif action.kind == "BUY_SPELL":
                score = 28 + row[ACTION_DIM+STATE_DIM+2] * 7
            elif action.kind == "FREEZE":
                valuable = any(isinstance(x, self.e["Minion"]) and x.get(T.COST, 3) > hero.gold
                               and getattr(x, "tech_level", 1) >= hero.tavern_tier
                               for x in hero.tavern)
                score = 8 if valuable else -12
            elif action.kind == "UPGRADE":
                expected = min(6, 1 + game.turn // 2)
                score = 75 if hero.tavern_tier < expected else -15
            elif action.kind == "SELL":
                copies = row[ACTION_DIM+STATE_DIM+11]
                score = 40 + copies * 30 if (len(hero.board) == 7 and hero.gold >= 2
                                                   and copies > .15) else -100
            elif action.kind == "REFRESH":
                score = 5 if hero.gold >= 4 else -10
            else:
                score = 0
            scores.append(score)
        return max(range(len(actions)), key=lambda i: scores[i])

    def execute(self, game, hero, action):
        if action.kind == "END":
            return
        if action.kind == "BUY":
            ok = game.buy_from_tavern(hero, hero.tavern[action.index])
        elif action.kind == "BUY_SPELL":
            ok = game.buy_from_tavern(hero, hero.tavern[action.index])
        elif action.kind == "SELL":
            ok = game.sell_minion(hero, hero.board[action.index])
        elif action.kind == "PLAY":
            ok = game.play_minion(hero, hero.hand[action.index], position=action.position)
        elif action.kind == "PLAY_SPELL":
            spell = hero.hand[action.index]
            targets = self.spell_targets(game, hero, spell)
            ok = game.play_spell(hero, spell, target=targets[0] if targets else None)
        elif action.kind == "UPGRADE":
            ok = game.upgrade_tavern(hero)
        elif action.kind == "FREEZE":
            game.freeze_tavern(hero)
            ok = True
        elif action.kind == "REFRESH":
            game.refresh_tavern(hero)
            ok = True
        else:
            raise ValueError(action.kind)
        if not ok:
            raise RuntimeError(f"Masked legal action rejected: {action}")
        game.queue.resolve(game)
        self.resolve_choices(game)

    @staticmethod
    def resolve_choices(game):
        # Fixed, explicitly recorded helper. Target/discover policy is not learned yet.
        for _ in range(40):
            if not game.pending_choices:
                return
            choice = game.pending_choices.pop(0)
            if not choice.options:
                raise RuntimeError("Pending choice has no options")
            choice.choose(0)
            game.queue.resolve(game)
        raise RuntimeError("Pending-choice chain exceeded laboratory budget")

    def run(self, policies, seed, *, collect=False):
        if len(policies) != 8:
            raise ValueError("Exactly eight seat policies required")
        random.seed(seed)
        heroes = [self.e["Hero"](f"LAB_{i}", f"Seat{i}", armor=10) for i in range(8)]
        game = self.e["Game"](heroes.copy(), self.e["db"], seed=seed)
        game.choice_policy = "first"  # Explicit fixed helper; not learned targeting/discover.
        game.start_game()
        self.resolve_choices(game)
        started = time.monotonic()
        trajectories = [[] for _ in range(8)]
        eliminated_groups, turns, combat_phases, capped = [], [], 0, 0
        for _ in range(self.turn_limit):
            if time.monotonic() - started > self.seconds:
                raise TimeoutError("Game exceeded wall-time budget")
            for seat, hero in enumerate(heroes):
                if hero not in game.heroes or not hero.is_alive:
                    continue
                for step in range(self.action_limit):
                    actions = self.actions(game, hero)
                    rows = self.features(game, hero, actions)
                    teacher = self.heuristic(game, hero, actions)
                    policy = policies[seat]
                    selected = (policy.choose_context(game, hero, actions, rows, teacher)
                                if hasattr(policy, 'choose_context') else policy(rows, teacher))
                    if not isinstance(selected, int) or not 0 <= selected < len(actions):
                        raise ValueError("Policy returned invalid candidate index")
                    if collect:
                        trajectories[seat].append(dict(x=rows, action=selected, teacher=teacher,
                                                       turn=game.turn, kind=actions[selected].kind,
                                                       teacher_kind=actions[teacher].kind,
                                                       decision=dict(getattr(policy,'last_decision',{}))))
                    action = actions[selected]
                    self.execute(game, hero, action)
                    if action.kind == "END":
                        break
                else:
                    capped += 1
            turn = game.turn
            before = set(game.heroes)
            game.end_recruit_phase()  # Exceptions propagate; never skip broken combat.
            self.resolve_choices(game)
            combat_phases += 1
            eliminated = [i for i, h in enumerate(heroes) if h in before and h not in game.heroes]
            if eliminated:
                eliminated_groups.append(eliminated)
            turns.append(dict(turn=turn, alive=len(game.heroes), eliminated=eliminated,
                              health=[h.health + h.armor for h in heroes]))
            if game.finished:
                break
        finished = game.finished and len(game.heroes) <= 1
        ranks = None
        if finished:
            survivor = [i for i, h in enumerate(heroes) if h in game.heroes]
            groups = ([survivor] if survivor else []) + eliminated_groups[::-1]
            ranks = placement_from_groups(groups)
        return dict(profile=PROFILE, engine_revision=self.e["revision"], seed=seed,
                    finished=finished, turns=turns, combat_phases=combat_phases,
                    placements=ranks, action_limit_hits=capped,
                    trajectories=trajectories, elapsed_seconds=round(time.monotonic()-started, 3))


def teacher_policy(rows, teacher):
    return teacher


def random_policy(rng):
    return lambda rows, teacher: rng.randrange(len(rows))


def save_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
