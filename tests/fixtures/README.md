# melee.gg sample data

Real responses captured from a live tournament, for reference on data shape and
as a starting point for integration tests (currently `tracker.py` is only
covered by hand-written mocks in `tests/test_tracker.py`).

Source: https://melee.gg/Tournament/View/403891 ("2026 Galactic Championship
Last Chance Qualifier"), captured 2026-07-25. All data here is public —
melee.gg tournament pages are visible to anyone with the link. Every player's
username, display name and `ResultString` has since been replaced with an
invented name, consistently across all files, so the same player keeps the
same name in every round and tournament.

## Files

- **`tournament_view.html`** — full HTML of `GET /Tournament/View/{id}`.
  `melee_client.get_tournament_page()` reads everything it needs from this one
  page: the rounds from the `#pairings-round-selector-container` div, the name
  from the `og:title` meta tag, the UTC start time from
  `#tournament-headline-start-date-field`, and the status ("Ended" here) from
  the text after the info-circle icon in `#tournament-headline-registration`.

- **`round_matches.json`** — trimmed response (5 of 400 rows) from
  `POST /Match/GetRoundMatches/{round_id}`, the DataTables endpoint
  `tracker.get_matches()` posts to. Each row is one match: `Competitors` is a
  list of 1-2 teams, each with a `Team.Players` list (`Username`/`DisplayName`)
  and `ResultString` (e.g. `"MistyOtter won 2-0-0"`, `"...awarded a bye"`).

- **`round_standings.json`** — trimmed response (5 of 1485 rows) from
  `POST /Standing/GetRoundStandings`, used by `tracker.get_standings()`. Each
  row has `Rank`, `MatchRecord`, `GameRecord`, `Points`, tiebreaker percentages,
  and the same `Team.Players` shape as above.

- **`profile_results.json`** — trimmed response (3 of 70 rows, taken from
  the first page) from `POST /Profile/GetResults/{username}`, the DataTables
  endpoint behind the results table on a player's public profile page, used
  by `melee_client.get_player_tournaments()`. Captured 2026-10-01 for the
  username `IvoryTiger71` (picked from `round_standings.json`). Each row is
  one tournament the player entered: `TournamentId`, `TournamentName`,
  `TournamentStartDate` (UTC), `TournamentStatusDescription`, plus rank,
  record and organiser details the bot doesn't use. Every row in the capture
  has status `"Ended"`; no other status value has been observed yet.

All three JSON files keep the full DataTables envelope (`draw`, `recordsTotal`,
`recordsFiltered`, `data`) — `_post_all` in tracker.py reads `recordsTotal` to
decide whether to paginate for more rows.

Note: standings aren't available for every round_id — melee.gg only publishes
them for rounds where the `roundId` corresponds to a completed Swiss round;
some round IDs (e.g. a late top-cut round in this same tournament) return an
empty `data` array with `recordsTotal: 0`.
