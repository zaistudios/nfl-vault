# /// script
# requires-python = ">=3.11"
# dependencies = ["yahoofantasy>=1.4.9"]
# ///
"""Read my Yahoo leagues: rosters, matchups, who scored what.

    uv run yahoo.py --probe     # see what Yahoo actually returns
    uv run yahoo.py --sync      # roster_<id>.txt + rostered_<id>.txt for weekly.py
    uv run yahoo.py --recap     # week recap for every league, [--week N]

Auth is handled by `uvx --from yahoofantasy yahoofantasy login` (needs a Yahoo dev
app with redirect https://localhost:8000). Fantasy API access has to be approved by
Yahoo first: https://sports.yahoo.com/developer/access/

Untested against a live account - waiting on that approval. Attributes are read
through g() so Yahoo renaming a field gives "?" rather than a crash.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
VAULT = Path.home() / "Documents" / "brain" / "ZAI" / "life-admin" / "Process"
RECAP = VAULT / "Fantasy Football 2026 - Weekly Recap.md"
SEASON_DEFAULT = date.today().year if date.today().month >= 9 else date.today().year - 1


def g(obj, *names, default=None):
    """First attribute that exists. Yahoo's fields are built from XML at runtime,
    so they are not guaranteed and differ between league types."""
    for n in names:
        cur, ok = obj, True
        for part in n.split("."):
            if not hasattr(cur, part):
                ok = False
                break
            cur = getattr(cur, part)
        if ok and cur is not None:
            return cur
    return default


def connect():
    try:
        from yahoofantasy import Context
    except ImportError:
        sys.exit("pip/uv could not load yahoofantasy. Run via: uv run yahoo.py")
    try:
        return Context()
    except ValueError as e:
        sys.exit(
            f"{e}\n\nNot logged in yet. See the setup block at the top of this file:\n"
            "  uvx --from yahoofantasy yahoofantasy login"
        )


def leagues(ctx, season: int):
    from yahoofantasy.api.games import _find_game_id, games

    # ponytail: yahoofantasy's season->game_id table is hardcoded and lags new seasons;
    # ask Yahoo for the missing one. Drop this when the library ships the year.
    if str(season) not in games["nfl"]:
        games["nfl"][str(season)] = int(_find_game_id("nfl", season, ctx))
    ls = ctx.get_leagues("nfl", season)
    if not ls:
        sys.exit(f"No NFL leagues found for {season}. Wrong season, or the app lacks Fantasy Sports permission.")
    return ls


def my_team(league):
    """The team owned by the logged-in user."""
    for t in league.teams():
        if str(g(t, "is_owned_by_current_login", default="0")) == "1":
            return t
    return None


def probe(season: int) -> int:
    """Dump what Yahoo actually returns, so the recap can be written against facts."""
    ctx = connect()
    ls = leagues(ctx, season)
    print(f"Found {len(ls)} NFL league(s) for {season}\n")
    for lg in ls:
        print("=" * 70)
        print(f"league : {g(lg, 'name')}")
        print(f"  id   : {g(lg, 'league_id')}   key: {g(lg, 'league_key')}")
        print(f"  size : {g(lg, 'num_teams')} teams   scoring: {g(lg, 'scoring_type')}")
        print(f"  week : current={g(lg, 'current_week')}  start={g(lg, 'start_week')}  end={g(lg, 'end_week')}")
        print(f"  league attrs: {sorted(k for k in vars(lg) if not k.startswith('_'))}")

        teams = lg.teams()
        print(f"\n  {len(teams)} teams; first team attrs:")
        print(f"    {sorted(k for k in vars(teams[0]) if not k.startswith('_'))}")
        mine = my_team(lg)
        print(f"  my team: {g(mine, 'name') if mine else 'NOT IDENTIFIED - check is_owned_by_current_login'}")

        try:
            wks = lg.weeks()
            print(f"\n  {len(wks)} weeks. Last week's first matchup:")
            m = wks[-1].matchups[0]
            print(f"    matchup attrs: {sorted(k for k in vars(m) if not k.startswith('_'))}")
            print(f"    {g(m, 'team1.name')} vs {g(m, 'team2.name')}")
            print(f"    team1 points: {g(m, 'team1.team_points.total', 'team1_points')}")
        except Exception as e:
            print(f"    weeks/matchups failed: {type(e).__name__}: {e}")

        try:
            p = (mine or teams[0]).players()[0]
            print(f"\n  sample player: {g(p, 'name.full')}  pos={g(p, 'display_position')}")
            print(f"    player attrs: {sorted(k for k in vars(p) if not k.startswith('_'))}")
            print(f"    get_points(): {p.get_points()}")
        except Exception as e:
            print(f"    player probe failed: {type(e).__name__}: {e}")
    print("\nPaste this output back and the recap gets written against real shapes.")
    return 0


def sync(season: int) -> int:
    """Write, per league: my roster (for weekly.py) and every rostered player
    (so the waiver list can exclude players nobody can actually claim)."""
    ctx = connect()
    for lg in leagues(ctx, season):
        lid = g(lg, "league_id", default="unknown")
        mine = my_team(lg)
        if mine:
            names = [g(p, "name.full", default="?") for p in mine.players()]
            f = HERE / f"roster_{lid}.txt"
            f.write_text(
                f"# {g(lg, 'name')} (league {lid}) - synced {date.today()} by yahoo.py --sync\n"
                "# Do not hand-edit; re-run --sync after any add or drop.\n"
                + "\n".join(names) + "\n",
                encoding="utf-8",
            )
            print(f"{f.name}: {len(names)} players")

        owned = sorted({g(p, "name.full", default="?") for t in lg.teams() for p in t.players()})
        f = HERE / f"rostered_{lid}.txt"
        f.write_text("\n".join(owned) + "\n", encoding="utf-8")
        print(f"{f.name}: {len(owned)} players owned league-wide")
        print(f"  -> uv run weekly.py --roster roster_{lid}.txt --exclude rostered_{lid}.txt")
    return 0


def recap(season: int, week: int | None) -> int:
    ctx = connect()
    out = [
        f"# Fantasy Football {season} - Weekly Recap",
        "",
        f"> Generated {date.today().isoformat()} by `yahoo.py --recap`. Overwritten each run - do not hand-edit.",
        "> League state and results. Role trends and waiver targets live in [[Fantasy Football 2026 - Weekly Report]].",
        "",
    ]

    for lg in leagues(ctx, season):
        wk = week or int(g(lg, "current_week", default=1) or 1)
        # current_week is the week in progress; the last finished one is the one to read.
        wk = max(1, wk - 1) if week is None else wk
        out += [f"## {g(lg, 'name')} - week {wk}", ""]

        try:
            weeks = {int(g(w, "week_num", "week", default=i + 1)): w for i, w in enumerate(lg.weeks())}
            wkobj = weeks.get(wk)
        except Exception as e:
            out += [f"*Could not read weeks: {type(e).__name__}: {e}*", ""]
            continue
        if wkobj is None:
            out += [f"*No data for week {wk} yet.*", ""]
            continue

        mine = my_team(lg)
        myname = g(mine, "name", default=None)

        out += ["| | Team | Pts | | Team | Pts |", "|:--:|---|---:|:--:|---|---:|"]
        scores: list[tuple[str, float]] = []
        for m in wkobj.matchups:
            t1, t2 = g(m, "team1"), g(m, "team2")
            n1, n2 = g(t1, "name", default="?"), g(t2, "name", default="?")
            p1 = float(g(m, "team1.team_points.total", "team1_points", default=0) or 0)
            p2 = float(g(m, "team2.team_points.total", "team2_points", default=0) or 0)
            scores += [(n1, p1), (n2, p2)]
            star = "**" if myname in (n1, n2) else ""
            mark1, mark2 = ("W" if p1 > p2 else "L"), ("W" if p2 > p1 else "L")
            out.append(f"| {mark1} | {star}{n1}{star} | {p1:.1f} | {mark2} | {star}{n2}{star} | {p2:.1f} |")

        if scores:
            hi = max(scores, key=lambda s: s[1])
            lo = min(scores, key=lambda s: s[1])
            avg = sum(s[1] for s in scores) / len(scores)
            out += ["", f"High **{hi[0]} {hi[1]:.1f}** · low {lo[0]} {lo[1]:.1f} · league average {avg:.1f}", ""]

        # Who performed well - every rostered player in the league, by actual Yahoo points.
        perf: list[tuple[float, str, str, str]] = []
        for t in lg.teams():
            tn = g(t, "name", default="?")
            for p in t.players():
                try:
                    pts = float(p.get_points() or 0)
                except Exception:
                    continue
                perf.append((pts, g(p, "name.full", default="?"), g(p, "display_position", default="?"), tn))
        if perf:
            perf.sort(reverse=True)
            out += ["**Top 10 scorers, league-wide**", "", "| Player | Pos | Pts | Roster |", "|---|---|---:|---|"]
            for pts, nm, pos, tn in perf[:10]:
                star = "**" if tn == myname else ""
                out.append(f"| {nm} | {pos} | {pts:.1f} | {star}{tn}{star} |")
            if myname:
                out += ["", f"**{myname} - every starter and bench player**", "", "| Player | Pos | Pts |", "|---|---|---:|"]
                for pts, nm, pos, tn in [x for x in perf if x[3] == myname]:
                    out.append(f"| {nm} | {pos} | {pts:.1f} |")

        try:
            out += ["", "**Standings**", "", "| # | Team | Rec |", "|--:|---|---|"]
            for t in lg.standings():
                o = g(t, "team_standings.outcome_totals")
                rec = f"{g(o, 'wins', default=0)}-{g(o, 'losses', default=0)}-{g(o, 'ties', default=0)}" if o else "?"
                nm = g(t, "name", default="?")
                star = "**" if nm == myname else ""
                out.append(f"| {g(t, 'team_standings.rank', default='?')} | {star}{nm}{star} | {rec} |")
        except Exception as e:
            out += [f"*standings unavailable: {type(e).__name__}*"]
        out.append("")

    out += ["---", "*Generated by `source/repos/nfl-vault/yahoo.py`. Method: [[Fantasy Football - The Weekly Lineup Decision]]. Settings: [[Fantasy Football 2026 - Season Tracker]].*", ""]
    text = "\n".join(out)
    print(text)
    RECAP.parent.mkdir(parents=True, exist_ok=True)
    RECAP.write_text(text, encoding="utf-8")
    print(f"\n-> written to {RECAP}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", type=int, default=SEASON_DEFAULT)
    p.add_argument("--week", type=int, default=None)
    p.add_argument("--probe", action="store_true", help="dump what Yahoo returns - run this first")
    p.add_argument("--sync", action="store_true", help="write roster_*.txt and rostered_*.txt")
    p.add_argument("--recap", action="store_true", help="write the weekly recap into the vault")
    a = p.parse_args()

    if a.probe:
        return probe(a.season)
    if a.sync:
        return sync(a.season)
    if a.recap:
        return recap(a.season, a.week)
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
