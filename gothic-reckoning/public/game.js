// Gothic Reckoning PWA bootstrap and offline-game fallback.
//
// Production installs use the remote OpenViking API. A native or PWA install
// without a reachable API gets an equivalent deterministic tutorial table so
// the app remains playable rather than showing a blank WebView.

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((error) => {
      console.warn('Service worker unavailable:', error);
    });
  });
}

const OfflineTable = (() => {
  let game = null;
  let votes = {};

  const rolesFor = (names, seed) => {
    const roles = ['SEER', 'WITCH', 'HUNTER', 'GUARD', 'WEREWOLF', 'WEREWOLF', 'WEREWOLF', 'WEREWOLF', 'VILLAGER', 'VILLAGER', 'VILLAGER', 'VILLAGER'];
    let n = seed >>> 0;
    for (let i = roles.length - 1; i > 0; i--) {
      n = (n * 1664525 + 1013904223) >>> 0;
      const j = n % (i + 1);
      [roles[i], roles[j]] = [roles[j], roles[i]];
    }
    return names.map((name, index) => ({ name, role: roles[index], alive: true }));
  };

  const response = (data) => new Response(JSON.stringify(data), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });

  const snapshot = () => ({
    players: game.players.map((p) => ({ ...p })),
    phase: game.phase,
    night_count: game.night_count,
    winner: game.winner,
    game_over: Boolean(game.winner),
  });

  const winner = () => {
    const wolves = game.players.filter((p) => p.alive && p.role === 'WEREWOLF').length;
    const good = game.players.filter((p) => p.alive && p.role !== 'WEREWOLF').length;
    return wolves === 0 ? 'VILLAGE' : wolves >= good ? 'WOLVES' : null;
  };

  const newGame = (body) => {
    const names = body.players || Array.from({ length: 12 }, (_, i) => `Soul ${i + 1}`);
    game = { players: rolesFor(names, body.seed || 42), phase: 'NIGHT', night_count: 0, winner: null, random: body.seed || 42 };
    votes = {};
    return response({ game: snapshot(), phase: game.phase, offline: true });
  };

  const advance = () => {
    if (!game) return response({ error: 'no active game' });
    const result = { game: null, phase: game.phase, lynched: null, night_victim: null, winner: null, offline: true };
    if (game.phase === 'NIGHT') {
      const candidates = game.players.filter((p) => p.alive && p.role !== 'WEREWOLF');
      if (candidates.length) {
        game.random = (game.random * 1664525 + 1013904223) >>> 0;
        const victim = candidates[game.random % candidates.length];
        victim.alive = false;
        result.night_victim = { name: victim.name, role: victim.role };
      }
      game.night_count += 1;
      game.phase = 'DAY';
    } else if (game.phase === 'DAY') {
      game.phase = 'VOTE';
    } else if (game.phase === 'VOTE') {
      const tally = {};
      Object.values(votes).forEach((target) => { tally[target] = (tally[target] || 0) + 1; });
      const entries = Object.entries(tally).sort((a, b) => b[1] - a[1]);
      if (entries.length > 1 && entries[0][1] > entries[1][1] || entries.length === 1) {
        const target = game.players.find((p) => p.name === entries[0][0]);
        if (target && target.alive) {
          target.alive = false;
          result.lynched = { name: target.name, role: target.role };
        }
      }
      votes = {};
      game.phase = 'DUSK';
    } else {
      game.phase = 'NIGHT';
    }
    game.winner = winner();
    result.winner = game.winner;
    result.game = snapshot();
    result.phase = game.phase;
    return response(result);
  };

  const vote = (body) => {
    if (!game) return response({ error: 'no active game' });
    const voter = game.players[body.voter_index];
    const target = game.players[body.target_index];
    if (!voter?.alive || !target?.alive) return response({ error: 'the dead cannot judge' });
    game.phase = 'VOTE';
    votes[voter.name] = target.name;
    return response({ game: snapshot(), phase: game.phase, offline: true });
  };

  return async (path, options = {}) => {
    if (path.endsWith('/new')) return newGame(JSON.parse(options.body || '{}'));
    if (path.endsWith('/advance')) return advance();
    if (path.endsWith('/vote')) return vote(JSON.parse(options.body || '{}'));
    if (path.endsWith('/state')) return response(game ? { game: snapshot(), phase: game.phase, offline: true } : { error: 'no active game' });
    if (path.endsWith('/reset')) { game = null; return response({ ok: true }); }
    return response({ error: 'unknown local operation' });
  };
})();

const networkFetch = window.fetch.bind(window);
const sessionKey = 'gothic-reckoning-session';
const sessionId = sessionStorage.getItem(sessionKey)
  || (crypto.randomUUID ? crypto.randomUUID() : `gr-${Date.now()}-${Math.random()}`);
sessionStorage.setItem(sessionKey, sessionId);

window.fetch = async (input, init = {}) => {
  const path = typeof input === 'string' ? input : input.url;
  if (!path.includes('/api/game/')) return networkFetch(input, init);
  const headers = new Headers(init.headers || {});
  headers.set('X-Session-Id', sessionId);
  const requestInit = { ...init, headers };
  try {
    const response = await networkFetch(input, requestInit);
    // A packaged app points at its WebView origin: its 404 means no API exists.
    if (response.ok) return response;
  } catch (_) {
    // Expected offline/standalone install path; use deterministic local table.
  }
  return OfflineTable(path, requestInit);
};
