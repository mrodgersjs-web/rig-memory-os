"""Gothic Reckoning — FastAPI server bridging engine.py <-> 90s UI.

Exposes: / (game), /manifest.json, /sw.js, /api/game/new, /api/game/state,
/api/game/advance, plus vote endpoint for the day phase.
TDD: tests/test_server.py must pass.
"""
from __future__ import annotations

import random
from pathlib import Path

from fastapi import FastAPI, Header
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from gothic.engine import Game, Phase, Role

ROOT = Path(__file__).parent.resolve()
PUBLIC = ROOT.parent / "public"
app = FastAPI(title="Gothic Reckoning")

_STATE = {"game": None, "games": {}}


class NewGameIn(BaseModel):
    players: list[str] | None = None
    seed: int | None = None


class VoteIn(BaseModel):
    voter_index: int
    target_index: int


def serialize_game(g: Game) -> dict:
    data = g.to_dict()
    return {
        "players": data["players"],
        "phase": data["phase"],
        "night_count": data["night_count"],
        "winner": data["winner"],
        "game_over": data["game_over"],
    }

def _get_game(session_id: str | None) -> Game | None:
    """Return the caller's table; retain the default table for CLI/tests."""
    if session_id:
        return _STATE["games"].get(session_id)
    return _STATE.get("game")


def _set_game(session_id: str | None, game: Game | None) -> None:
    if session_id:
        if game is None:
            _STATE["games"].pop(session_id, None)
        else:
            _STATE["games"][session_id] = game
    else:
        _STATE["game"] = game


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((PUBLIC / "index.html").read_text())


@app.get("/manifest.json")
async def manifest():
    return FileResponse(PUBLIC / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def sw():
    return FileResponse(PUBLIC / "sw.js", media_type="application/javascript")

@app.get("/game.js")
async def game_js():
    return FileResponse(PUBLIC / "game.js", media_type="application/javascript")

@app.get("/privacy.html", response_class=HTMLResponse)
async def privacy():
    return HTMLResponse((PUBLIC / "privacy.html").read_text())



@app.get("/icon-512.png")
async def icon():
    return FileResponse(PUBLIC / "icon-512.png", media_type="image/png")


@app.post("/api/game/new")
async def new_game(
    body: NewGameIn, x_session_id: str | None = Header(default=None)
):
    names = body.players or [f"Soul {i+1}" for i in range(12)]
    seed = body.seed if body.seed is not None else random.randint(1, 99999)
    g = Game(player_names=names[:12], seed=seed)
    _set_game(x_session_id, g)
    return {"game": serialize_game(g), "phase": "NIGHT"}


@app.get("/api/game/state")
async def get_state(x_session_id: str | None = Header(default=None)):
    g = _get_game(x_session_id)
    if not g:
        return JSONResponse({"error": "no active game"}, status_code=404)
    return {"game": serialize_game(g), "phase": g.phase.name}


@app.post("/api/game/vote")
async def cast_vote(
    body: VoteIn, x_session_id: str | None = Header(default=None)
):
    g = _get_game(x_session_id)
    if not g:
        return JSONResponse({"error": "no active game"}, status_code=404)
    try:
        g.vote(g.players[body.voter_index], g.players[body.target_index])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"game": serialize_game(g), "phase": g.phase.name}


@app.post("/api/game/advance")
async def advance(x_session_id: str | None = Header(default=None)):
    g = _get_game(x_session_id)
    if not g:
        return JSONResponse({"error": "no active game"}, status_code=404)
    prev_alive = {p.name: p.alive for p in g.players}
    prev_phase = g.phase
    g.advance()
    result = {"game": serialize_game(g), "phase": g.phase.name, "lynched": None, "winner": g.winner}
    # detect new deaths
    new_deaths = [p for p in g.players if p.name in prev_alive and prev_alive[p.name] and not p.alive]
    if new_deaths and prev_phase == Phase.NIGHT:
        for d in new_deaths:
            d_role = d.role
            result["night_victim"] = {"name": d.name, "role": d_role.name}
    if g.winner:
        result["winner"] = g.winner
    # if vote was just resolved, identify lynched
    if prev_phase == Phase.VOTE and new_deaths:
        result["lynched"] = {"name": new_deaths[-1].name, "role": new_deaths[-1].role.name}
    return result


@app.post("/api/game/reset")
async def reset(x_session_id: str | None = Header(default=None)):
    _set_game(x_session_id, None)
    return {"ok": True}
