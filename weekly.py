# /// script
# requires-python = ">=3.11"
# dependencies = ["nflreadpy>=0.1", "polars>=1.0"]
# ///
"""Weekly fantasy report off nflverse data. Half-PPR, my league's scoring.

    uv run weekly.py [--season 2025] [--selftest] [--export] [--no-write]

Roster comes from roster.txt. Joins on name+team+week; --selftest checks the rate.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

import polars as pl

HERE = Path(__file__).parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)

# keep the nflverse cache in the repo instead of somewhere in AppData
os.environ.setdefault("NFLREADPY_CACHE", "filesystem")
os.environ.setdefault("NFLREADPY_CACHE_DIR", str(DATA))
os.environ.setdefault("NFLREADPY_CACHE_DURATION", "86400")  # 1 day
os.environ.setdefault("NFLREADPY_VERBOSE", "false")

import nflreadpy as nfl  # noqa: E402  (must follow the cache env vars)

VAULT = Path.home() / "Documents" / "brain" / "ZAI" / "life-admin" / "Process"
REPORT = VAULT / "Fantasy Football 2026 - Weekly Report.md"

SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm(name: str) -> str:
    """Normalise a player name for joining. 'Kyle Pitts Sr.' -> 'kyle pitts'."""
    n = name.lower().replace(".", "").replace("'", "").replace("-", " ")
    return " ".join(SUFFIXES.sub("", n).split())


def half_ppr() -> pl.Expr:
    """This league's scoring, as one expression."""

    def c(name: str) -> pl.Expr:
        return pl.col(name).fill_null(0)

    return (
        c("passing_yards") / 25
        + c("passing_tds") * 4
        - c("passing_interceptions")
        + c("rushing_yards") / 10
        + c("rushing_tds") * 6
        + c("receptions") * 0.5
        + c("receiving_yards") / 10
        + c("receiving_tds") * 6
        + c("special_teams_tds") * 6  # league: "Return Touchdowns 6". Missing this cost 6pts/return TD.
        - (c("rushing_fumbles_lost") + c("receiving_fumbles_lost") + c("sack_fumbles_lost")) * 2
        + (c("passing_2pt_conversions") + c("rushing_2pt_conversions") + c("receiving_2pt_conversions")) * 2
    ).round(2)


