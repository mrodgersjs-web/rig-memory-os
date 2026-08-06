# Gothic Reckoning — Primary-Source Functional Research

Research date: 2026-08-05. Scope: the existing OpenViking Werewolf game only. This document cites the first-party implementation and game souls, not secondary descriptions.

## Product truth

Gothic Reckoning is a spectator-first AI Werewolf game with an optional human seat. The existing product is not a conventional local single-player game: OpenViking bots execute the rules through a god/referee soul and player souls; FastAPI exposes operational state and control surfaces. [Source: `werewolf_server.py:48-76,568-581`; `SOUL-god.md`; `SOUL-player.md`]

## Modes

- `all_agents` is the default: every seat is an AI agent.
- `human_player` adds one human-controlled seat with private chat.

[Source: `README.md`; `start_werewolf_demo.py` game-mode handling]

## Players and roles

The supported table size is 6–12. The referee recommends symmetric groups: 6 players = 2 wolves / 2 villagers / 2 specials; 9 = 3 / 3 / 3; 12 = 4 / 4 / 4. [Source: `SOUL-god.md`, configuration section]

Implemented role family:

| Side | Roles |
| --- | --- |
| Wolves | Werewolf, White Wolf King, Wolf Cub |
| Village | Villager, Seer, Witch, Hunter, Guard, Idiot |

[Source: `SOUL-god.md`; `SOUL-player.md`; `werewolf_server.py:298` leaderboard variants]

## Game state and phase contract

The authoritative state lives in `GAME_RECORD.md`; each player has a private `GAME.md`. The FastAPI layer parses those files into its operational `GameState`, which tracks running status, router task, auto-run, human-mode status, and session identifiers. [Source: `SOUL-god.md` record-update rules; `werewolf_server.py:48-76,568-581`]

1. **Init** — identities are assigned privately and the public record is initialized.
2. **Night** — the referee advances players in a fixed order; actions are private; kills, potions, protection, and seer checks are settled by the god.
3. **Day** — on day one, sheriff election happens before the prior-night deaths are announced; then speeches and voting occur. The sheriff has a 1.5× vote weight.
4. **Settlement** — exile, hunter retaliation, final words, and win check settle before the next phase.

The first-night delayed announcement is an intentional anti-leak rule. [Source: `SOUL-god.md`, sections I–III; `werewolf_server.py:218-581` parsing/state orchestration]

## Win conditions

Wolves win after eliminating all good players or all special-role players. The good side wins after eliminating every wolf. [Source: `SOUL-god.md`, win-condition section]

## Existing surface and API

The existing PWA already has the following user-facing surfaces: arena seats and phase display, chat/log, roster, voting, leaderboard, replay viewer, human input, and start/continue/auto/stop/restart mode controls. [Source: `werewolfUI.html:143-600` CSS/JS, arena/chat/roster/vote/leaderboard/replay markup]

The source server owns these operations:

| Endpoint family | Responsibility |
| --- | --- |
| `/api/status` | Parse and report live game state |
| `/api/start`, `/api/continue`, `/api/stop`, `/api/auto-run` | Control an active game |
| `/api/restart` | Start a fresh session with a game mode |
| `/api/human/send` | Route private or public human text |
| leaderboard/replay endpoints | Read archived workspace records |

[Source: `werewolf_server.py:1706-1772`; `README.md` button/API mapping]

## Architecture decision for the mobile app

The mobile client must remain a **presentation and control layer**, not reimplement the OpenViking referee as a second game engine. It will render the source server's state, surface the same controls, and package the responsive PWA using Capacitor. The local deterministic engine is retained only for offline tutorial/demo mode. This avoids a divergent rules implementation. [Inference grounded in the source boundary above]
