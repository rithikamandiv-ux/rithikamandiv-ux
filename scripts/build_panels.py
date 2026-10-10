#!/usr/bin/env python3
"""Build the themed Metrics and Contribution Activity panels for the profile README.

Run by .github/workflows/snake.yml after the snake has been generated.

Why this script exists
----------------------
An SVG shown in a README cannot load other images, so third-party stat cards
cannot be placed inside a themed box. This script instead reads the numbers
from the GitHub API and draws the cards itself, inside the same panel design
as the other sections. It also wraps the generated snake in a matching panel.

Inputs (environment variables)
    GITHUB_TOKEN  token used to call the GitHub API (provided by GitHub Actions)
    GH_USER       GitHub login to read statistics for
    OUT_DIR       folder holding github-snake-dark.svg; panels are written here

Outputs
    OUT_DIR/metrics.svg
    OUT_DIR/activity.svg

Only the Python standard library is used, so the workflow needs no install step.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOGIN = os.environ.get("GH_USER", "rithikamandiv-ux")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
OUT_DIR = Path(os.environ.get("OUT_DIR", "dist"))
SNAKE_FILE = OUT_DIR / "github-snake-dark.svg"

W = 1200            # panel width in SVG units
HEAD = 132          # height of the header strip
SERIF = "'Hiragino Mincho ProN','Yu Mincho','Noto Serif CJK JP','Noto Serif JP','Songti SC',serif"
SANS = "'Segoe UI','Helvetica Neue',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
PINK, CYAN, WHITE, LAV = "#F472B6", "#5EEAD4", "#FFFFFF", "#B8A9D9"
CARD = 'fill="#0B0918" fill-opacity="0.82" stroke="#4A3470" stroke-width="1.2"'


# --------------------------------------------------------------------------
# GitHub API access
# --------------------------------------------------------------------------
def _request(url: str, payload: dict | None = None) -> dict:
    """Send one authenticated request and return the decoded JSON body."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"{LOGIN}-profile-panels"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def graphql(query: str, **variables) -> dict:
    """Run a GraphQL query. Partial results are accepted; a response with no data is an error."""
    body = _request("https://api.github.com/graphql", {"query": query, "variables": variables})
    if body.get("errors"):
        print("GraphQL warnings:", json.dumps(body["errors"])[:500], file=sys.stderr)
    if not body.get("data"):
        raise RuntimeError("GitHub GraphQL API returned no data")
    return body["data"]


