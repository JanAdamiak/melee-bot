"""HTTP front for the fake site: the four melee.gg endpoints the bot calls,
plus a control page at /_demo for moving the tournaments along.

    uv run python -m demo_fake_melee
"""

import argparse
import html
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from demo_fake_melee.world import PHASES, SWISS_ROUNDS, Tournament, World

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
DEFAULT_STATE = Path(__file__).resolve().parent / "state.json"
# The real site returns at most this many rows per page, whatever is asked for.
PAGE_CAP = 500


def tournament_page(t: Tournament) -> str:
    """Just the parts of /Tournament/View/{id} that the bot reads."""
    name = html.escape(t.name, quote=True)
    start = t.start_time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    buttons = "\n".join(
        f'<button class="btn btn-gray round-selector" data-id="{t.round_id(n)}" '
        f'data-name="Round {n}" data-is-started="{n <= t.current_round}">Round {n}</button>'
        for n in range(1, SWISS_ROUNDS + 1)
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<title>{name} | Melee</title>
<meta content="{name}" property="og:title"></meta>
</head>
<body>
<p id="tournament-headline-start-date-field">
    <i class="fa fa-calendar-alt mr-1"></i><span data-toggle="datetime" data-value="{start}"></span>
</p>
<h3 class="mb-1">{name}</h3>
<p id="tournament-headline-registration">
    Entry Fee: Free |
    <i class="fa fa-user-check mr-1"></i>Open Registration |
    Format: Premier |
    <i class="fa fa-info-circle mr-1"></i>{t.status} |
</p>
<div id="pairings-round-selector-container" class="round-selector-container">
{buttons}
</div>
</body>
</html>
"""


def control_page(world: World) -> str:
    rounds = "".join(f'<option value="{n}">Round {n}</option>' for n in range(1, SWISS_ROUNDS + 1))
    phases = "".join(f'<option value="{p}">{p}</option>' for p in PHASES)
    rows = ""
    for t in world.tournaments.values():
        rows += f"""
<tr>
  <td>{t.id}</td>
  <td>{html.escape(t.name)}</td>
  <td>{t.status}</td>
  <td>{t.position}</td>
  <td>
    <form method="post" action="/_demo/{t.id}/advance"><button>Advance one step</button></form>
    <form method="post" action="/_demo/{t.id}/goto">
      <select name="round"><option value="0">Not started</option>{rounds}</select>
      <select name="phase">{phases}</select>
      <button>Jump</button>
    </form>
    <form method="post" action="/_demo/{t.id}/end"><button>End</button></form>
  </td>
</tr>"""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Demo fake melee</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #bbb; padding: .5rem .75rem; text-align: left; vertical-align: top; }}
  form {{ display: inline-block; margin: 0 .5rem .25rem 0; }}
</style>
</head>
<body>
<h1>Demo fake melee</h1>
<p>Each round goes: paired &rarr; half the results in &rarr; all results in. The bot picks up a
change on its next poll.</p>
<table>
<tr><th>ID</th><th>Tournament</th><th>Status</th><th>Position</th><th>Controls</th></tr>{rows}
</table>
<p><form method="post" action="/_demo/reset"><button>Reset everything</button></form>
Jumping backwards or resetting leaves the bot holding results it has already cached:
delete the demo database and restart the bot afterwards.</p>
</body>
</html>
"""


def _state(world: World) -> dict:
    return {
        str(t.id): {
            "name": t.name, "status": t.status, "round": t.current_round,
            "phase": t.phase, "position": t.position, "start_time": t.start_time.isoformat(),
        }
        for t in world.tournaments.values()
    }


class Handler(BaseHTTPRequestHandler):
    world: World   # set on the subclass made by make_server

    def log_message(self, format: str, *args) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, body: str, status: int = 200) -> None:
        self._send(status, body, "text/html; charset=utf-8")

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data), "application/json; charset=utf-8")

    def _not_found(self) -> None:
        self._send(404, "Not found", "text/plain; charset=utf-8")

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        fields = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        return {key: values[-1] for key, values in fields.items()}

    def _datatable(self, rows: list[dict], form: dict[str, str]) -> None:
        start = int(form.get("start") or 0)
        length = min(int(form.get("length") or PAGE_CAP), PAGE_CAP)
        self._json({
            "draw": int(form.get("draw") or 1),
            "recordsTotal": len(rows),
            "recordsFiltered": len(rows),
            "data": rows[start:start + length],
        })

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        with self.world.lock:
            if m := re.fullmatch(r"/Tournament/View/(\d+)", path):
                tournament = self.world.tournaments.get(int(m.group(1)))
                if tournament is None:
                    return self._not_found()
                return self._html(tournament_page(tournament))
            if path in ("", "/_demo"):
                return self._html(control_page(self.world))
            if path == "/_demo/state":
                return self._json(_state(self.world))
        self._not_found()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        form = self._form()
        with self.world.lock:
            if m := re.fullmatch(r"/Match/GetRoundMatches/(\d+)", path):
                found = self.world.round(int(m.group(1)))
                return self._datatable(found[0].matches(found[1]) if found else [], form)
            if path == "/Standing/GetRoundStandings":
                round_id = form.get("roundId", "")
                found = self.world.round(int(round_id)) if round_id.isdigit() else None
                return self._datatable(found[0].standings(found[1]) if found else [], form)
            if m := re.fullmatch(r"/Profile/GetResults/(.+)", path):
                return self._datatable(self.world.profile(unquote(m.group(1))), form)
            if path == "/_demo/reset":
                self.world.reset()
                return self._redirect()
            if m := re.fullmatch(r"/_demo/(\d+)/(advance|goto|end)", path):
                tournament_id, action = int(m.group(1)), m.group(2)
                if tournament_id not in self.world.tournaments:
                    return self._not_found()
                try:
                    self._control(tournament_id, action, form)
                except ValueError as e:
                    return self._send(400, str(e), "text/plain; charset=utf-8")
                return self._redirect()
        self._not_found()

    def _control(self, tournament_id: int, action: str, form: dict[str, str]) -> None:
        if action == "advance":
            self.world.advance(tournament_id)
        elif action == "end":
            self.world.end(tournament_id)
        else:
            phase = form.get("phase") or "paired"
            if phase not in PHASES:
                raise ValueError(f"phase must be one of {', '.join(PHASES)}")
            self.world.goto(tournament_id, int(form.get("round") or 0), phase)

    def _redirect(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/_demo")
        self.send_header("Content-Length", "0")
        self.end_headers()


def make_server(world: World, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("DemoHandler", (Handler,), {"world": world})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="A fake melee.gg for demos and screenshots.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--state", type=Path, default=DEFAULT_STATE,
        help="file the tournaments' positions are kept in between runs",
    )
    args = parser.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")

    server = make_server(World(state_path=args.state), args.host, args.port)
    log.info("Fake melee.gg on http://%s:%s — controls at http://%s:%s/_demo",
             args.host, args.port, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
