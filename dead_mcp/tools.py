"""Grateful Dead setlist database tools — query dead.db (shows, performances, Plex library, archive.org recordings)."""
import asyncio
import calendar
import os
import sqlite3
import sys
from datetime import date as dt_date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lore import query as lore_query          # noqa: E402
from lore.router import ask as router_ask     # noqa: E402

DB_PATH = Path(os.environ.get("DEAD_DB", "/data/nas/dead.db"))


def _connect():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql, params=()):
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _loc(row):
    parts = [row.get("venue"), row.get("city"), row.get("state") or row.get("country")]
    return ", ".join(p for p in parts if p)


def _find_song(conn, query):
    """Returns (matched_name, []) on hit, (None, [suggestions]) on miss."""
    row = conn.execute(
        "SELECT DISTINCT song_name FROM performances WHERE LOWER(song_name) = LOWER(?) LIMIT 1",
        (query,),
    ).fetchone()
    if row:
        return row[0], []
    rows = conn.execute(
        "SELECT DISTINCT song_name FROM performances "
        "WHERE LOWER(song_name) LIKE LOWER(?) ORDER BY song_name LIMIT 5",
        (f"%{query}%",),
    ).fetchall()
    return None, [r[0] for r in rows]


def _longest_chain(perfs):
    """perfs: list of (song_name, segued_out). Returns longest consecutive segue chain."""
    best, cur = [], []
    for song, seg in perfs:
        cur.append(song)
        if not seg:
            if len(cur) > len(best):
                best = cur[:]
            cur = []
    if cur and len(cur) > len(best):
        best = cur[:]
    return best


def _src_rank(src):
    return {"SBD": 0, "MATRIX": 1, "FM": 2, "AUD": 3}.get(src, 4)