def fetch_contribution_days() -> dict[dt.date, int]:
    """Return {date: contribution count} for every day since the account's first active year."""
    years = graphql(
        "query($login:String!){user(login:$login){contributionsCollection{contributionYears}}}",
        login=LOGIN,
    )["user"]["contributionsCollection"]["contributionYears"]
    query = """
    query($login:String!,$from:DateTime!,$to:DateTime!){
      user(login:$login){
        contributionsCollection(from:$from,to:$to){
          contributionCalendar{weeks{contributionDays{date contributionCount}}}
        }
      }
    }"""
    days: dict[dt.date, int] = {}
    for year in years:
        cal = graphql(query, login=LOGIN, **{"from": f"{year}-01-01T00:00:00Z", "to": f"{year}-12-31T23:59:59Z"})
        for week in cal["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[dt.date.fromisoformat(day["date"])] = day["contributionCount"]
    return days


def compute_streaks(days: dict[dt.date, int], today: dt.date) -> dict:
    """Work out total contributions, the current streak and the longest streak.

    A day with no contributions ends a streak. Today is the one exception: an
    empty "today" does not end the current streak, because the day is not over.
    """
    past = sorted(d for d in days if d <= today)
    total = sum(days[d] for d in past)
    first = next((d for d in past if days[d] > 0), None)

    longest = {"length": 0, "start": None, "end": None}
    run_start, run_len = None, 0
    for d in past:
        if days[d] > 0:
            run_start = run_start or d
            run_len += 1
            if run_len > longest["length"]:
                longest = {"length": run_len, "start": run_start, "end": d}
        else:
            run_start, run_len = None, 0

    cursor = today if days.get(today, 0) > 0 else today - dt.timedelta(days=1)
    current = {"length": 0, "start": None, "end": None}
    while days.get(cursor, 0) > 0:
        current["length"] += 1
        current["start"] = cursor
        current["end"] = current["end"] or cursor
        cursor -= dt.timedelta(days=1)
    return {"total": total, "first": first, "current": current, "longest": longest}


# GitHub's own colours for common languages (the REST API does not return them).
LANGUAGE_COLOURS = {
    "TypeScript": "#3178C6", "JavaScript": "#F1E05A", "Java": "#B07219", "Python": "#3572A5",
    "C#": "#9B7BFF", "Go": "#00ADD8", "CSS": "#8E6FD8", "HTML": "#E34C26", "PHP": "#7A86B8",
    "Kotlin": "#A97BFF", "Rust": "#DEA584", "Swift": "#F05138", "Shell": "#89E051",
    "Dockerfile": "#5B8FA3", "EJS": "#D4427A", "Jupyter Notebook": "#DA5B0B", "SCSS": "#C6538C",
    "C++": "#F34B7D", "C": "#8A8A8A", "PLpgSQL": "#5B8FC7", "Makefile": "#6FA83A",
}


def rest(path: str) -> dict | list:
    """Call the GitHub REST API. Public data here is readable by the workflow token."""
    return _request("https://api.github.com" + path)


def search_count(kind: str, query: str) -> int:
    """Number of results for a search, for example all pull requests opened by the user."""
    return rest(f"/search/{kind}?per_page=1&q=" + urllib.parse.quote(query)).get("total_count", 0)


def fetch_stats() -> dict:
    """Return profile statistics and the most used languages.

    The REST API is used here on purpose. The token GitHub Actions provides is
    limited to the repository the workflow runs in, and the GraphQL API refuses
    to return details of the user's other repositories to it ("Resource not
    accessible by integration"). The REST API serves the same public data.
    """
    followers = rest(f"/users/{LOGIN}").get("followers", 0)

    repos, page = [], 1
    while True:
        batch = rest(f"/users/{LOGIN}/repos?type=owner&per_page=100&page={page}")
        repos += [r for r in batch if not r.get("fork")]
        if len(batch) < 100:
            break
        page += 1

    sizes: dict[str, int] = {}
    for repo in repos:
        for name, size in rest(f"/repos/{repo['full_name']}/languages").items():
            sizes[name] = sizes.get(name, 0) + size
    top = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:8]
    shown_total = sum(size for _, size in top) or 1
    languages = [{"name": name, "color": LANGUAGE_COLOURS.get(name, "#8B949E"), "percent": 100 * size / shown_total}
                 for name, size in top]

    # Optional extras. If the token may not read them, the panel still builds.
    reviews = contributed_to = 0
    try:
        extra = graphql(
            """
            query($login:String!){
              user(login:$login){
                contributionsCollection{totalPullRequestReviewContributions}
                repositoriesContributedTo(first:1,contributionTypes:[COMMIT,ISSUE,PULL_REQUEST,REPOSITORY]){totalCount}
              }
            }""",
            login=LOGIN,
        ).get("user") or {}
        reviews = (extra.get("contributionsCollection") or {}).get("totalPullRequestReviewContributions") or 0
        contributed_to = (extra.get("repositoriesContributedTo") or {}).get("totalCount") or 0
    except (RuntimeError, urllib.error.URLError, ValueError) as exc:
        print("Optional statistics unavailable:", exc, file=sys.stderr)

    return {
        "stars": sum(r.get("stargazers_count", 0) for r in repos),
        "commits": search_count("commits", f"author:{LOGIN}"),
        "prs": search_count("issues", f"author:{LOGIN} type:pr"),
        "issues": search_count("issues", f"author:{LOGIN} type:issue"),
        "contributed_to": contributed_to,
        "reviews": reviews,
        "followers": followers,
        "languages": languages,
    }


