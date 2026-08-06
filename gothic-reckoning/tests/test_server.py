"""
TDD: server layer tests (red before server.py satisfied them).
"""
import pytest
from fastapi.testclient import TestClient

from src.server import app, _STATE


@pytest.fixture(autouse=True)
def fresh_state():
    _STATE["game"] = None
    yield
    _STATE["game"] = None


client = TestClient(app)


def test_index_serves_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "Gothic Reckoning" in r.text
    assert "manifest.json" in r.text
    assert "game.js" in r.text


def test_static_pwa_assets_are_served():
    for path, content_type in (
        ("/manifest.json", "application/manifest+json"),
        ("/sw.js", "application/javascript"),
        ("/game.js", "application/javascript"),
        ("/privacy.html", "text/html"),
        ("/icon-512.png", "image/png"),
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert content_type in r.headers["content-type"], path
    assert client.get("/manifest.json").json()["display"] == "standalone"


def test_new_game_returns_12_players():
    r = client.post("/api/game/new", json={})
    assert r.status_code == 200
    d = r.json()
    assert len(d["game"]["players"]) == 12
    assert d["phase"] == "NIGHT"


def test_state_404_before_new_game():
    r = client.get("/api/game/state")
    assert r.status_code == 404


def test_advance_moves_phase():
    client.post("/api/game/new", json={})
    r = client.post("/api/game/advance")
    assert r.status_code == 200
    assert r.json()["phase"] == "DAY"
    assert r.json()["night_victim"] is not None


def test_vote_records_and_resolves():
    client.post("/api/game/new", json={})
    client.post("/api/game/advance")  # NIGHT -> DAY
    client.post("/api/game/advance")  # DAY -> VOTE
    s = client.get("/api/game/state").json()["game"]
    voters = [i for i, p in enumerate(s["players"]) if p["alive"]][:4]
    target = voters[-1]
    for voter in voters[:3]:
        r = client.post("/api/game/vote", json={"voter_index": voter, "target_index": target})
        assert r.status_code == 200
    r = client.post("/api/game/advance")  # resolves vote
    d = r.json()
    assert d["lynched"]["name"] == s["players"][target]["name"]
    assert d["game"]["players"][target]["alive"] is False


def test_dead_cannot_vote():
    client.post("/api/game/new", json={})
    client.post("/api/game/advance")
    s = client.get("/api/game/state").json()["game"]
    dead = [i for i, p in enumerate(s["players"]) if not p["alive"]]
    alive = [i for i, p in enumerate(s["players"]) if p["alive"]]
    r = client.post("/api/game/vote", json={"voter_index": dead[0], "target_index": alive[0]})
    assert r.status_code == 400
    assert "dead" in r.json()["error"]


def test_reset_clears_state():
    client.post("/api/game/new", json={})
    client.post("/api/game/reset")
    r = client.get("/api/game/state")
    assert r.status_code == 404


def test_two_browser_sessions_are_isolated():
    first = {"X-Session-Id": "first-table"}
    second = {"X-Session-Id": "second-table"}
    client.post("/api/game/new", json={"seed": 1}, headers=first)
    client.post("/api/game/new", json={"seed": 2}, headers=second)
    client.post("/api/game/advance", headers=first)
    one = client.get("/api/game/state", headers=first).json()["game"]
    two = client.get("/api/game/state", headers=second).json()["game"]
    assert one["night_count"] == 1
    assert two["night_count"] == 0
    assert sum(not player["alive"] for player in one["players"]) == 1
    assert sum(not player["alive"] for player in two["players"]) == 0
