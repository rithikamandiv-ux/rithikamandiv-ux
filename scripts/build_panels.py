#!/usr/bin/env python3
"""Build the themed profile panel for the profile README.

Run by .github/workflows/snake.yml after the snake has been generated.

Why this script exists
----------------------
An SVG shown in a README cannot load other images, so third-party stat cards
and the generated snake cannot simply be placed inside a themed box. This
script instead reads the numbers from the GitHub API, draws every section
itself, and stacks them inside ONE frame so the profile reads as a single
continuous panel:

    Intro -> Core Capabilities -> Tech Stack -> Metrics -> Contribution Activity

Inputs
    GITHUB_TOKEN                  token for the GitHub API (provided by GitHub Actions)
    GH_USER                       GitHub login to read statistics for
    OUT_DIR                       folder holding github-snake-dark.svg; output goes here
    scripts/profile_content.json  name, intro, capabilities and tech stack entries (edit this to add a technology)
    assets/banner.png             banner picture shown in the intro

Output
    OUT_DIR/profile.svg

Only the Python standard library is used, so the workflow needs no install step.
"""
from __future__ import annotations

import base64
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
CONTENT_FILE = Path(__file__).with_name("profile_content.json")

W = 1200            # panel width in SVG units
HEAD = 132          # height of each section's header strip
DIVIDER = 64        # vertical space taken by the sakura divider between sections
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
# SVG drawing helpers
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;")


def short(n: int) -> str:
    """1234 -> '1.2k'"""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


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


