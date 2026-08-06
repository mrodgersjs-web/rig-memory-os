"""Gothic Reckoning — core werewolf engine.

Pure game logic, zero I/O, fully deterministic via seed. Written to satisfy
tests/test_engine.py (TDD). 12 players by default: 1 seer, 1 witch, 1 hunter,
1 guard, ~1/3 werewolves, rest villagers.

Phases cycle: NIGHT -> DAY -> VOTE -> DUSK -> NIGHT ...
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum, auto


class Role(Enum):
    SEER = auto()
    WITCH = auto()
    HUNTER = auto()
    GUARD = auto()
    WEREWOLF = auto()
    VILLAGER = auto()


class Phase(Enum):
    NIGHT = auto()
    DAY = auto()
    VOTE = auto()
    DUSK = auto()


@dataclass
class Player:
    name: str
    role: Role
    alive: bool = True


@dataclass
class Game:
    player_names: list[str]
    seed: int = 42
    players: list[Player] = field(init=False)
    phase: Phase = field(default=Phase.NIGHT, init=False)
    night_count: int = field(default=0, init=False)
    winner: str | None = field(default=None, init=False)
    game_over: bool = field(default=False, init=False)
    _seer_knowledge: dict = field(default_factory=dict, init=False, repr=False)
    _witch_save_target: Player | None = field(default=None, init=False, repr=False)
    _guard_target: Player | None = field(default=None, init=False, repr=False)
    _witch_used_save: bool = field(default=False, init=False, repr=False)
    _witch_used_poison: bool = field(default=False, init=False, repr=False)
    _votes: dict = field(default_factory=dict, init=False, repr=False)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        n = len(self.player_names)
        roles = self._deal_roles(n)
        self._rng.shuffle(roles)
        self.players = [Player(name=n, role=r) for n, r in zip(self.player_names, roles)]

    # ---- setup ----
    def _deal_roles(self, n: int) -> list[Role]:
        n_wolves = max(3, min(5, round(n / 3)))
        special = [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD]
        wolves = [Role.WEREWOLF] * n_wolves
        villagers = [Role.VILLAGER] * (n - n_wolves - len(special))
        return special + wolves + villagers

    # ---- helpers ----
    @property
    def alive(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def get_role(self, role: Role) -> Player | None:
        for p in self.players:
            if p.role == role:
                return p
        return None

    def wolves(self, alive_only: bool = True) -> list[Player]:
        pool = self.alive if alive_only else self.players
        return [p for p in pool if p.role == Role.WEREWOLF]

    # ---- night actions ----
    def night_seer_check(self, seer: Player, target: Player) -> None:
        if not seer.alive:
            return
        self._seer_knowledge.setdefault(seer.name, {})[target.name] = target.role

    def seer_knows(self, seer: Player, target: Player):
        return self._seer_knowledge.get(seer.name, {}).get(target.name)

    def witch_save(self, witch: Player, target: Player) -> None:
        if witch.alive and not self._witch_used_save:
            self._witch_save_target = target
            self._witch_used_save = True

    def witch_poison(self, witch: Player, target: Player) -> None:
        if witch.alive and not self._witch_used_poison:
            target.alive = False
            self._witch_used_poison = True

    def guard_protect(self, guard: Player, target: Player) -> None:
        if guard.alive:
            self._guard_target = target

    def hunter_revenge(self, hunter: Player, target: Player) -> None:
        if hunter.role == Role.HUNTER:
            target.alive = False

    # ---- phase resolution ----
    def advance(self) -> None:
        """Resolve current phase and move to the next."""
        if self.phase == Phase.NIGHT:
            self._resolve_night()
            self.phase = Phase.DAY
        elif self.phase == Phase.DAY:
            self.phase = Phase.VOTE
        elif self.phase == Phase.VOTE:
            self.resolve_vote()
            self.phase = Phase.DUSK
        elif self.phase == Phase.DUSK:
            self.phase = Phase.NIGHT
        self.check_win()

    def _resolve_night(self) -> None:
        wolves = self.wolves(alive_only=True)
        if not wolves:
            self.night_count += 1
            return
        victims = [p for p in self.alive if p.role != Role.WEREWOLF]
        if not victims:
            self.night_count += 1
            return
        victim = self._rng.choice(victims)
        protected = victim is self._guard_target or victim is self._witch_save_target
        if not protected:
            victim.alive = False
        self.night_count += 1
        self._guard_target = None
        self._witch_save_target = None

    # ---- day/vote ----
    def vote(self, voter: Player, target: Player) -> None:
        if not voter.alive:
            raise ValueError(f"{voter.name} is dead and cannot vote")
        if self.phase != Phase.VOTE:
            self.phase = Phase.VOTE
        self._votes[voter.name] = target.name

    def resolve_vote(self) -> None:
        if not self._votes:
            return
        tally: dict[str, int] = {}
        for t in self._votes.values():
            tally[t] = tally.get(t, 0) + 1
        top = max(tally.values())
        leaders = [name for name, cnt in tally.items() if cnt == top]
        if len(leaders) == 1:
            for p in self.players:
                if p.name == leaders[0]:
                    p.alive = False
                    break
        self._votes.clear()

    # ---- win check ----
    def check_win(self) -> None:
        wolves_alive = len(self.wolves(alive_only=True))
        others_alive = len([p for p in self.alive if p.role != Role.WEREWOLF])
        if wolves_alive == 0:
            self.winner = "VILLAGE"
            self.game_over = True
        elif wolves_alive >= others_alive:
            self.winner = "WOLVES"
            self.game_over = True

    # ---- debug/serialize ----
    def to_dict(self) -> dict:
        return {
            "phase": self.phase.name,
            "night_count": self.night_count,
            "winner": self.winner,
            "game_over": self.game_over,
            "players": [
                {"name": p.name, "role": p.role.name, "alive": p.alive}
                for p in self.players
            ],
        }
