# demo fake melee

A stand-in for melee.gg, for taking screenshots and writing the user manual
without waiting for a live event. It replays the three captured tournaments in
`tests/fixtures/` one step at a time, with every player renamed, and the real
bot runs against it in real Discord servers.

Nothing here is used by the production bot and none of it is copied into the
Docker image.

## One-time setup

1. Create a second Discord application for the demo (same steps as "Discord
   setup" in the main README) and invite it to two throwaway servers.
2. Copy `demo_fake_melee/env.demo.example` to `.env.demo` in the repository
   root and fill in the demo token and the two server IDs.

The demo reads `.env.demo` only. It refuses to start unless that file sets the
token, the server IDs, a database path and a `MELEE_BASE_URL` on this machine,
so it cannot run with the real bot's token or database, or against the real
melee.gg.

## Running it

In one terminal, the fake site:

```bash
uv run python -m demo_fake_melee
```

In another, the bot:

```bash
uv run python -m demo_fake_melee.seed      # optional: fill both rosters
uv run python -m demo_fake_melee.run_bot
```

Then open <http://127.0.0.1:8765/_demo> for the controls.

## The scenario

Defined in `scenario.py`:

| ID | Tournament | Starts at | Use it for |
|---|---|---|---|
| 900101 | Northgate Open | round 2 finished | `/tournament bind`, `/watch` |
| 900102 | Midsummer Invitational | not started, begins in 2 days | discovery, waiting for round 1 |
| 900103 | Harbour City Championship | round 11 just paired | `/topcut` |

Six invented players are entered in all three: `Kestrel_VX`, `mossgoblin`,
`NovaTide`, `QuietRook`, `Brambleback` and `ohm_sweet_ohm`. The seed script
puts the first four on the first server's roster and the last three, plus
`QuietRook` again under another display name, on the second's. Skip the seed
script if you want to screenshot `/player add` yourself.

With discovery on, all three tournaments are suggested, since none has ended.

## Controls

Each Swiss round takes three steps: paired, half the results in, all results
in. On the control page:

- **Advance one step** moves a tournament to the next step. After round 12's
  results, one more advance ends it.
- **Jump** goes straight to a round and phase.
- **End** shows the tournament as Ended, which makes the bot conclude it.
- **Reset everything** returns to the scenario above.

Positions are kept in `demo_fake_melee/state.json`, so the fake site can be
restarted without losing its place.

The bot caches rounds it has seen finished. After jumping backwards or
resetting, delete the demo database (`demo.db`) and restart the bot.

The same controls work from a terminal:

```bash
curl -X POST http://127.0.0.1:8765/_demo/900101/advance
curl -X POST -d "round=11&phase=paired" http://127.0.0.1:8765/_demo/900101/goto
curl http://127.0.0.1:8765/_demo/state
```

## Demo timings

`run_bot.py` shortens the bot's waits so each state can be captured quickly.
Edit the constants at the top of that file to change them.

| | Demo | Real |
|---|---|---|
| Poll while Active | 15 seconds | 60 seconds |
| Poll while Idle | 30 seconds | 15 minutes |
| Goes Idle after | 3 minutes | 90 minutes |
| Fresh embed after a quiet spell of | 10 minutes | 6 hours |
| `/topcut` refresh | 15 seconds | 60 seconds |
| Discovery scan | every 2 minutes | every 3 hours |

The 5-second delay between requests to melee.gg is also dropped, since the
requests never leave this machine.

## What is not real

- The statuses "Registration Open" and "In Progress" are invented. Only
  "Ended" has been seen on the real site.
- There are 12 Swiss rounds and no quarterfinals, semifinals or finals: the
  fixtures hold no match data for those.
- Standings are worked out from the match results. They match the captured
  round 12 standings exactly, which `tests/test_demo_fake_melee.py` checks.
- The embeds still link to `melee.gg/Tournament/View/9001xx`, which does not
  exist there.