def rank(stats: dict) -> tuple[str, float]:
    """Grade the profile from S to C.

    Each figure is compared with a typical ("median") value and squashed into
    the range 0 to 1, then the results are combined with weights. This follows
    the public formula of the open-source github-readme-stats project, so the
    grade matches the card it replaces. Returns (grade, percentile); a lower
    percentile is better.
    """
    def exponential(x: float) -> float:
        return 1 - 2 ** -x

    def log_normal(x: float) -> float:
        return x / (1 + x)

    parts = [  # (weight, score)
        (2, exponential(stats["commits"] / 1000)),
        (3, exponential(stats["prs"] / 50)),
        (1, exponential(stats["issues"] / 25)),
        (1, exponential(stats["reviews"] / 2)),
        (4, log_normal(stats["stars"] / 50)),
        (1, log_normal(stats["followers"] / 10)),
    ]
    percentile = (1 - sum(w * s for w, s in parts) / sum(w for w, _ in parts)) * 100
    grades = [(1, "S"), (12.5, "A+"), (25, "A"), (37.5, "A-"), (50, "B+"), (62.5, "B"), (75, "B-"), (87.5, "C+"), (100, "C")]
    return next(g for limit, g in grades if percentile <= limit), percentile


# --------------------------------------------------------------------------
# SVG drawing helpers (shared look with the other panels in assets/)
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;")


def short(n: int) -> str:
    """1234 -> '1.2k'"""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def petals(seed: int, n: int, height: int, duration=(10, 20)) -> str:
    """Falling petals. A fixed seed keeps the pattern identical between runs."""
    rng = random.Random(seed)
    shape = "M0,-7 C4.5,-4 5.5,2 0,7 C-5.5,2 -4.5,-4 0,-7 Z"
    out = []
    for _ in range(n):
        x = rng.uniform(20, W - 20)
        d1, d2 = rng.uniform(-45, 45), rng.uniform(-45, 45)
        dur = rng.uniform(*duration)
        begin = -rng.uniform(0, dur)
        rot = rng.uniform(0, 360)
        spin = rng.choice([-1, 1]) * rng.uniform(180, 420)
        colour = rng.choice(["#F9A8D4", "#F472B6", "#FBCFE8", "#E879A9", "#C4B5FD"])
        out.append(
            f'<g><animateTransform attributeName="transform" type="translate" '
            f'values="{x:.0f},-16;{x + d1:.0f},{height * 0.5:.0f};{x + d2:.0f},{height + 18}" '
            f'dur="{dur:.1f}s" begin="{begin:.1f}s" repeatCount="indefinite"/>'
            f'<path d="{shape}" fill="{colour}" opacity="{rng.uniform(0.35, 0.8):.2f}" transform="scale({rng.uniform(0.95, 1.6):.2f})">'
            f'<animateTransform attributeName="transform" type="rotate" additive="sum" '
            f'values="{rot:.0f};{rot + spin:.0f}" dur="{dur:.1f}s" repeatCount="indefinite"/></path></g>')
    return "\n    ".join(out)