def petals(seed: int, height: int) -> str:
    """Falling petals over the whole panel.

    A fixed seed keeps the pattern identical between runs. The fall time grows
    with the panel height so petals drift at the same gentle speed in a tall panel.
    """
    rng = random.Random(seed)
    shape = "M0,-7 C4.5,-4 5.5,2 0,7 C-5.5,2 -4.5,-4 0,-7 Z"
    out = []
    for _ in range(max(12, height // 40)):
        x = rng.uniform(20, W - 20)
        d1, d2 = rng.uniform(-60, 60), rng.uniform(-60, 60)
        dur = height / rng.uniform(32, 60)          # seconds to fall the full height
        begin = -rng.uniform(0, dur)                # negative start = already mid-fall on load
        rot = rng.uniform(0, 360)
        spin = rng.choice([-1, 1]) * rng.uniform(360, 1440)
        colour = rng.choice(["#F9A8D4", "#F472B6", "#FBCFE8", "#E879A9", "#C4B5FD"])
        out.append(
            f'<g><animateTransform attributeName="transform" type="translate" '
            f'values="{x:.0f},-16;{x + d1:.0f},{height * 0.5:.0f};{x + d2:.0f},{height + 18}" '
            f'dur="{dur:.1f}s" begin="{begin:.1f}s" repeatCount="indefinite"/>'
            f'<path d="{shape}" fill="{colour}" opacity="{rng.uniform(0.35, 0.8):.2f}" transform="scale({rng.uniform(0.95, 1.6):.2f})">'
            f'<animateTransform attributeName="transform" type="rotate" additive="sum" '
            f'values="{rot:.0f};{rot + spin:.0f}" dur="{dur:.1f}s" repeatCount="indefinite"/></path></g>')
    return "\n    ".join(out)


def section_header(stamp: str, kanji: str, romaji: str, title: str) -> str:
    """Header strip of one section: red stamp, kanji and title."""
    kx = 150
    tx = kx + 54 * len(kanji) + 26
    ul = tx + len(title) * 19 + 60
    return f'''<text x="{W - 40}" y="118" text-anchor="end" font-family="{SERIF}" font-weight="700" font-size="150" fill="#C4B5FD" opacity="0.07">{kanji}</text>
    <g transform="translate(36,31) rotate(-5 35 35)" filter="url(#rough)"><rect width="70" height="70" rx="9" fill="#D7263D"/><rect x="5" y="5" width="60" height="60" rx="6" fill="none" stroke="#FFE4E6" stroke-width="1.6" opacity="0.85"/><text x="35" y="50" text-anchor="middle" font-family="{SERIF}" font-weight="700" font-size="40" fill="#FFF1F2">{stamp}</text></g>
    <text x="{kx}" y="88" font-family="{SERIF}" font-weight="700" font-size="54" fill="{WHITE}" filter="url(#glow)">{kanji}</text>
    <text x="{tx}" y="54" font-family="{SANS}" font-weight="700" font-size="13" letter-spacing="4" fill="{PINK}">{romaji}</text>
    <text x="{tx}" y="88" font-family="{SANS}" font-weight="800" font-size="25" letter-spacing="5" fill="{WHITE}">{title}</text>
    <path d="M{kx - 4},106 Q{(kx + ul) // 2},100 {ul},105" fill="none" stroke="url(#line)" stroke-width="3.5" stroke-linecap="round"/>
    <path d="M{kx + 20},111 Q{(kx + ul) // 2},107 {ul - 80},110" fill="none" stroke="url(#line)" stroke-width="1.2" stroke-linecap="round" opacity="0.7"/>
    <rect x="40" y="{HEAD}" width="{W - 80}" height="1.5" fill="url(#sep)"/>'''


def sakura_divider(y: float) -> str:
    """The flower line drawn between two sections, centred on height y."""
    flower = "".join(f'<path d="M0,-2 C5,-9 4,-15 0,-17 C-4,-15 -5,-9 0,-2 Z" transform="rotate({a})"/>' for a in range(0, 360, 72))
    return f'''<g transform="translate(0,{y})">
      <rect x="60" y="-1" width="500" height="2" fill="url(#fl)"/><rect x="640" y="-1" width="500" height="2" fill="url(#fr)"/>
      <rect x="566" y="-4.5" width="9" height="9" fill="{PINK}" transform="rotate(45 570.5 0)"/><rect x="625" y="-4.5" width="9" height="9" fill="{CYAN}" transform="rotate(45 629.5 0)"/>
      <g transform="translate(600,0)"><g fill="{PINK}">{flower}<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="24s" repeatCount="indefinite"/></g><circle r="3.2" fill="#FDE68A"/></g>
    </g>'''


def pill_width(item: dict, padding: int) -> int:
    """Width of a chip: fixed padding plus the label width (measured, or estimated for new entries)."""
    return padding + item.get("text_width", round(len(item["label"]) * 9.2))


# --------------------------------------------------------------------------
# Section bodies. Each returns (svg, height) with y measured from the section top.
# --------------------------------------------------------------------------
def hero_body(profile: dict) -> tuple[str, int]:
    """Top section: name, role line, banner picture and the intro paragraph.

    An SVG shown as an image cannot load a separate picture file, so the banner
    is read from the repository and embedded as text (a base64 "data URI").
    """
    banner = Path(__file__).resolve().parent.parent / profile["banner"]
    encoded = base64.b64encode(banner.read_bytes()).decode()
    bw = W - 80
    bh = round(bw * profile["banner_height"] / profile["banner_width"])
    by = 168
    roles = f'</tspan><tspan fill="{CYAN}" dx="12">|</tspan><tspan dx="12">'.join(esc(r) for r in profile["roles"])
    parts = [
        f'<text x="{W - 40}" y="124" text-anchor="end" font-family="{SERIF}" font-weight="700" font-size="120" fill="#C4B5FD" opacity="0.07">自己紹介</text>',
        f'<text x="60" y="52" font-family="{SANS}" font-weight="700" font-size="13" letter-spacing="4" fill="{PINK}">JIKOSHŌKAI · じこしょうかい</text>',
        f'<text x="58" y="104" font-family="{SANS}" font-weight="800" font-size="50" letter-spacing="4" fill="{WHITE}" filter="url(#glow)">{esc(profile["name"])}</text>',
        '<path d="M56,120 Q330,113 640,119" fill="none" stroke="url(#line)" stroke-width="3.5" stroke-linecap="round"/>',
        f'<text x="60" y="150" font-family="{SANS}" font-weight="700" font-size="18" letter-spacing="1.5" fill="#F4EEFF"><tspan>{roles}</tspan></text>',
        f'<image x="40" y="{by}" width="{bw}" height="{bh}" preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{encoded}"/>',
    ]
    ty = by + bh + 44
    for i, line in enumerate(profile["intro_lines"]):
        parts.append(text(600, ty + i * 31, line, 19, "#E9E1FF", 400, "middle"))
    return "\n    ".join(parts), ty + (len(profile["intro_lines"]) - 1) * 31 + 34


def capabilities_body(items: list[dict]) -> tuple[str, int]:
    chip_h, gap, parts, y = 42, 12, [], HEAD + 38
    half = (len(items) + 1) // 2
    for row in (items[:half], items[half:]):
        widths = [pill_width(i, 16 + 12 + 10 + 18) for i in row]
        x = (W - sum(widths) - gap * (len(row) - 1)) / 2
        for item, w in zip(row, widths):
            colour = PINK if items.index(item) % 2 == 0 else CYAN
            parts.append(
                f'<g transform="translate({x:.1f},{y})"><rect width="{w}" height="{chip_h}" rx="9" {CARD}/>'
                f'<rect x="16.5" y="{chip_h / 2 - 5.5}" width="11" height="11" rx="2" fill="{colour}" transform="rotate(45 22 {chip_h / 2})"/>'
                f'<text x="38" y="{chip_h / 2 + 5.5}" font-family="{SANS}" font-weight="600" font-size="16" fill="#F4EEFF" '
                f'textLength="{w - 56}" lengthAdjust="spacingAndGlyphs">{esc(item["label"])}</text></g>')
            x += w + gap
        y += chip_h + 16
    return "\n    ".join(parts), y - 16 + 36


def tech_stack_body(categories: list[dict]) -> tuple[str, int]:
    pill_h, pad_l, icon, gap, pad_r, between = 40, 13, 18, 9, 15, 10
    parts, y = [], HEAD + 46
    for cat in categories:
        title = cat["category"].upper()
        tw = len(title) * 14.2
        parts.append(
            f'<rect x="{600 - tw / 2 - 190:.0f}" y="{y - 6}" width="170" height="1.5" fill="url(#fl)"/>'
            f'<rect x="{600 + tw / 2 + 20:.0f}" y="{y - 6}" width="170" height="1.5" fill="url(#fr)"/>'
            f'<text x="600" y="{y}" text-anchor="middle" font-family="{SANS}" font-weight="700" font-size="17" letter-spacing="3" fill="{WHITE}">{esc(title)}</text>')
        y += 22
        widths = [pill_width(i, pad_l + icon + gap + pad_r) for i in cat["items"]]
        x = (W - sum(widths) - between * (len(widths) - 1)) / 2
        for item, w in zip(cat["items"], widths):
            colour = item.get("colour", CYAN)
            if item.get("icon"):
                mark = f'<path d="{item["icon"]}" fill="{colour}" transform="translate({pad_l},{(pill_h - icon) / 2}) scale({icon / 24})"/>'
            else:
                c = pad_l + icon / 2
                mark = f'<rect x="{c - 5.5}" y="{pill_h / 2 - 5.5}" width="11" height="11" rx="2" fill="{colour}" transform="rotate(45 {c} {pill_h / 2})"/>'
            parts.append(
                f'<g transform="translate({x:.1f},{y})"><rect width="{w}" height="{pill_h}" rx="9" {CARD}/>'
                f'<rect x="6" y="{pill_h - 2.2}" width="{w - 12}" height="1.6" rx="0.8" fill="{colour}" opacity="0.55"/>{mark}'
                f'<text x="{pad_l + icon + gap}" y="{pill_h / 2 + 5.5}" font-family="{SANS}" font-weight="600" font-size="16" fill="#F4EEFF" '
                f'textLength="{w - pad_l - icon - gap - pad_r}" lengthAdjust="spacingAndGlyphs">{esc(item["label"])}</text></g>')
            x += w + between
        y += pill_h + 50
    return "\n    ".join(parts), y - 50 + 36


def metrics_body(streaks: dict, stats: dict, today: dt.date) -> tuple[str, int]:
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

    return "\n    ".join(parts), py + ph + 36


def activity_body(snake_svg: str) -> tuple[str, int]:
    """Place the generated snake under its header.

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
    return f'<svg x="40" y="{top}" width="{width}" height="{snake_h}" viewBox="{view.group(1)}">{inner}</svg>', top + snake_h + 30


# --------------------------------------------------------------------------
# Whole panel
# --------------------------------------------------------------------------
def build_profile(content: dict, streaks: dict, stats: dict, snake_svg: str, today: dt.date) -> str:
    """Stack the intro and the four sections in one frame, with a sakura divider between them."""
    sections = [  # (stamp, kanji, romaji, title, (body svg, body height))
        ("壱", "技能", "GINŌ · ぎのう", "CORE CAPABILITIES", capabilities_body(content["capabilities"])),
        ("弐", "武器庫", "BUKIKO · ぶきこ", "TECH STACK", tech_stack_body(content["tech_stack"])),
        ("参", "戦闘力", "SENTŌRYOKU · せんとうりょく", "METRICS", metrics_body(streaks, stats, today)),
        ("肆", "草", "KUSA · くさ", "CONTRIBUTION ACTIVITY", activity_body(snake_svg)),
    ]
    hero, hero_height = hero_body(content["profile"])
    blocks = [f"<g>\n    {hero}\n    </g>", sakura_divider(hero_height + DIVIDER / 2)]
    y = hero_height + DIVIDER
    for position, (stamp, kanji, romaji, title, (body, height)) in enumerate(sections, start=1):
        blocks.append(f'<g transform="translate(0,{y})">\n    {section_header(stamp, kanji, romaji, title)}\n    {body}\n    </g>')
        y += height
        if position < len(sections):
            blocks.append(sakura_divider(y + DIVIDER / 2))
            y += DIVIDER
    height = y

    grade, _ = rank(stats)
    label = (
        f'{content["profile"]["name"].title()}. {" | ".join(content["profile"]["roles"])}. {" ".join(content["profile"]["intro_lines"])} '
        + "Core capabilities: " + ", ".join(i["label"] for i in content["capabilities"]) + ". "
        + "Tech stack: " + " ".join(f'{c["category"]}: {", ".join(i["label"] for i in c["items"])}.' for c in content["tech_stack"])
        + f' Metrics: {streaks["total"]} total contributions, current streak {streaks["current"]["length"]} days, '
        f'longest streak {streaks["longest"]["length"]} days, power level {grade}. '
        "Contribution activity: animated snake eating the contribution graph."
    )
    waves = "".join(f'<circle cx="{cx}" cy="{cy}" r="{r}"/>' for cx, cy in ((20, 0), (0, 10), (40, 10), (20, 20)) for r in (20, 13, 6))
    joined = "\n    ".join(blocks)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" height="{height}" role="img" aria-label="{esc(label)}">
  <title>{esc(label)}</title>
  <style>@media (prefers-reduced-motion: reduce) {{ .petals {{ display: none; }} }}</style>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0D0B1E"/><stop offset="0.55" stop-color="#1B1033"/><stop offset="1" stop-color="#2B1442"/></linearGradient>
    <linearGradient id="line" x1="0" x2="1"><stop offset="0" stop-color="#E11D48"/><stop offset="0.5" stop-color="{PINK}"/><stop offset="1" stop-color="{PINK}" stop-opacity="0"/></linearGradient>
    <linearGradient id="fl" x1="0" x2="1"><stop offset="0" stop-color="{PINK}" stop-opacity="0"/><stop offset="1" stop-color="{PINK}"/></linearGradient>
    <linearGradient id="fr" x1="0" x2="1"><stop offset="0" stop-color="{CYAN}"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></linearGradient>
    <linearGradient id="sep" x1="0" x2="1"><stop offset="0" stop-color="#4A3470" stop-opacity="0"/><stop offset="0.5" stop-color="#6B4FA0"/><stop offset="1" stop-color="#4A3470" stop-opacity="0"/></linearGradient>
    <linearGradient id="fade" x1="0" y1="0" x2="1" y2="0"><stop offset="0.35" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff" stop-opacity="1"/></linearGradient>
    <mask id="m"><rect width="{W}" height="{height}" fill="url(#fade)"/></mask>
    <pattern id="waves" width="40" height="20" patternUnits="userSpaceOnUse"><g fill="#1B1033" stroke="#8B6FC4" stroke-width="1">{waves}</g></pattern>
    <clipPath id="clip"><rect x="1" y="1" width="{W - 2}" height="{height - 2}" rx="14"/></clipPath>
    <clipPath id="langbar"><rect x="0" y="0" width="344" height="10" rx="5"/></clipPath>
    <filter id="glow" x="-20%" y="-40%" width="140%" height="180%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    <filter id="rough" x="-10%" y="-10%" width="120%" height="120%"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="14" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="2.2"/></filter>
  </defs>
  <g clip-path="url(#clip)">
    <rect width="{W}" height="{height}" fill="url(#bg)"/>
    <rect width="{W}" height="{height}" fill="url(#waves)" opacity="0.16" mask="url(#m)"/>
    {joined}
    <g class="petals">
    {petals(321, height)}
    </g>
  </g>
  <rect x="1" y="1" width="{W - 2}" height="{height - 2}" rx="14" fill="none" stroke="#4A3470" stroke-width="1.5"/>
</svg>
'''


def main() -> None:
    if not TOKEN:
        sys.exit("GITHUB_TOKEN is not set")
    if not SNAKE_FILE.exists():
        sys.exit(f"{SNAKE_FILE} not found; the snake must be generated before this script runs")
    content = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
    today = dt.datetime.now(dt.timezone.utc).date()
    streaks = compute_streaks(fetch_contribution_days(), today)
    stats = fetch_stats()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svg = build_profile(content, streaks, stats, SNAKE_FILE.read_text(encoding="utf-8"), today)
    (OUT_DIR / "profile.svg").write_text(svg, encoding="utf-8")
    print(f'Wrote {OUT_DIR}/profile.svg (total {streaks["total"]}, '
          f'current streak {streaks["current"]["length"]}, grade {rank(stats)[0]})')


if __name__ == "__main__":
    main()
