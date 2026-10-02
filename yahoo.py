# /// script
# requires-python = ">=3.11"
# dependencies = ["yahoofantasy>=1.4.9"]
# ///
"""Are my waiver candidates actually available? Asks Yahoo, prints, saves nothing.

    uv run yahoo.py --check             # candidates from the weekly report's riser table
    uv run yahoo.py --check "DJ Moore"  # or name them
    uv run yahoo.py --probe             # field names and types only, no values
    uv run yahoo.py --selftest          # offline: parsing + "writes nothing"

Personal Use under the Yahoo Fantasy API Access and Use Agreement (2026-09-13).
The rules this file is built around (vault: NFL Fantasy Builder - Compliant Yahoo Concept):
  - nothing Yahoo returns is written to disk (s2c-vii) - the library's response cache is off
  - only the named candidates are looked up, never a whole league (s2c-x)
  - attribution under every output (cover page)
  - run it in your own terminal, not through an AI assistant (s3e)

Auth: `uvx --python 3.11 --from yahoofantasy yahoofantasy login`, run from this folder.
The .yahoofantasy file it writes holds credentials only.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).parent
REPORT = Path.home() / "Documents" / "brain" / "ZAI" / "life-admin" / "Process" / "Fantasy Football 2026 - Weekly Report.md"
SEASON_DEFAULT = date.today().year if date.today().month >= 9 else date.today().year - 1
ATTRIBUTION = "Fantasy data provided by Yahoo Fantasy - https://football.fantasysports.yahoo.com/"

SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm(name: str) -> str:
    # ponytail: copy of weekly.norm - importing weekly drags in polars/nflreadpy for 3 lines
    n = name.lower().replace(".", "").replace("'", "").replace("-", " ")
    return " ".join(SUFFIXES.sub("", n).split())


def connect():
    try:
        from yahoofantasy import Context
    except ImportError:
        sys.exit("uv could not load yahoofantasy. Run via: uv run yahoo.py")

    class NoCache(Context):
        """Context that never persists Yahoo responses (s2c-vii). Only the login
        credentials in .yahoofantasy are read; nothing is ever written."""

        def _load(self, persist_path, default, ttl=None):
            return super()._load(persist_path, default, ttl=-1) if persist_path == "auth" else default

        def _save(self, persist_path, persist_val):
            pass

    try:
        return NoCache()
    except ValueError as e:
        sys.exit(f"{e}\n\nNot logged in. From this folder:\n  uvx --python 3.11 --from yahoofantasy yahoofantasy login")


def leagues(ctx, season: int):
    from yahoofantasy.api.games import _find_game_id, games

    # ponytail: yahoofantasy's season->game_id table is hardcoded and lags new seasons;
    # ask Yahoo for the missing one. Drop this when the library ships the year.
    if str(season) not in games["nfl"]:
        games["nfl"][str(season)] = int(_find_game_id("nfl", season, ctx))
    ls = ctx.get_leagues("nfl", season)
    if not ls:
        sys.exit(f"No NFL leagues found for {season}.")
    return ls


def candidates() -> list[str]:
    """Player names from the riser table in the nflverse weekly report (not Yahoo data)."""
    if not REPORT.exists():
        sys.exit(f"No weekly report at {REPORT}. Run `uv run weekly.py` first, or name players.")
    names, inside = [], False
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line.startswith("## Role risers")
        elif inside and line.startswith("| ") and not line.startswith("| Player"):
            names.append(line.split("|")[1].strip())
    return names


def availability(xml: str, name: str) -> str:
    """FA / waivers / taken / ? for one player, from a players;search=.../ownership response."""
    from yahoofantasy.api.parse import as_list, get_value, parse_response

    players = (parse_response(xml)["fantasy_content"]["league"].get("players") or {}).get("player")
    for p in as_list(players or []):
        if norm(get_value(p["name"]["full"])) == norm(name):
            kind = get_value(p.get("ownership", {}).get("ownership_type", {"$": "?"}))
            return {"freeagents": "FA", "waivers": "waivers", "team": "taken"}.get(kind, kind)
    return "?"


def check(ctx, season: int, names: list[str]) -> int:
    import requests

    rows = []
    try:
        lgs = [(lg.league_key, getattr(lg, "name", lg.league_key)) for lg in leagues(ctx, season)]
        for name in names:
            last = norm(name).split()[-1]  # Yahoo search matches name substrings; full names with punctuation miss
            rows.append([name] + [
                availability(ctx.make_request(f"league/{key}/players;search={quote(last)}/ownership"), name)
                for key, _ in lgs
            ])
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 429:
            sys.exit("Yahoo rate limit (429). Stopping - try again later, do not loop (s2c-v).")
        if code == 403:
            sys.exit("403: this app is not authorized for the Fantasy API yet. Waiting on Yahoo's activation (s2a).")
        raise

    w = max([len(n) for n in names] + [6])
    cols = [n[:14] for _, n in lgs]
    print(f"{'Player':<{w}}  " + "  ".join(f"{c:<14}" for c in cols))
    for r in rows:
        print(f"{r[0]:<{w}}  " + "  ".join(f"{v:<14}" for v in r[1:]))
    print(f"\nFA = claim now | waivers = claim with priority | taken = owned | ? = not found\n{ATTRIBUTION}")
    return 0


def probe(ctx, season: int) -> int:
    """Field names and types only - safe to paste to anyone, including an AI assistant."""
    def shape(label, obj):
        print(f"{label}:")
        for k in sorted(k for k in vars(obj) if not k.startswith("_")):
            print(f"  {k}: {type(getattr(obj, k)).__name__}")

    lgs = leagues(ctx, season)
    print(f"{len(lgs)} league(s)")
    shape("league", lgs[0])
    from yahoofantasy.api.parse import parse_response
    xml = ctx.make_request(f"league/{lgs[0].league_key}/players;search=smith/ownership")
    print("ownership response top-level keys:", list(parse_response(xml)["fantasy_content"]["league"]))
    print(ATTRIBUTION)
    return 0


def selftest() -> int:
    """Offline. Fails if parsing breaks or if --check writes anything anywhere."""
    xml = ('<?xml version="1.0"?><fantasy_content><league><league_key>461.l.1</league_key><players count="2">'
           '<player><name><full>DJ Moore</full></name><ownership><ownership_type>freeagents</ownership_type></ownership></player>'
           '<player><name><full>Skyy Moore</full></name><ownership><ownership_type>team</ownership_type></ownership></player>'
           '</players></league></fantasy_content>')
    assert availability(xml, "DJ Moore") == "FA"
    assert availability(xml, "Skyy Moore") == "taken"
    assert availability(xml, "Nobody Moore") == "?"

    class League:
        league_key, name = "461.l.1", "Test League"

    class FakeCtx:
        def get_leagues(self, game, season):
            return [League()]

        def make_request(self, url):
            return xml

    watched = [HERE, REPORT.parent]
    before = {d: sorted(p.name for p in d.iterdir()) for d in watched if d.exists()}
    check(FakeCtx(), 2025, ["DJ Moore", "Skyy Moore"])
    after = {d: sorted(p.name for p in d.iterdir()) for d in watched if d.exists()}
    assert before == after, "check() wrote a file - Yahoo data must not be stored (s2c-vii)"
    print("OK  parsing, and --check writes nothing")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", type=int, default=SEASON_DEFAULT)
    p.add_argument("--check", nargs="*", metavar="NAME", help="availability of these players (default: report's risers)")
    p.add_argument("--probe", action="store_true", help="field names and types only")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()

    if a.selftest:
        return selftest()
    if a.probe:
        return probe(connect(), a.season)
    if a.check is not None:
        return check(connect(), a.season, a.check or candidates())
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