def panel(height: int, label: str, seed: int, stamp: str, kanji: str, romaji: str, title: str, index: int, body: str) -> str:
    """Wrap `body` in the themed frame: gradient background, header strip, petals and border."""
    waves = "".join(f'<circle cx="{cx}" cy="{cy}" r="{r}"/>' for cx, cy in ((20, 0), (0, 10), (40, 10), (20, 20)) for r in (20, 13, 6))
    kx = 150
    tx = kx + 54 * len(kanji) + 26
    ul = tx + len(title) * 19 + 60
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" height="{height}" role="img" aria-label="{esc(label)}">
  <title>{esc(label)}</title>
  <style>@media (prefers-reduced-motion: reduce) {{ .petals {{ display: none; }} }}</style>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0D0B1E"/><stop offset="0.55" stop-color="#1B1033"/><stop offset="1" stop-color="#2B1442"/></linearGradient>
    <linearGradient id="line" x1="0" x2="1"><stop offset="0" stop-color="#E11D48"/><stop offset="0.5" stop-color="{PINK}"/><stop offset="1" stop-color="{PINK}" stop-opacity="0"/></linearGradient>
    <linearGradient id="sep" x1="0" x2="1"><stop offset="0" stop-color="#4A3470" stop-opacity="0"/><stop offset="0.5" stop-color="#6B4FA0"/><stop offset="1" stop-color="#4A3470" stop-opacity="0"/></linearGradient>
    <linearGradient id="fade" x1="0" y1="0" x2="1" y2="0"><stop offset="0.35" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff" stop-opacity="1"/></linearGradient>
    <mask id="m"><rect width="{W}" height="{height}" fill="url(#fade)"/></mask>
    <pattern id="waves" width="40" height="20" patternUnits="userSpaceOnUse"><g fill="#1B1033" stroke="#8B6FC4" stroke-width="1">{waves}</g></pattern>
    <clipPath id="clip"><rect x="1" y="1" width="{W - 2}" height="{height - 2}" rx="14"/></clipPath>
    <clipPath id="langbar"><rect x="0" y="0" width="344" height="10" rx="5"/></clipPath>
    <filter id="glow" x="-20%" y="-40%" width="140%" height="180%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    <filter id="rough" x="-10%" y="-10%" width="120%" height="120%"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="{seed}" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="2.2"/></filter>
  </defs>
  <g clip-path="url(#clip)">
    <rect width="{W}" height="{height}" fill="url(#bg)"/>
    <rect width="{W}" height="{height}" fill="url(#waves)" opacity="0.16" mask="url(#m)"/>
    <text x="{W - 40}" y="118" text-anchor="end" font-family="{SERIF}" font-weight="700" font-size="150" fill="#C4B5FD" opacity="0.07">{kanji}</text>
    <g transform="translate(36,31) rotate(-5 35 35)" filter="url(#rough)"><rect width="70" height="70" rx="9" fill="#D7263D"/><rect x="5" y="5" width="60" height="60" rx="6" fill="none" stroke="#FFE4E6" stroke-width="1.6" opacity="0.85"/><text x="35" y="50" text-anchor="middle" font-family="{SERIF}" font-weight="700" font-size="40" fill="#FFF1F2">{stamp}</text></g>
    <text x="{kx}" y="88" font-family="{SERIF}" font-weight="700" font-size="54" fill="{WHITE}" filter="url(#glow)">{kanji}</text>
    <text x="{tx}" y="54" font-family="{SANS}" font-weight="700" font-size="13" letter-spacing="4" fill="{PINK}">{romaji}</text>
    <text x="{tx}" y="88" font-family="{SANS}" font-weight="800" font-size="25" letter-spacing="5" fill="{WHITE}">{title}</text>
    <path d="M{kx - 4},106 Q{(kx + ul) // 2},100 {ul},105" fill="none" stroke="url(#line)" stroke-width="3.5" stroke-linecap="round"/>
    <path d="M{kx + 20},111 Q{(kx + ul) // 2},107 {ul - 80},110" fill="none" stroke="url(#line)" stroke-width="1.2" stroke-linecap="round" opacity="0.7"/>
    <text x="{W - 34}" y="34" text-anchor="end" font-family="{MONO}" font-size="13" letter-spacing="3" fill="#A99BC9">{index:02d} / 04</text>
    <rect x="40" y="{HEAD}" width="{W - 80}" height="1.5" fill="url(#sep)"/>
{body}
    <g class="petals">
    {petals(seed + 300, max(10, height // 34), height)}
    </g>
  </g>
  <rect x="1" y="1" width="{W - 2}" height="{height - 2}" rx="14" fill="none" stroke="#4A3470" stroke-width="1.5"/>
</svg>
'''


def text(x, y, content, size=16, fill=WHITE, weight=400, anchor="start") -> str:
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{SANS}" font-weight="{weight}" '
            f'font-size="{size}" fill="{fill}">{esc(content)}</text>')


def fmt_date(day: dt.date | None, today: dt.date) -> str:
    """'Oct 5' for this year, 'Oct 21, 2025' for other years."""
    if day is None:
        return "-"
    label = f"{day:%b} {day.day}"
    return label if day.year == today.year else f"{label}, {day.year}"


def fmt_range(streak: dict, today: dt.date) -> str:
    if not streak["length"]:
        return "-"
    return f'{fmt_date(streak["start"], today)} - {fmt_date(streak["end"], today)}'


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------
def build_metrics(streaks: dict, stats: dict, today: dt.date) -> str:
    parts: list[str] = []

    # --- streak card -------------------------------------------------------
    cx, cy, cw, ch = 220, HEAD + 30, 760, 190
    left, mid, right = cx + cw / 6, cx + cw / 2, cx + cw * 5 / 6
    parts.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="12" {CARD}/>')
    for sx in (cx + cw / 3, cx + cw * 2 / 3):
        parts.append(f'<rect x="{sx}" y="{cy + 24}" width="1.5" height="{ch - 48}" fill="{PINK}" opacity="0.8"/>')
    parts += [
        text(left, cy + 84, f'{streaks["total"]:,}', 40, WHITE, 700, "middle"),
        text(left, cy + 120, "Total Contributions", 18, CYAN, 400, "middle"),
        text(left, cy + 150, f'{fmt_date(streaks["first"], today)} - Present', 14, LAV, 400, "middle"),
        f'<circle cx="{mid}" cy="{cy + 72}" r="42" fill="none" stroke="{PINK}" stroke-width="5"/>',
        f'<rect x="{mid - 13}" y="{cy + 14}" width="26" height="24" fill="#0F0C22"/>',
        f'<path transform="translate({mid - 9},{cy + 12}) scale(0.75)" fill="{CYAN}" d="M12 2c1 3-1 5-3 7-2 2-4 4-4 7a7 7 0 0 0 14 0c0-2-1-4-2-5-1 2-2 3-3 3 1-4 0-9-2-12Zm0 19a3 3 0 0 1-3-3c0-1 1-2 2-3 1 1 4 2 4 4a3 3 0 0 1-3 2Z"/>',
        text(mid, cy + 84, streaks["current"]["length"], 34, WHITE, 700, "middle"),
        text(mid, cy + 142, "Current Streak", 18, PINK, 700, "middle"),
        text(mid, cy + 168, fmt_range(streaks["current"], today), 14, LAV, 400, "middle"),
        text(right, cy + 84, streaks["longest"]["length"], 40, WHITE, 700, "middle"),
        text(right, cy + 120, "Longest Streak", 18, CYAN, 400, "middle"),
        text(right, cy + 150, fmt_range(streaks["longest"], today), 14, LAV, 400, "middle"),
    ]

    # --- power level card --------------------------------------------------
    px, py, pw, ph = 100, cy + ch + 24, 580, 236
    grade, percentile = rank(stats)
    ring_r = 48
    circumference = 2 * 3.14159265 * ring_r
    filled = circumference * (100 - percentile) / 100
    parts.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="12" {CARD}/>')
    parts.append(text(px + 28, py + 46, "戦闘力 · Power Level", 21, PINK, 700))
    rows = [("Total Stars Earned:", stats["stars"]), ("Total Commits:", stats["commits"]), ("Total PRs:", stats["prs"]),
            ("Total Issues:", stats["issues"]), ("Contributed to (last year):", stats["contributed_to"])]
    for i, (label, value) in enumerate(rows):
        ry = py + 88 + i * 30
        parts.append(f'<rect x="{px + 28}" y="{ry - 11}" width="10" height="10" rx="2" fill="{CYAN}" transform="rotate(45 {px + 33} {ry - 6})"/>')
        parts.append(text(px + 52, ry, label, 16, WHITE, 700))
        parts.append(text(px + 320, ry, short(value), 16, WHITE, 700))
    rcx, rcy = px + 470, py + 138
    parts += [
        f'<circle cx="{rcx}" cy="{rcy}" r="{ring_r}" fill="none" stroke="#3B2A5C" stroke-width="7"/>',
        f'<circle cx="{rcx}" cy="{rcy}" r="{ring_r}" fill="none" stroke="{PINK}" stroke-width="7" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.1f} {circumference:.1f}" transform="rotate(-90 {rcx} {rcy})"/>',
        text(rcx, rcy + 11, grade, 30, WHITE, 800, "middle"),
    ]

    # --- languages card ----------------------------------------------------
    lx, lw = px + pw + 20, 400
    parts.append(f'<rect x="{lx}" y="{py}" width="{lw}" height="{ph}" rx="12" {CARD}/>')
    parts.append(text(lx + 28, py + 46, "属性 · Elemental Affinities", 21, PINK, 700))
    bar_w, offset, segments = 344, 0.0, []
    for lang in stats["languages"]:
        seg = bar_w * lang["percent"] / 100
        segments.append(f'<rect x="{offset:.1f}" y="0" width="{seg:.1f}" height="10" fill="{lang["color"]}"/>')
        offset += seg
    parts.append(f'<g transform="translate({lx + 28},{py + 68})" clip-path="url(#langbar)">{"".join(segments)}</g>')
    half = (len(stats["languages"]) + 1) // 2
    for i, lang in enumerate(stats["languages"]):
        col, row = divmod(i, half)
        tx, ty = lx + 28 + col * 178, py + 116 + row * 30
        parts.append(f'<circle cx="{tx + 6}" cy="{ty - 5}" r="6" fill="{lang["color"]}"/>')
        parts.append(text(tx + 20, ty, f'{lang["name"]} {lang["percent"]:.2f}%', 15, WHITE, 500))

    height = py + ph + 40
    label = (f'Metrics. {streaks["total"]} total contributions, current streak {streaks["current"]["length"]} days, '
             f'longest streak {streaks["longest"]["length"]} days. Power level {grade}: {stats["stars"]} stars, '
             f'{stats["commits"]} commits, {stats["prs"]} pull requests, {stats["issues"]} issues. '
             f'Most used languages: {", ".join(l["name"] for l in stats["languages"])}.')
    body = "\n".join("    " + p for p in parts)
    return panel(height, label, 21, "参", "戦闘力", "SENTŌRYOKU · せんとうりょく", "METRICS", 3, body)


def build_activity(snake_svg: str) -> str:
    """Place the generated snake inside a themed panel.

    The snake file is itself an SVG, and one SVG may contain another, so its
    contents are copied in as a nested <svg> element and scaled to fit.
    """
    root = re.search(r"<svg\b[^>]*>", snake_svg)
    view = re.search(r'viewBox="([^"]+)"', root.group(0)) if root else None
    if not root or not view:
        raise RuntimeError("Snake file is not in the expected SVG format")
    _, _, vw, vh = (float(v) for v in view.group(1).split())
    inner = snake_svg[root.end(): snake_svg.rindex("</svg>")]
    width = W - 80
    snake_h = round(width * vh / vw)
    top = HEAD + 22
    body = f'    <svg x="40" y="{top}" width="{width}" height="{snake_h}" viewBox="{view.group(1)}">{inner}</svg>'
    return panel(top + snake_h + 26, "Contribution Activity: animated snake eating the contribution graph",
                 28, "肆", "草", "KUSA · くさ", "CONTRIBUTION ACTIVITY", 4, body)


def main() -> None:
    if not TOKEN:
        sys.exit("GITHUB_TOKEN is not set")
    if not SNAKE_FILE.exists():
        sys.exit(f"{SNAKE_FILE} not found; the snake must be generated before this script runs")
    today = dt.datetime.now(dt.timezone.utc).date()
    streaks = compute_streaks(fetch_contribution_days(), today)
    stats = fetch_stats()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "metrics.svg").write_text(build_metrics(streaks, stats, today), encoding="utf-8")
    (OUT_DIR / "activity.svg").write_text(build_activity(SNAKE_FILE.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"Wrote metrics.svg and activity.svg to {OUT_DIR}/ "
          f'(total {streaks["total"]}, current streak {streaks["current"]["length"]}, grade {rank(stats)[0]})')


if __name__ == "__main__":
    main()
