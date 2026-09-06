# nfl-vault

Scripts I use for my fantasy football leagues. Nothing fancy.

`weekly.py` pulls [nflverse](https://github.com/nflverse) data and prints a weekly report:
my roster's snap share and target share trends, who's picking up a role and might be worth
a waiver claim, and who's losing one. Scoring is hardcoded to my league (half PPR, 4pt
passing TD, -1 INT, -2 fumble lost, 6 for return TDs).

`yahoo.py` reads my Yahoo leagues for matchup scores, top scorers and standings. Doesn't
work yet - Yahoo makes you apply for Fantasy API access now and I'm waiting on that.

## running it

Needs [uv](https://docs.astral.sh/uv/). Deps are declared in the scripts, so there's no
venv or requirements.txt to deal with.

    uv run weekly.py --season 2025    # season hasn't started, use last year
    uv run weekly.py --selftest       # checks the data and my scoring math
    uv run weekly.py --export         # dump all nflverse seasons to db/

Roster is `roster.txt`, one name per line.

Once Yahoo access comes through:

    uv run yahoo.py --sync
    uv run weekly.py --roster roster_1433436.txt --exclude rostered_1433436.txt

The `--exclude` bit drops players who are already rostered somewhere in the league, so
the waiver list only shows people I can actually claim.

## db/

`--export` writes about 50MB of parquet covering 1999-2025 (rosters go back to 1920).
Query it with duckdb, no import step:

    select player_display_name, sum(receiving_yards)::int yds
    from 'db/player_stats.parquet'
    where season = 2025 and season_type = 'REG'
    group by 1 order by yds desc limit 10

Skipping play-by-play, it's several GB and nothing here uses it.

## notes to self

Joins are on normalised name + team + week. Runs about 99% against snap counts and
`--selftest` fails under 85%. nflverse ships an id crosswalk (`load_ff_playerids`) if it
ever gets worse than that.

The report is useless before about week 4 - snap and target share need a few games before
they mean anything. It prints a notice instead of pretending otherwise.