def load_roster(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"No roster file at {path}. One player name per line, '#' for comments.")
    lines = (line.split("#")[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line]


class NoSeasonData(Exception):
    """nflverse has not published this season yet. Normal before week 1."""


def build(season: int) -> pl.DataFrame:
    """One row per player-week: league points, role metrics, snap share, injury status."""
    try:
        stats_raw = nfl.load_player_stats([season])
    except (ConnectionError, OSError) as e:
        if "404" in str(e):
            raise NoSeasonData(season) from e
        raise

    stats = (
        stats_raw
        .filter(pl.col("season_type") == "REG")
        .with_columns(half_ppr().alias("pts"), pl.col("player_display_name").map_elements(norm, return_dtype=pl.Utf8).alias("key"))
    )
    if stats.height == 0:
        raise NoSeasonData(season)

    snaps = nfl.load_snap_counts([season]).with_columns(
        pl.col("player").map_elements(norm, return_dtype=pl.Utf8).alias("key")
    ).select("key", "team", "week", "offense_pct")

    inj = nfl.load_injuries([season]).with_columns(
        pl.col("full_name").map_elements(norm, return_dtype=pl.Utf8).alias("key")
    ).select("key", "team", "week", "report_status", "practice_status")

    return (
        stats.join(snaps, on=["key", "team", "week"], how="left")
        .join(inj, on=["key", "team", "week"], how="left")
        .select(
            "key", "player_display_name", "position", "team", "week", "opponent_team",
            "pts", "offense_pct", "target_share", "air_yards_share", "wopr",
            "targets", "receptions", "carries", "receiving_yards", "rushing_yards",
            "report_status", "practice_status",
        )
    )


def trend(df: pl.DataFrame, weeks: int) -> pl.DataFrame:
    """Latest week against the mean of the prior weeks, per player."""
    latest = df["week"].max()
    window = df.filter(pl.col("week") > latest - weeks)

    now = window.filter(pl.col("week") == latest)
    before = (
        window.filter(pl.col("week") < latest)
        .group_by("key")
        .agg(
            pl.col("offense_pct").mean().alias("snap_prev"),
            pl.col("target_share").mean().alias("tgt_prev"),
            pl.col("carries").mean().alias("car_prev"),
            pl.col("pts").mean().alias("pts_prev"),
        )
    )
    return now.join(before, on="key", how="left").with_columns(
        (pl.col("offense_pct") - pl.col("snap_prev")).alias("d_snap"),
        (pl.col("target_share") - pl.col("tgt_prev")).alias("d_tgt"),
        (pl.col("carries") - pl.col("car_prev")).alias("d_car"),
    )


def pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def arrow(v) -> str:
    if v is None:
        return " "
    return "UP" if v > 0.07 else "DOWN" if v < -0.07 else "flat"


def report(season: int, weeks: int, roster: list[str], owned: list[str] | None = None) -> str:
    try:
        df = build(season)
    except NoSeasonData:
        return (
            f"# Fantasy Football {season} - Weekly Report\n\n"
            f"> **No {season} regular-season data published yet.** nflverse posts weekly stats after games are "
            f"played, so this is normal until week 1 is in the books.\n\n"
            f"Until then:\n\n"
            f"- `uv run weekly.py --season {season - 1}` reads last season, which is what the method notes are "
            f"calibrated against.\n"
            f"- Role metrics need 3-4 games before they mean anything - see "
            f"[[Football Analytics - EPA, DVOA and the Stats That Predict]] section 10. The first genuinely "
            f"useful run of this report is around week 4.\n"
        )

    latest = int(df["week"].max())
    t = trend(df, weeks)
    keys = {norm(n): n for n in roster}

    # most recent game per player, not just the latest week - otherwise anyone who
    # was hurt or rested just disappears from the table
    mine_all = df.filter(pl.col("key").is_in(list(keys)))
    missing = [n for k, n in keys.items() if k not in set(mine_all["key"])]
    mine = (
        mine_all.join(mine_all.group_by("key").agg(pl.col("week").max().alias("lw")), on="key")
        .filter(pl.col("week") == pl.col("lw"))
        .join(t.select("key", "d_snap", "d_tgt", "d_car"), on="key", how="left")
        .sort("pts", descending=True)
    )
    stale = mine.filter(pl.col("week") < latest).height

    out = [
        f"# Fantasy Football {season} - Weekly Report",
        "",
        f"> Generated {date.today().isoformat()} from nflverse, through **week {latest}**. "
        f"Trend compares week {latest} against the mean of the prior {weeks - 1} weeks.",
        "> Regenerate with `uv run weekly.py`. Do not hand-edit - this file is overwritten.",
        "",
    ]
    if missing:
        out += [f"> NOTE - no {season} data for **{', '.join(missing)}**, so they are absent from the table below. "
                f"That is expected for a rookie or a player who missed the season; it is a typo otherwise. "
                f"`uv run weekly.py --selftest` tells you which.", ""]

    out += [
        "## My roster",
        "",
        "| Player | Pos | Tm | Wk | Pts | Snap% | Tgt share | Car | Snap | Role | Status |",
        "|---|---|---|:--:|---:|---:|---:|---:|:--:|:--:|---|",
    ]
    for r in mine.iter_rows(named=True):
        status = r["report_status"] or r["practice_status"] or ""
        wk = str(r["week"]) if r["week"] == latest else f"**{r['week']}\\***"
        out.append(
            f"| {r['player_display_name']} | {r['position']} | {r['team']} | {wk} | {r['pts']:.1f} | "
            f"{pct(r['offense_pct'])} | {pct(r['target_share'])} | {r['carries'] or 0} | "
            f"{arrow(r['d_snap'])} | {arrow(r['d_tgt'])} | {status} |"
        )
    if stale:
        out += ["", f"\\* **did not play in week {latest}** - the line shown is his last game. Check why before starting him."]

    # Waiver candidates: real role, growing, not mine. In a 10-team league most of
    # these are free - check availability in Yahoo before claiming.
    # Anyone rostered anywhere in the league cannot be claimed, so drop them. Without
    # the Yahoo sync this list is empty and the table is a shortlist to check by hand.
    taken = {norm(n) for n in (owned or [])} | set(keys)
    risers = (
        t.filter(~pl.col("key").is_in(list(taken)))
        .filter(pl.col("position").is_in(["RB", "WR", "TE"]))
        .filter((pl.col("offense_pct") >= 0.35) | (pl.col("targets") >= 5) | (pl.col("carries") >= 8))
        # has to have been playing already, or the list fills with rest-week fill-ins
        .filter(pl.col("snap_prev") >= 0.15)
        .with_columns((pl.col("d_snap").fill_null(0) + pl.col("d_tgt").fill_null(0) * 2).alias("score"))
        .sort("score", descending=True)
        .head(15)
    )

    out += [
        "",
        "## Role risers - waiver targets" + (" (free agents only)" if owned else " (not filtered for availability)"),
        "",
        "*Ranked by snap-share and target-share gain. Rolling waiver priority, so spend the slot on a role change, not a streamer.*"
        + ("" if owned else "\n\n*No rostered-player list supplied, so some of these are owned. Run `yahoo.py --sync`, then pass `--exclude rostered_<id>.txt`.*"),
        "",
        "| Player | Pos | Tm | Pts | Snap% | d Snap | Tgt share | d Tgt | Car |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in risers.iter_rows(named=True):
        out.append(
            f"| {r['player_display_name']} | {r['position']} | {r['team']} | {r['pts']:.1f} | "
            f"{pct(r['offense_pct'])} | {pct(r['d_snap'])} | {pct(r['target_share'])} | "
            f"{pct(r['d_tgt'])} | {r['carries'] or 0} |"
        )

    # same score as the risers but negative. don't use an OR of two thresholds here,
    # it flags guys whose snaps went up
    fallers = (
        mine.with_columns((pl.col("d_snap").fill_null(0) + pl.col("d_tgt").fill_null(0) * 2).alias("score"))
        .filter(pl.col("score") < -0.07)
        .sort("score")
    )
    out += ["", "## Role fallers on my roster - the sell signal", ""]
    if fallers.height == 0:
        out.append("*None. No starter lost meaningful snap or target share this week.*")
    else:
        out.append("*A declining snap share shows up before the production drops. See [[Fantasy Football - The Weekly Lineup Decision]] step 3.*")
        out.append("")
        out.append("| Player | Pos | Snap% | d Snap | Tgt share | d Tgt |")
        out.append("|---|---|---:|---:|---:|---:|")
        for r in fallers.iter_rows(named=True):
            out.append(
                f"| {r['player_display_name']} | {r['position']} | {pct(r['offense_pct'])} | "
                f"{pct(r['d_snap'])} | {pct(r['target_share'])} | {pct(r['d_tgt'])} |"
            )

    out += ["", "---", "*Generated by `source/repos/nfl-vault/weekly.py`. Method: [[Fantasy Football - The Weekly Lineup Decision]]. Roster and settings: [[Fantasy Football 2026 - Season Tracker]].*", ""]
    return "\n".join(out)


def export() -> int:
    """Dump every nflverse season to db/*.parquet so DuckDB can query it.

    nflreadpy's own cache is hash-named and useless for lookups. Skipping pbp, it's
    several GB and nothing here uses it.
    """
    db = HERE / "db"
    db.mkdir(exist_ok=True)
    tables = [
        ("player_stats", lambda: nfl.load_player_stats(seasons=True)),
        ("team_stats", lambda: nfl.load_team_stats(seasons=True)),
        ("schedules", lambda: nfl.load_schedules(seasons=True)),
        ("snap_counts", lambda: nfl.load_snap_counts(seasons=True)),
        ("injuries", lambda: nfl.load_injuries(seasons=True)),
        ("ff_opportunity", lambda: nfl.load_ff_opportunity(seasons=True)),
        ("rosters", lambda: nfl.load_rosters(seasons=True)),
        ("draft_picks", lambda: nfl.load_draft_picks()),
        ("players", lambda: nfl.load_players()),
        ("contracts", lambda: nfl.load_contracts()),
    ]
    total = 0.0
    for name, fn in tables:
        try:
            df = fn()
        except Exception as e:  # one dataset being unavailable must not kill the rest
            print(f"skip {name}: {type(e).__name__}: {str(e)[:90]}")
            continue
        path = db / f"{name}.parquet"
        df.write_parquet(path)
        mb = path.stat().st_size / 1e6
        total += mb
        rng = ""
        if "season" in df.columns:
            rng = f"  seasons {df['season'].min()}-{df['season'].max()}"
        print(f"{name:16} {df.height:>8,} rows  {df.width:>4} cols  {mb:6.1f} MB{rng}")
    print(f"\n{total:.1f} MB in {db}")
    return 0


def selftest(season: int, roster: list[str]) -> int:
    """The smallest set of checks that fail if the data or the math breaks."""
    df = build(season)
    assert df.height > 0, f"no {season} data at all"

    raw = nfl.load_player_stats([season]).filter(pl.col("season_type") == "REG")

    # 1. nflverse PPR semantics: full PPR minus standard is exactly receptions.
    #    If nflverse changes this, the scoring assumptions in this file need a re-read.
    delta = raw.select(
        (pl.col("fantasy_points_ppr") - pl.col("fantasy_points") - pl.col("receptions").fill_null(0)).abs().max()
    ).item()
    assert delta < 0.01, f"PPR semantics changed: max delta {delta}"

    # 2. my scoring should land between standard and full PPR, once you undo the one
    #    difference: INT is -1 in my league, -2 in nflverse. keep the tolerance tight,
    #    a loose one hid a missing return-TD bug earlier
    both = raw.with_columns(half_ppr().alias("pts")).filter(pl.col("receptions").fill_null(0) > 0)
    bad = both.with_columns(
        (pl.col("pts") - pl.col("passing_interceptions").fill_null(0)).alias("equiv")
    ).filter(
        (pl.col("equiv") < pl.col("fantasy_points").fill_null(0) - 0.02)
        | (pl.col("equiv") > pl.col("fantasy_points_ppr").fill_null(0) + 0.02)
    )
    assert bad.height == 0, (
        f"{bad.height} rows scored outside [standard, full PPR]; worst: "
        f"{bad.sort((pl.col('equiv') - pl.col('fantasy_points')).abs(), descending=True).head(3).select('player_display_name', 'week', 'equiv', 'fantasy_points', 'fantasy_points_ppr').rows()}"
    )

    # 3. Target share is a share: it sums to ~1 per team-week.
    sums = (
        raw.filter(pl.col("target_share").is_not_null())
        .group_by(["team", "week"])
        .agg(pl.col("target_share").sum().alias("s"))
    )
    off = sums.filter((pl.col("s") < 0.95) | (pl.col("s") > 1.05))
    assert off.height == 0, f"{off.height} team-weeks where target_share does not sum to 1"

    # 4. name joins can duplicate rows, which would double-count a week
    assert df.height == raw.height, f"join fanned out: {raw.height} rows in, {df.height} out"

    # 5. did the snap join land. skill positions only - both tables carry linemen
    sk = df.filter(pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
    rate = sk.filter(pl.col("offense_pct").is_not_null()).height / max(sk.height, 1)
    assert rate > 0.85, f"snap join rate only {rate:.0%} - switch to load_ff_playerids"

    print(f"OK  rows={df.height}  weeks=1..{df['week'].max()}  snap-join={rate:.0%}  ppr-delta={delta:.4f}")

    # 6. roster names resolve. typo = fail, rookie with no stats yet = fine
    keys = set(df["key"])
    unmatched = [n for n in roster if norm(n) not in keys]
    if not unmatched:
        print(f"OK  all {len(roster)} roster names matched")
        return 0

    known = nfl.load_players().with_columns(
        pl.col("display_name").map_elements(norm, return_dtype=pl.Utf8).alias("key")
    )
    valid = dict(zip(known["key"], known["rookie_season"]))
    typos = [n for n in unmatched if norm(n) not in valid]
    no_stats = [(n, valid[norm(n)]) for n in unmatched if norm(n) in valid]

    for n, rk in no_stats:
        print(f"OK  {n}: real player, no {season} stats (rookie season {rk})")
    if typos:
        print(f"FAIL {len(typos)} name(s) not in nflverse at all - fix roster.txt: {', '.join(typos)}")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", type=int, default=date.today().year if date.today().month >= 9 else date.today().year - 1)
    p.add_argument("--weeks", type=int, default=3, help="lookback window including the latest week")
    p.add_argument("--roster", type=Path, default=HERE / "roster.txt")
    p.add_argument("--exclude", type=Path, default=None,
                   help="file of players rostered league-wide (from yahoo.py --sync); they are dropped from the waiver list")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--export", action="store_true", help="write all nflverse seasons to db/ as named parquet")
    a = p.parse_args()

    if a.export:
        return export()

    roster = load_roster(a.roster)
    if a.selftest:
        try:
            return selftest(a.season, roster)
        except NoSeasonData:
            print(f"SKIP  no {a.season} data published yet - run --selftest --season {a.season - 1}")
            return 0

    owned = load_roster(a.exclude) if a.exclude else None
    text = report(a.season, a.weeks, roster, owned)
    print(text)
    if not a.no_write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"\n-> written to {REPORT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