def register(mcp):

    @mcp.tool()
    async def dead_setlist(date: str) -> str:
        """Return the full setlist for a Grateful Dead show by date (YYYY-MM-DD).
        Shows songs in set order with segue markers (>) and venue info.
        Also indicates if the show is in your Plex library."""
        def _run():
            shows = _q("SELECT * FROM shows WHERE date = ?", (date,))
            if not shows:
                return f"No show found for {date}."
            show = shows[0]

            perfs = _q(
                "SELECT set_num, position, song_name, segued_out "
                "FROM performances WHERE show_date = ? ORDER BY set_num, position",
                (date,),
            )
            if not perfs:
                return f"Show on {date} exists but has no setlist data."

            plex = _q("SELECT title FROM plex_albums WHERE show_date = ?", (date,))

            lines = [f"{date} — {_loc(show)}"]
            if plex:
                titles = " / ".join(p["title"] for p in plex)
                lines.append(f"In Plex: {titles}")
            lines.append("")

            cur_set = None
            for p in perfs:
                if p["set_num"] != cur_set:
                    cur_set = p["set_num"]
                    label = f"Set {cur_set}" if cur_set <= 2 else f"Encore {cur_set - 2}"
                    lines.append(f"  {label}:")
                sep = " >" if p["segued_out"] else ""
                lines.append(f"    {p['position']}. {p['song_name']}{sep}")

            return "\n".join(lines)

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_song_history(song: str, year: int = 0, limit: int = 30) -> str:
        """Show every performance of a song, optionally filtered to a specific year.
        Returns date, venue, set number, and position in the set.
        Useful for finding the best-known versions or seeing how often a song was played."""
        def _run():
            like = f"%{song}%"
            if year:
                rows = _q(
                    "SELECT p.show_date, p.set_num, p.position, p.song_name, p.segued_out, "
                    "s.venue, s.city, s.state "
                    "FROM performances p JOIN shows s ON s.date = p.show_date "
                    "WHERE p.song_name LIKE ? AND p.show_date LIKE ? "
                    "ORDER BY p.show_date LIMIT ?",
                    (like, f"{year}-%", limit),
                )
            else:
                rows = _q(
                    "SELECT p.show_date, p.set_num, p.position, p.song_name, p.segued_out, "
                    "s.venue, s.city, s.state "
                    "FROM performances p JOIN shows s ON s.date = p.show_date "
                    "WHERE p.song_name LIKE ? ORDER BY p.show_date LIMIT ?",
                    (like, limit),
                )
            if not rows:
                return f"No performances found matching '{song}'."

            total = _q(
                "SELECT COUNT(*) n FROM performances WHERE song_name LIKE ?", (like,)
            )[0]["n"]

            lines = [f"'{rows[0]['song_name']}' — {total} total performances (showing {len(rows)}):"]
            for r in rows:
                set_label = f"S{r['set_num']}P{r['position']}"
                seg = ">" if r["segued_out"] else " "
                lines.append(f"  {r['show_date']} {seg} {set_label}  {_loc(r)}")
            return "\n".join(lines)

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_shows(
        year: int = 0,
        venue: str = "",
        city: str = "",
        song: str = "",
        limit: int = 25,
    ) -> str:
        """Search Grateful Dead shows by year, venue, city, or a song that was played.
        All filters are optional and combinable. Returns date and location.
        Set song= to find all shows where a particular song appeared."""
        def _run():
            wheres, params = [], []
            if year:
                wheres.append("s.date LIKE ?"); params.append(f"{year}-%")
            if venue:
                wheres.append("s.venue LIKE ?"); params.append(f"%{venue}%")
            if city:
                wheres.append("s.city LIKE ?"); params.append(f"%{city}%")

            if song:
                sql = (
                    "SELECT DISTINCT s.date, s.venue, s.city, s.state, s.country "
                    "FROM shows s JOIN performances p ON p.show_date = s.date "
                    "WHERE p.song_name LIKE ?"
                )
                song_params = [f"%{song}%"]
                if wheres:
                    sql += " AND " + " AND ".join(wheres)
                    song_params += params
                sql += " ORDER BY s.date LIMIT ?"
                song_params.append(limit)
                rows = _q(sql, song_params)
            else:
                base = "SELECT date, venue, city, state, country FROM shows"
                if wheres:
                    base += " WHERE " + " AND ".join(wheres)
                base += " ORDER BY date LIMIT ?"
                params.append(limit)
                rows = _q(base, params)

            if not rows:
                return "No shows matched."
            lines = [f"{r['date']}  {_loc(r)}" for r in rows]
            return f"{len(rows)} shows:\n" + "\n".join(lines)

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_plex_library(query: str = "", limit: int = 40) -> str:
        """List Grateful Dead shows in your Plex library, with venue info from the setlist DB.
        Optionally filter by album title keyword. Returns Plex ratingKey (for playlist tools),
        show date, album title, and venue."""
        def _run():
            if query:
                rows = _q(
                    "SELECT pa.rating_key, pa.title, pa.show_date, "
                    "s.venue, s.city, s.state "
                    "FROM plex_albums pa LEFT JOIN shows s ON s.date = pa.show_date "
                    "WHERE pa.title LIKE ? AND pa.show_date IS NOT NULL "
                    "ORDER BY pa.show_date LIMIT ?",
                    (f"%{query}%", limit),
                )
            else:
                rows = _q(
                    "SELECT pa.rating_key, pa.title, pa.show_date, "
                    "s.venue, s.city, s.state "
                    "FROM plex_albums pa LEFT JOIN shows s ON s.date = pa.show_date "
                    "WHERE pa.show_date IS NOT NULL "
                    "ORDER BY pa.show_date LIMIT ?",
                    (limit,),
                )
            if not rows:
                return "No dated GD albums found in Plex library."
            lines = []
            for r in rows:
                loc = _loc(r) or "unknown venue"
                lines.append(f"  [{r['rating_key']}] {r['show_date']}  {loc}\n    {r['title']}")
            return f"{len(rows)} Plex GD albums:\n" + "\n".join(lines)

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_show_recordings(date: str) -> str:
        """How to hear a specific Grateful Dead show (YYYY-MM-DD).
        Shows owned Plex copies and top-ranked archive.org recordings.
        Anchors to the shows table — stray archive.org items on non-show dates are ignored.
        Example: dead_show_recordings('1977-05-08') → Cornell 77, 23 archive recordings."""
        def _run():
            conn = _connect()
            try:
                show = conn.execute(
                    "SELECT date, venue, city, state FROM shows WHERE date = ?", (date,)
                ).fetchone()
                if not show:
                    return "Not a known Grateful Dead show date."
                show = dict(show)

                lines = [f"{date} — {_loc(show)}", ""]

                owned = [dict(r) for r in conn.execute(
                    "SELECT title, rating_key FROM plex_albums WHERE show_date = ?", (date,)
                ).fetchall()]
                if owned:
                    lines.append("In your Plex library:")
                    for o in owned:
                        lines.append(f"  • {o['title']}  (ratingKey: {o['rating_key']})")
                    lines.append("")

                arch_all = [dict(r) for r in conn.execute(
                    "SELECT identifier, source_type, avg_rating, num_reviews, downloads, archive_url "
                    "FROM archive_recordings WHERE show_date = ?", (date,)
                ).fetchall()]

                if arch_all:
                    arch_sorted = sorted(
                        arch_all,
                        key=lambda r: (
                            0 if (r["num_reviews"] or 0) >= 3 else 1,
                            -(r["avg_rating"] or 0),
                            _src_rank(r["source_type"]),
                            -(r["downloads"] or 0),
                        ),
                    )
                    lines.append(f"Top recordings on archive.org ({len(arch_all)} total):")
                    for i, r in enumerate(arch_sorted[:5], 1):
                        rating = (f"★{r['avg_rating']:.1f} ({r['num_reviews']} reviews)"
                                  if r["avg_rating"] else f"↓{r['downloads']} downloads")
                        lines.append(f"  {i}. [{r['source_type']:<6}] {rating}")
                        lines.append(f"     {r['archive_url']}")
                    lines.append("")
                elif not owned:
                    lines.append("No recordings available — known show but nothing in your library or on archive.org.")

                return "\n".join(lines).rstrip()
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_this_date(month: int, day: int) -> str:
        """Shows the Dead played on this calendar date across all years (1965-1995).
        Returns set opener, Set 2 highlight (longest segue chain), Plex and archive status.
        Example: dead_this_date(5, 8) → every May 8th show in GD history."""
        def _run():
            conn = _connect()
            try:
                date_filter = f"{month:02d}-{day:02d}"
                shows = [dict(r) for r in conn.execute(
                    "SELECT s.date, s.venue, s.city, s.state, s.country, "
                    "COUNT(DISTINCT ar.identifier) AS archive_count "
                    "FROM shows s "
                    "LEFT JOIN archive_recordings ar ON ar.show_date = s.date "
                    "WHERE strftime('%m-%d', s.date) = ? "
                    "GROUP BY s.date ORDER BY s.date",
                    (date_filter,),
                ).fetchall()]

                month_name = calendar.month_name[month]
                if not shows:
                    return f"The Dead did not play on {month_name} {day} in any year."

                lines = [f"On {month_name} {day} in Grateful Dead history:\n"]
                for show in shows:
                    date = show["date"]
                    lines.append(f"  {date} — {_loc(show)}")

                    s1 = conn.execute(
                        "SELECT song_name FROM performances "
                        "WHERE show_date = ? AND set_num = 1 ORDER BY position LIMIT 1",
                        (date,),
                    ).fetchone()
                    if s1:
                        lines.append(f"    Set 1 opener: {s1[0]}")

                    s2 = [(r[0], r[1]) for r in conn.execute(
                        "SELECT song_name, segued_out FROM performances "
                        "WHERE show_date = ? AND set_num = 2 ORDER BY position",
                        (date,),
                    ).fetchall()]
                    if s2:
                        chain = _longest_chain(s2)
                        if len(chain) > 1:
                            lines.append(f"    Set 2 highlight: {' > '.join(chain)}")
                        else:
                            lines.append(f"    Set 2 opener: {s2[0][0]}")

                    plex = conn.execute(
                        "SELECT title FROM plex_albums WHERE show_date = ?", (date,)
                    ).fetchone()
                    plex_str = plex[0] if plex else "no"
                    lines.append(f"    [In Plex: {plex_str}]   [Archive: {show['archive_count']} recordings]")
                    lines.append("")

                return "\n".join(lines).rstrip()
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_song_stats(song: str) -> str:
        """Deep statistics on a single song: performance count, first/last played,
        longest gap, set distribution, decade breakdown.
        Example: dead_song_stats('Dark Star') → 276 performances, first/last dates, gap analysis."""
        def _run():
            conn = _connect()
            try:
                matched, suggestions = _find_song(conn, song)
                if not matched:
                    if suggestions:
                        return f"No exact match for '{song}'. Did you mean: {', '.join(suggestions)}?"
                    return f"No song found matching '{song}'."

                dates = [r[0] for r in conn.execute(
                    "SELECT DISTINCT show_date FROM performances "
                    "WHERE LOWER(song_name) = LOWER(?) ORDER BY show_date",
                    (matched,),
                ).fetchall()]
                total = len(dates)

                first, last = dates[0], dates[-1]

                # Longest gap between consecutive plays
                longest_gap = timedelta(0)
                gap_from = gap_to = ""
                for i in range(1, len(dates)):
                    d1 = dt_date.fromisoformat(dates[i - 1])
                    d2 = dt_date.fromisoformat(dates[i])
                    g = d2 - d1
                    if g > longest_gap:
                        longest_gap = g
                        gap_from = dates[i - 1]
                        gap_to = dates[i]

                gap_years = longest_gap.days // 365
                gap_months = (longest_gap.days % 365) // 30
                gap_str = ""
                if gap_years:
                    gap_str += f"{gap_years} year{'s' if gap_years != 1 else ''}"
                    if gap_months:
                        gap_str += f", {gap_months} month{'s' if gap_months != 1 else ''}"
                else:
                    gap_str = f"{longest_gap.days} days"

                # Set distribution
                set_counts = {}
                for r in conn.execute(
                    "SELECT set_num, COUNT(*) n FROM performances "
                    "WHERE LOWER(song_name) = LOWER(?) GROUP BY set_num ORDER BY n DESC",
                    (matched,),
                ).fetchall():
                    label = f"Set {r[0]}" if r[0] <= 2 else f"Encore"
                    set_counts[label] = r[1]

                # Decade breakdown
                decade_counts: dict = {}
                for d in dates:
                    decade = (int(d[:4]) // 10) * 10
                    decade_counts[decade] = decade_counts.get(decade, 0) + 1

                # Years played
                years_played = len({d[:4] for d in dates})

                lines = [
                    f'"{matched}" — {total} performances\n',
                    f"  First played:   {first} — {_loc(_q('SELECT venue, city, state FROM shows WHERE date = ?', (first,))[0])}",
                    f"  Last played:    {last} — {_loc(_q('SELECT venue, city, state FROM shows WHERE date = ?', (last,))[0])}",
                ]
                if gap_from:
                    lines.append(f"  Longest gap:    {gap_str}  ({gap_from} → {gap_to})")

                set_line = ", ".join(f"{label} ({n}x)" for label, n in set_counts.items())
                lines.append(f"  Most common:    {set_line}")
                lines.append(f"  Years played:   {years_played} distinct years")

                decade_parts = []
                for dec in sorted(decade_counts):
                    decade_parts.append(f"{dec}s ({decade_counts[dec]}x)")
                lines.append(f"  By decade:      {', '.join(decade_parts)}")

                return "\n".join(lines)
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_segues(song_a: str, song_b: str = "") -> str:
        """Segue analysis. Two modes:
        - One song: top 10 songs that song_a most often segued into.
        - Two songs: every time song_a > song_b happened, with date and set context.
        Example: dead_segues('Scarlet Begonias', 'Fire On The Mountain') → all 238 occurrences."""
        def _run():
            conn = _connect()
            try:
                matched_a, sugg_a = _find_song(conn, song_a)
                if not matched_a:
                    if sugg_a:
                        return f"No match for '{song_a}'. Did you mean: {', '.join(sugg_a)}?"
                    return f"No song found matching '{song_a}'."

                if song_b:
                    matched_b, sugg_b = _find_song(conn, song_b)
                    if not matched_b:
                        if sugg_b:
                            return f"No match for '{song_b}'. Did you mean: {', '.join(sugg_b)}?"
                        return f"No song found matching '{song_b}'."

                    rows = [dict(r) for r in conn.execute(
                        "SELECT p1.show_date, s.venue, s.city, s.state, p1.set_num, p1.position "
                        "FROM performances p1 "
                        "JOIN performances p2 ON p2.show_date = p1.show_date "
                        "  AND p2.set_num = p1.set_num AND p2.position = p1.position + 1 "
                        "JOIN shows s ON s.date = p1.show_date "
                        "WHERE LOWER(p1.song_name) = LOWER(?) AND p1.segued_out = 1 "
                        "  AND LOWER(p2.song_name) = LOWER(?) "
                        "ORDER BY p1.show_date",
                        (matched_a, matched_b),
                    ).fetchall()]

                    if not rows:
                        return f'"{matched_a} > {matched_b}" — never occurred.'

                    lines = [f'"{matched_a} > {matched_b}" — {len(rows)} occurrences\n']
                    for r in rows:
                        lines.append(f"  {r['show_date']}  {_loc(r)}  (Set {r['set_num']}, pos {r['position']}>{r['position']+1})")
                    return "\n".join(lines)

                else:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT p2.song_name, COUNT(*) n "
                        "FROM performances p1 "
                        "JOIN performances p2 ON p2.show_date = p1.show_date "
                        "  AND p2.set_num = p1.set_num AND p2.position = p1.position + 1 "
                        "WHERE LOWER(p1.song_name) = LOWER(?) AND p1.segued_out = 1 "
                        "GROUP BY p2.song_name ORDER BY n DESC LIMIT 10",
                        (matched_a,),
                    ).fetchall()]

                    total_segues = sum(r["n"] for r in rows)
                    if not rows:
                        return f'"{matched_a}" — no recorded segues out.'

                    lines = [f'"{matched_a}" most often segued into ({total_segues} total segues):\n']
                    for r in rows:
                        lines.append(f"  {r['n']:>4}x  {r['song_name']}")
                    return "\n".join(lines)
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_run(date: str) -> str:
        """Tour run context for any show: finds all adjacent shows (within 3 days of each other)
        and returns the full run with the queried date marked.
        Example: dead_run('1977-05-08') → entire spring '77 tour leg."""
        def _run():
            conn = _connect()
            try:
                if not conn.execute("SELECT 1 FROM shows WHERE date = ?", (date,)).fetchone():
                    return f"{date} is not a known Grateful Dead show date."

                all_dates = [r[0] for r in conn.execute(
                    "SELECT date FROM shows WHERE length(date) = 10 ORDER BY date"
                ).fetchall()]

                # Build runs (consecutive shows ≤3 days apart)
                run_of_target = []
                cur_run = [all_dates[0]]
                for i in range(1, len(all_dates)):
                    gap = (dt_date.fromisoformat(all_dates[i]) - dt_date.fromisoformat(all_dates[i - 1])).days
                    if gap <= 3:
                        cur_run.append(all_dates[i])
                    else:
                        if date in cur_run:
                            run_of_target = cur_run
                            break
                        cur_run = [all_dates[i]]
                if not run_of_target:
                    run_of_target = cur_run  # last run

                if len(run_of_target) == 1:
                    show = dict(conn.execute(
                        "SELECT venue, city, state FROM shows WHERE date = ?", (date,)
                    ).fetchone())
                    return f"{date} was a standalone show — no adjacent dates within 3 days.\n  {_loc(show)}"

                # Fetch venue info for all dates in the run
                placeholders = ",".join("?" * len(run_of_target))
                shows = [dict(r) for r in conn.execute(
                    f"SELECT date, venue, city, state FROM shows WHERE date IN ({placeholders}) ORDER BY date",
                    run_of_target,
                ).fetchall()]

                first = dt_date.fromisoformat(run_of_target[0])
                last = dt_date.fromisoformat(run_of_target[-1])
                span_days = (last - first).days + 1

                if first.month == last.month:
                    label = f"{calendar.month_name[first.month]} {first.year}"
                else:
                    label = f"{calendar.month_abbr[first.month]}-{calendar.month_abbr[last.month]} {first.year}"

                lines = [
                    f"{date} was part of a multi-night run:\n",
                    f"  {label} ({len(run_of_target)} shows over {span_days} days):",
                ]
                for s in shows:
                    marker = "→ " if s["date"] == date else "  "
                    lines.append(f"  {marker}{s['date']}  {_loc(s)}")

                return "\n".join(lines)
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_rare_songs(year: int = 0, max_plays: int = 10) -> str:
        """Songs played max_plays times or fewer across the entire run, or within a single year.
        Useful for finding deep cuts, one-offs, and songs the Dead almost never played.
        Example: dead_rare_songs(max_plays=1) → songs played exactly once."""
        def _run():
            if year:
                rows = _q(
                    "SELECT song_name, COUNT(*) n FROM performances WHERE show_date LIKE ? "
                    "GROUP BY song_name HAVING n <= ? ORDER BY n ASC, song_name",
                    (f"{year}-%", max_plays),
                )
                scope = f"in {year}"
            else:
                rows = _q(
                    "SELECT song_name, COUNT(*) n FROM performances "
                    "GROUP BY song_name HAVING n <= ? ORDER BY n ASC, song_name",
                    (max_plays,),
                )
                scope = "overall"

            if not rows:
                plays_str = f"{max_plays} play{'s' if max_plays != 1 else ''}"
                return f"No songs with ≤{plays_str} {scope}."

            lines = [f"{len(rows)} songs played ≤{max_plays} times {scope}:\n"]
            prev_n = None
            for r in rows:
                if r["n"] != prev_n:
                    lines.append(f"  {r['n']}x:")
                    prev_n = r["n"]
                lines.append(f"    {r['song_name']}")
            return "\n".join(lines)

        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_stats() -> str:
        """Overall stats for the Grateful Dead setlist database: show count, song count,
        most-played songs, most-played venues, most active years, and archive.org coverage."""
        def _run():
            totals = _q(
                "SELECT (SELECT COUNT(*) FROM shows) shows,"
                "       (SELECT COUNT(*) FROM performances) perfs,"
                "       (SELECT COUNT(*) FROM songs) songs,"
                "       (SELECT COUNT(*) FROM plex_albums WHERE show_date IS NOT NULL) plex_dated,"
                "       (SELECT COUNT(*) FROM archive_recordings) arch_recordings,"
                "       (SELECT COUNT(DISTINCT show_date) FROM archive_recordings) arch_shows"
            )[0]

            top_songs = _q(
                "SELECT song_name, COUNT(*) n FROM performances "
                "GROUP BY song_name ORDER BY n DESC LIMIT 10"
            )
            top_venues = _q(
                "SELECT venue, city, state, COUNT(*) n FROM shows "
                "WHERE venue IS NOT NULL GROUP BY venue ORDER BY n DESC LIMIT 10"
            )
            years = _q(
                "SELECT substr(date,1,4) yr, COUNT(*) n FROM shows "
                "GROUP BY yr ORDER BY n DESC LIMIT 10"
            )

            arch_pct = totals["arch_shows"] / totals["shows"] * 100

            lines = [
                "Grateful Dead setlist DB",
                f"  {totals['shows']} shows  |  {totals['perfs']} performances  |  {totals['songs']} songs",
                f"  {totals['plex_dated']} shows in Plex library",
                f"  {totals['arch_recordings']:,} archive.org recordings ({totals['arch_shows']} shows, {arch_pct:.0f}% coverage)",
                "",
                "Top 10 most-played songs:",
            ]
            for r in top_songs:
                lines.append(f"  {r['n']:>4}x  {r['song_name']}")
            lines.append("")
            lines.append("Top 10 most-played venues:")
            for r in top_venues:
                lines.append(f"  {r['n']:>4}x  {_loc(r)}")
            lines.append("")
            lines.append("Most active years (top 10):")
            for r in years:
                lines.append(f"  {r['yr']}  {r['n']} shows")
            return "\n".join(lines)

        return await asyncio.to_thread(_run)


    @mcp.tool()
    async def dead_lore(query: str, k: int = 5, source: str | None = None) -> dict:
        """Raw semantic search over the Grateful Dead lore corpus.

        Returns top-k chunks of prose from Wikipedia, Light Into Ashes essays
        and primary-source clippings, Grateful Dead books, and the Good Ol'
        Grateful Deadcast transcripts, ranked by
        semantic similarity to the query.

        Args:
            query: Free-text search query.
            k: Number of chunks to return (default 5, max 20).
            source: Optional filter — one of 'wikipedia', 'lia_essays',
                    'lia_sources', 'book', 'deadcast'. Default None = all sources.

        Returns: {chunks: [{text, source, title, url, section, distance,
                  mentioned_dates, mentioned_songs, era}]}
        """
        def _run():
            k_clamped = max(1, min(int(k), 20))
            results = lore_query.search(query, k=k_clamped, source=source)
            return {
                "chunks": [
                    {
                        "text": r.text,
                        "source": r.source,
                        "title": r.title,
                        "url": r.url,
                        "section": r.section,
                        "distance": r.distance,
                        "mentioned_dates": r.mentioned_dates,
                        "mentioned_songs": r.mentioned_songs,
                        "era": r.era,
                    }
                    for r in results
                ]
            }
        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_top_versions(song_name: str, k: int = 10) -> dict:
        """Top community-voted versions of a Grateful Dead song.

        Pulls from the HeadyVersion community_votes table joined to your
        canonical shows/performances. Returns the highest-voted submissions
        with date, venue, blurb, vote count, and archive.org identifier
        when available.

        Args:
            song_name: Canonical song name as in dead.db.songs.name. Case-
                       insensitive; alias resolution via song_matcher.
            k: Number of top versions to return (default 10, max 50).
        """
        def _run():
            from lore.song_matcher import _gazetteer
            table, _, _ = _gazetteer()
            name_lower = song_name.lower()
            canonical = table.get(name_lower, (song_name, False))[0]

            k_clamped = max(1, min(int(k), 50))
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = conn.execute("""
                SELECT cv.show_date,
                       COALESCE(s.venue, cv.venue)   AS venue,
                       COALESCE(s.city,  cv.city)    AS city,
                       s.state, s.country,
                       cv.vote_score, cv.blurb, cv.heady_url,
                       ar.identifier                  AS archive_id,
                       sng.name                       AS song_canonical
                FROM community_votes cv
                LEFT JOIN songs   sng ON sng.uuid = cv.song_uuid
                LEFT JOIN shows   s   ON s.date   = cv.show_date
                LEFT JOIN archive_recordings ar ON ar.show_date = cv.show_date
                WHERE LOWER(sng.name) = LOWER(?) OR LOWER(cv.song_name) = LOWER(?)
                ORDER BY cv.vote_score DESC, ar.avg_rating DESC NULLS LAST
                LIMIT ?
            """, (canonical, song_name, k_clamped)).fetchall()
            conn.close()
            return {
                "song": canonical,
                "versions": [
                    {"date": r[0], "venue": r[1], "city": r[2],
                     "state": r[3], "country": r[4],
                     "vote_score": r[5], "blurb": r[6], "heady_url": r[7],
                     "archive_id": r[8]}
                    for r in rows
                ],
            }
        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_show_votes(date: str) -> dict:
        """Community-voted submissions from a single show.

        Returns every HeadyVersion submission for the given date, sorted by
        vote score. Useful for "what stood out about <date>" questions.

        Args:
            date: Show date in YYYY-MM-DD format.
        """
        def _run():
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = conn.execute("""
                SELECT cv.song_name, cv.vote_score, cv.blurb, cv.heady_url
                FROM community_votes cv
                WHERE cv.show_date = ?
                ORDER BY cv.vote_score DESC
            """, (date,)).fetchall()
            show_row = conn.execute("""
                SELECT venue, city, state, country
                FROM shows WHERE date = ?
            """, (date,)).fetchone()
            archive_rows = conn.execute("""
                SELECT identifier, avg_rating, num_reviews
                FROM archive_recordings WHERE show_date = ?
                ORDER BY avg_rating DESC NULLS LAST
            """, (date,)).fetchall()
            conn.close()
            return {
                "date": date,
                "show": ({"venue": show_row[0], "city": show_row[1],
                          "state": show_row[2], "country": show_row[3]}
                         if show_row else None),
                "archive_recordings": [
                    {"identifier": r[0], "avg_rating": r[1], "num_reviews": r[2]}
                    for r in archive_rows
                ],
                "votes": [
                    {"song": r[0], "vote_score": r[1], "blurb": r[2],
                     "heady_url": r[3]}
                    for r in rows
                ],
            }
        return await asyncio.to_thread(_run)

    @mcp.tool()
    async def dead_ask(question: str, k: int = 8) -> dict:
        """Lore router for narrative/insight questions about the Grateful Dead.

        Extracts entities (dates, songs, eras) from the question, runs hybrid
        retrieval over the lore corpus (entity filters + vector ranking), and
        returns evidence chunks AND suggested followup SQL-tool calls.

        The CALLER (i.e. you, the chat assistant reading this docstring) is
        expected to:
          1. Read the rag_chunks for context.
          2. Make followup calls to the suggested tools (or others as needed)
             for structured data.
          3. Synthesize a final answer drawing on both.

        This tool does NOT synthesize prose. It gathers evidence and points at
        the next moves. Useful for any question whose answer combines lore
        (why/how/context) with structured facts (what/when/who).

        Args:
            question: A free-text question about the Grateful Dead.
            k: Number of chunks to return after diversity caps (default 8).

        Returns:
            {
              question: <echo>,
              entities: {dates, years, songs, eras},
              rag_chunks: [{chunk_id, distance, text, source, title, url,
                            section, mentioned_dates, mentioned_songs, era,
                            document_metadata}],
              suggested_followups: [{tool, args, reason}],
              retrieval_mode: 'hybrid' | 'pure_vector'
            }
        """
        def _run():
            k_clamped = max(1, min(int(k), 20))
            return router_ask(question, k=k_clamped)
        return await asyncio.to_thread(_run)


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    conn = _connect()
    passed = failed = 0

    def check(name, result, *assertions):
        global passed, failed
        ok = all(a(result) for a in assertions)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        if not ok:
            print(f"       got: {result[:200]!r}")
            failed += 1
        else:
            passed += 1

    # dead_this_date: May 27 → exactly 2 shows
    from calendar import month_name as _mn
    r = "\n".join(
        f"{s['date']}"
        for s in [
            dict(r) for r in conn.execute(
                "SELECT s.date, COUNT(DISTINCT ar.identifier) archive_count "
                "FROM shows s LEFT JOIN archive_recordings ar ON ar.show_date=s.date "
                "WHERE strftime('%m-%d',s.date)='05-27' GROUP BY s.date ORDER BY s.date"
            ).fetchall()
        ]
    )
    dates_may27 = r.strip().splitlines()
    check("dead_this_date(5,27) → 2 shows",
          str(len(dates_may27)),
          lambda x: x == "2")

    # dead_this_date: Feb 30 → no-show message
    conn2 = _connect()
    r = [dict(r) for r in conn2.execute(
        "SELECT date FROM shows WHERE strftime('%m-%d',date)='02-30'"
    ).fetchall()]
    check("dead_this_date(2,30) → 0 shows", str(len(r)), lambda x: x == "0")

    # dead_show_recordings: Cornell 77 → 23 archive entries
    arch = [dict(r) for r in conn.execute(
        "SELECT * FROM archive_recordings WHERE show_date='1977-05-08'"
    ).fetchall()]
    check("dead_show_recordings('1977-05-08') → 23 archive",
          str(len(arch)), lambda x: x == "23")
    # top result is MATRIX or SBD
    sorted_arch = sorted(arch, key=lambda r: (
        0 if (r["num_reviews"] or 0) >= 3 else 1,
        -(r["avg_rating"] or 0),
        _src_rank(r["source_type"]),
        -(r["downloads"] or 0),
    ))
    check("dead_show_recordings('1977-05-08') top is MATRIX/SBD",
          sorted_arch[0]["source_type"],
          lambda x: x in ("MATRIX", "SBD"))

    # dead_show_recordings: 1980-01-01 → not a show
    r1980 = conn.execute("SELECT 1 FROM shows WHERE date='1980-01-01'").fetchone()
    check("1980-01-01 not in shows", str(r1980), lambda x: x == "None")

    # dead_show_recordings: 1995-07-09 → Soldier Field
    r1995 = conn.execute("SELECT venue FROM shows WHERE date='1995-07-09'").fetchone()
    check("1995-07-09 in shows (Soldier Field)",
          r1995[0] if r1995 else "", lambda x: "Soldier" in x)

    # dead_song_stats: Dark Star → first 1968, last 1994
    ds = conn.execute(
        "SELECT MIN(show_date) first, MAX(show_date) last, COUNT(*) n "
        "FROM performances WHERE LOWER(song_name)='dark star'"
    ).fetchone()
    check("Dark Star first played 1968-xx", ds[0][:4], lambda x: x == "1968")
    check("Dark Star last played 1994-xx", ds[1][:4], lambda x: x == "1994")

    # dead_song_stats: Dire Wolf → >200 performances
    dw = conn.execute(
        "SELECT COUNT(*) FROM performances WHERE LOWER(song_name) LIKE '%dire wolf%'"
    ).fetchone()[0]
    check("Dire Wolf >200 performances", str(dw), lambda x: int(x) > 200)

    # dead_song_stats: Xyzzy → no match, has suggestions (or empty)
    matched, suggestions = _find_song(conn, "Xyzzy")
    check("Xyzzy → no match", str(matched), lambda x: x == "None")

    # dead_segues: Scarlet > Fire → 100+ occurrences
    cnt = conn.execute(
        "SELECT COUNT(*) FROM performances p1 "
        "JOIN performances p2 ON p2.show_date=p1.show_date AND p2.set_num=p1.set_num "
        "  AND p2.position=p1.position+1 "
        "WHERE p1.song_name LIKE '%Scarlet%' AND p1.segued_out=1 "
        "  AND p2.song_name LIKE '%Fire on the Mountain%'"
    ).fetchone()[0]
    check("Scarlet > Fire 100+ occurrences", str(cnt), lambda x: int(x) >= 100)

    # dead_segues: Help on the Way → Slipknot! as top target
    top = conn.execute(
        "SELECT p2.song_name FROM performances p1 "
        "JOIN performances p2 ON p2.show_date=p1.show_date AND p2.set_num=p1.set_num "
        "  AND p2.position=p1.position+1 "
        "WHERE LOWER(p1.song_name) LIKE '%help on the way%' AND p1.segued_out=1 "
        "GROUP BY p2.song_name ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    check("Help on the Way → Slipknot! top target",
          top[0] if top else "", lambda x: "Slipknot" in x)

    # dead_run: 1977-05-08 → ≥5 connected dates
    all_dates = [r[0] for r in conn.execute("SELECT date FROM shows WHERE length(date)=10 ORDER BY date").fetchall()]
    cur_run: list = [all_dates[0]]
    target_run: list = []
    for i in range(1, len(all_dates)):
        gap = (dt_date.fromisoformat(all_dates[i]) - dt_date.fromisoformat(all_dates[i - 1])).days
        if gap <= 3:
            cur_run.append(all_dates[i])
        else:
            if "1977-05-08" in cur_run:
                target_run = cur_run
                break
            cur_run = [all_dates[i]]
    check("dead_run('1977-05-08') ≥5 dates", str(len(target_run)), lambda x: int(x) >= 5)

    # dead_run: 1995-07-09 → Soldier Field run ≥3 dates
    cur_run = [all_dates[0]]
    sf_run: list = []
    for i in range(1, len(all_dates)):
        gap = (dt_date.fromisoformat(all_dates[i]) - dt_date.fromisoformat(all_dates[i - 1])).days
        if gap <= 3:
            cur_run.append(all_dates[i])
        else:
            if "1995-07-09" in cur_run:
                sf_run = cur_run
                break
            cur_run = [all_dates[i]]
    if not sf_run:
        sf_run = cur_run
    check("dead_run('1995-07-09') ≥3 dates (Soldier Field run)",
          str(len(sf_run)), lambda x: int(x) >= 3)

    # dead_stats: LIMIT 10 years, 1969 or 1970 at top
    top_years = _q("SELECT substr(date,1,4) yr, COUNT(*) n FROM shows GROUP BY yr ORDER BY n DESC LIMIT 10")
    check("dead_stats top year is 1969 or 1970",
          top_years[0]["yr"], lambda x: x in ("1969", "1970"))
    check("dead_stats shows 10 years", str(len(top_years)), lambda x: x == "10")

    conn.close()
    conn2.close()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
