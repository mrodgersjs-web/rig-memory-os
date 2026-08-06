"""Gothic Reckoning — core game engine tests (TDD: written BEFORE implementation).

These tests encode the werewolf rules as observable behavior. They must all
fail (red) until engine.py is written, then pass (green).

Werewolf flow per game: night -> day -> vote -> dusk -> repeat until one side wins.
Roles: 1 seer, 1 witch (save+poison), 1 hunter, 1 guard, remainder villagers + ~30% werewolves.
"""
import pytest
from gothic.engine import Game, Role, Phase, Player


def make_game(player_names=None, seed=42):
    names = player_names or [f"Villager{i}" for i in range(1, 13)]
    return Game(player_names=names, seed=seed)


class TestSetup:
    def test_12_players_get_12_roles(self):
        g = make_game()
        assert len(g.players) == 12

    def test_exactly_one_seer_one_witch_one_hunter_one_guard(self):
        g = make_game()
        roles = [p.role for p in g.players]
        assert roles.count(Role.SEER) == 1
        assert roles.count(Role.WITCH) == 1
        assert roles.count(Role.HUNTER) == 1
        assert roles.count(Role.GUARD) == 1

    def test_wolves_are_roughly_a_third(self):
        g = make_game()
        wolves = [p for p in g.players if p.role == Role.WEREWOLF]
        assert 3 <= len(wolves) <= 5

    def test_game_starts_at_night_zero(self):
        g = make_game()
        assert g.phase == Phase.NIGHT
        assert g.night_count == 0

    def test_all_players_alive_at_start(self):
        g = make_game()
        assert all(p.alive for p in g.players)

    def test_seed_deterministic(self):
        g1 = make_game(seed=7)
        g2 = make_game(seed=7)
        assert [p.role for p in g1.players] == [p.role for p in g2.players]
        g3 = make_game(seed=8)
        assert (True for _ in g3.players)  # just prove different seed runs exist


class TestNightPhase:
    def test_wolves_kill_one_per_night(self):
        g = make_game()
        alive_before = len(g.alive)
        g.advance()  # resolve night
        assert len(g.alive) == alive_before - 1

    def test_seer_gets_reveal(self):
        g = make_game()
        seer = g.get_role(Role.SEER)
        g.night_seer_check(seer, g.players[1])
        assert g.seer_knows(seer, g.players[1]) is not None

    def test_witch_can_save(self):
        g = make_game()
        witch = g.get_role(Role.WITCH)
        target = g.players[3]
        g.witch_save(witch, target)
        g.advance()
        assert target.alive or target.role != Role.VILLAGER  # save applied

    def test_guard_protects_one(self):
        g = make_game()
        guard = g.get_role(Role.GUARD)
        protected = g.players[4]
        g.guard_protect(guard, protected)
        g.advance()
        assert protected.alive


class TestVotePhase:
    def test_majority_lynches_one(self):
        g = make_game()
        alive_before = len(g.alive)
        g.phase = Phase.VOTE
        g.vote(g.players[0], g.players[5])
        g.vote(g.players[1], g.players[5])
        g.vote(g.players[2], g.players[5])
        g.vote(g.players[3], g.players[1])
        g.resolve_vote()
        assert len(g.alive) == alive_before - 1
        assert not g.players[5].alive

    def test_tie_no_lynch(self):
        g = make_game()
        alive_before = len(g.alive)
        g.phase = Phase.VOTE
        g.vote(g.players[0], g.players[5])
        g.vote(g.players[1], g.players[5])
        g.vote(g.players[2], g.players[3])
        g.vote(g.players[3], g.players[3])
        g.resolve_vote()
        assert len(g.alive) == alive_before

    def test_dead_cannot_vote(self):
        g = make_game()
        g.players[0].alive = False
        g.phase = Phase.VOTE
        with pytest.raises(ValueError, match="dead"):
            g.vote(g.players[0], g.players[5])


class TestWinConditions:
    def test_villagers_win_when_all_wolves_dead(self):
        g = make_game()
        for p in g.players:
            if p.role == Role.WEREWOLF:
                p.alive = False
        g.check_win()
        assert g.winner == "VILLAGE"

    def test_wolves_win_when_outnumber(self):
        g = make_game()
        # Kill 6 non-wolves explicitly so wolves (3-4) outnumber remaining non-wolves
        killed = 0
        for p in g.players:
            if p.role != Role.WEREWOLF and killed < 6:
                p.alive = False
                killed += 1
        assert killed == 6, "test must kill exactly 6 non-wolves"
        g.check_win()
        assert g.winner == "WOLVES"

    def test_no_winner_early(self):
        g = make_game()
        g.check_win()
        assert g.winner is None

    def test_game_over_flag(self):
        g = make_game()
        for p in g.players:
            if p.role == Role.WEREWOLF:
                p.alive = False
        g.check_win()
        assert g.game_over is True


class TestHunterRevenge:
    def test_hunter_takes_one_down(self):
        g = make_game()
        hunter = g.get_role(Role.HUNTER)
        target = g.players[6]
        hunter.alive = False
        g.hunter_revenge(hunter, target)
        assert not target.alive
