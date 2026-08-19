#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATCHES = ROOT / "matches.json"

ECUADOR_TZ = timezone(timedelta(hours=-5))

SOURCES = [
    # Fast/current TML feeds.
    "https://stats.tennismylife.org/data/ongoing_tourneys.csv",
    "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/ongoing_tourneys.csv",

    # V7 season fallback: catches results that have already moved out of
    # ongoing_tourneys.csv but are present in the current-season database.
    "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/2026.csv",

    # Independent season fallback.
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2026.csv",
]

PLACEHOLDER_NAMES = {
    "", "tbd", "to be determined", "unknown", "bye", "n/a", "na", "-", "pending"
}

TOURNAMENT_ALIASES = {
    "cincinnati": "Cincinnati Open",
    "cincinnati open": "Cincinnati Open",
    "cincinnati masters": "Cincinnati Open",
    "western southern open": "Cincinnati Open",
    "western and southern open": "Cincinnati Open",
    "cincy": "Cincinnati Open",
    "us open": "US Open",
    "u s open": "US Open",
    "roland garros": "Roland Garros",
    "french open": "Roland Garros",
    "wimbledon": "Wimbledon",
}


def norm(v):
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def compact(v):
    return norm(v).replace(" ", "")


def name_parts(v):
    n = norm(v)
    parts = n.split()
    return parts


def surname(v):
    p = name_parts(v)
    return p[-1] if p else ""


def first_initial(v):
    p = name_parts(v)
    return p[0][0] if p and p[0] else ""


def is_placeholder(v):
    return norm(v) in PLACEHOLDER_NAMES


def canonical_tournament(v):
    n = norm(v)
    if n in TOURNAMENT_ALIASES:
        return TOURNAMENT_ALIASES[n]
    if "cincinnati" in n:
        return "Cincinnati Open"
    if n in {"us open tennis", "u s open tennis"}:
        return "US Open"
    if "roland garros" in n or "french open" in n:
        return "Roland Garros"
    return str(v or "ATP Tournament").strip()


def tournament_key(v):
    return norm(canonical_tournament(v))


def tournament_similarity(a, b):
    ca, cb = tournament_key(a), tournament_key(b)
    if ca == cb:
        return 1.0
    if ca and cb and (ca in cb or cb in ca):
        return 0.94
    return SequenceMatcher(None, ca, cb).ratio()


def player_similarity(a, b):
    """
    Robust but conservative player-name similarity.

    Examples intended to match:
      Juan Manuel Cerundolo <-> J. M. Cerundolo
      Felix Auger-Aliassime <-> Felix Auger Aliassime
      Nuno Borges <-> N. Borges
    """
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if compact(a) == compact(b):
        return 0.99

    sa, sb = surname(a), surname(b)
    ia, ib = first_initial(a), first_initial(b)

    seq = SequenceMatcher(None, na, nb).ratio()

    # Same surname is a strong signal. Initial agreement raises confidence.
    if sa and sb and sa == sb:
        if ia and ib and ia == ib:
            return max(seq, 0.94)
        return max(seq, 0.86)

    return seq


def pair_similarity(a1, a2, b1, b2):
    """
    Compare an unordered pair of players and return the strongest orientation.
    """
    direct = (player_similarity(a1, b1) + player_similarity(a2, b2)) / 2
    swapped = (player_similarity(a1, b2) + player_similarity(a2, b1)) / 2
    return max(direct, swapped)


def parse_iso_date(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def as_float(v):
    try:
        return float(v)
    except Exception:
        return None


def as_int(v):
    try:
        return int(float(v))
    except Exception:
        return None


def ratio(n, d):
    n, d = as_float(n), as_float(d)
    if n is None or d in (None, 0):
        return None
    return round(n / d, 4)


def parse_score(score):
    sets = []
    for token in str(score or "").split():
        if token.upper() in {"RET", "W/O", "WO", "DEF", "ABD"}:
            continue
        m = re.match(r"^(\d+)-(\d+)", token)
        if m:
            sets.append({"winner": int(m.group(1)), "loser": int(m.group(2))})
    return sets


def download_rows():
    """Combine all reachable free ATP result feeds."""
    all_rows = []
    used_sources = []
    errors = []

    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SportsAI/11.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8-sig")

            rows = list(csv.DictReader(io.StringIO(text)))
            if not rows:
                continue

            for row in rows:
                row["_source_url"] = url
                all_rows.append(row)

            used_sources.append(url)
        except Exception as e:
            errors.append(f"{url}: {e}")

    if not all_rows:
        raise RuntimeError("No ATP source available: " + " | ".join(errors))

    deduped = {}
    for row in all_rows:
        key = (
            norm(row.get("winner_name")),
            norm(row.get("loser_name")),
            norm(row.get("tourney_name") or row.get("tournament") or row.get("tourney")),
            str(row.get("round") or "").upper().strip(),
            re.sub(r"\D", "", str(row.get("tourney_date") or row.get("date") or ""))[:8],
        )
        if key not in deduped:
            deduped[key] = row

    return list(deduped.values()), used_sources


def row_pair(row):
    return frozenset((norm(row.get("winner_name")), norm(row.get("loser_name"))))


def tournament_name(row):
    raw = (
        row.get("tourney_name")
        or row.get("tournament")
        or row.get("tourney")
        or "ATP Tournament"
    )
    return canonical_tournament(raw)


def round_code(row):
    return str(row.get("round") or "").upper().strip()


def row_date(row):
    raw = str(row.get("tourney_date") or row.get("date") or "")
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return datetime.now(ECUADOR_TZ).date().isoformat()


def row_date_obj(row):
    return parse_iso_date(row_date(row))


def row_id(row):
    bits = [
        row_date(row),
        tournament_name(row),
        round_code(row),
        row.get("winner_name"),
        row.get("loser_name"),
    ]
    slug = "-".join(norm(x).replace(" ", "-") for x in bits if x)
    return "TML-" + slug[:150]


def same_tournament(a, b):
    return tournament_similarity(a, b) >= 0.82


def make_state(match, row, source_url):
    a_name = match["player_a"]
    b_name = match["player_b"]

    # Determine which source player corresponds to player A using similarity,
    # rather than requiring exact spelling.
    sim_a_w = player_similarity(a_name, row.get("winner_name"))
    sim_a_l = player_similarity(a_name, row.get("loser_name"))
    a_is_winner = sim_a_w >= sim_a_l

    sets = []
    for s in parse_score(row.get("score")):
        if a_is_winner:
            sets.append({"a": s["winner"], "b": s["loser"]})
        else:
            sets.append({"a": s["loser"], "b": s["winner"]})

    W = {
        "aces": as_int(row.get("w_ace")),
        "double_faults": as_int(row.get("w_df")),
        "first_serve_in_pct": ratio(row.get("w_1stIn"), row.get("w_svpt")),
        "first_serve_won_pct": ratio(row.get("w_1stWon"), row.get("w_1stIn")),
        "second_serve_won_pct": ratio(
            row.get("w_2ndWon"),
            (as_float(row.get("w_svpt")) or 0) - (as_float(row.get("w_1stIn")) or 0),
        ),
        "bp_saved_pct": ratio(row.get("w_bpSaved"), row.get("w_bpFaced")),
    }

    L = {
        "aces": as_int(row.get("l_ace")),
        "double_faults": as_int(row.get("l_df")),
        "first_serve_in_pct": ratio(row.get("l_1stIn"), row.get("l_svpt")),
        "first_serve_won_pct": ratio(row.get("l_1stWon"), row.get("l_1stIn")),
        "second_serve_won_pct": ratio(
            row.get("l_2ndWon"),
            (as_float(row.get("l_svpt")) or 0) - (as_float(row.get("l_1stIn")) or 0),
        ),
        "bp_saved_pct": ratio(row.get("l_bpSaved"), row.get("l_bpFaced")),
    }

    return {
        "status": "finished",
        "player_a": a_name,
        "player_b": b_name,
        "sets": sets,
        "set_score": [
            sum(s["a"] > s["b"] for s in sets),
            sum(s["b"] > s["a"] for s in sets),
        ],
        "winner": row.get("winner_name"),
        "duration_minutes": as_int(row.get("minutes")),
        "statistics": {
            "player_a": W if a_is_winner else L,
            "player_b": L if a_is_winner else W,
        },
        "source_note": "ATP multi-source database",
        "source_url": source_url,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_result(match, row, source_url, match_method="exact"):
    state = make_state(match, row, source_url)

    match["status"] = "finished"
    match["live_state"] = state
    match["result_winner"] = state["winner"]
    match["result_synced_at"] = datetime.now(timezone.utc).isoformat()
    match["result_match_method"] = match_method

    if match.get("prediction_status") != "not_predicted_discovered":
        predicted = (
            match["player_a"]
            if float(match.get("player_a_probability", 0.5)) >= 0.5
            else match["player_b"]
        )
        match["prediction_correct"] = (
            player_similarity(predicted, state["winner"]) >= 0.88
        )

    return match


def discover_finished(row, source_url):
    winner = row.get("winner_name")
    loser = row.get("loser_name")
    m = {
        "match_id": row_id(row),
        "date": row_date(row),
        "tournament": tournament_name(row),
        "tournament_level": row.get("tourney_level") or "ATP",
        "surface": row.get("surface") or "Unknown",
        "round": round_code(row),
        "player_a": winner,
        "player_b": loser,
        "event_type": "Singles",
        "status": "finished",
        "source": "ATP multi-source database",
        "prediction_status": "not_predicted_discovered",
        "player_a_probability": 0.5,
        "player_b_probability": 0.5,
        "discovered_automatically": True,
    }
    apply_result(m, row, source_url, "discovered")
    m["prediction_correct"] = None
    m["postmatch_summary"] = (
        f"{winner} ganó este partido. Se descubrió después de terminar, "
        "por lo que no cuenta como acierto ni fallo del modelo."
    )
    return m


def match_identity(m):
    pair = tuple(sorted((norm(m.get("player_a")), norm(m.get("player_b")))))
    return (
        pair,
        tournament_key(m.get("tournament")),
        str(m.get("round") or "").upper().strip(),
    )


def record_quality(m):
    predicted_before = m.get("prediction_status") != "not_predicted_discovered"
    status_rank = {
        "finished": 4,
        "pending_result": 3,
        "live": 2,
        "scheduled": 1,
    }.get(str(m.get("status") or "").lower(), 0)
    return (
        1 if predicted_before else 0,
        status_rank,
        1 if m.get("live_state") else 0,
        len(m.keys()),
    )


def merge_records(primary, secondary):
    result_fields = [
        "live_state",
        "result_winner",
        "result_synced_at",
        "prediction_correct",
        "postmatch_summary",
        "result_match_method",
    ]

    if str(secondary.get("status")).lower() == "finished":
        primary["status"] = "finished"

    for field in result_fields:
        if secondary.get(field) is not None and primary.get(field) is None:
            primary[field] = secondary[field]

    for key, value in secondary.items():
        if key not in primary or primary.get(key) in (None, "", []):
            primary[key] = value

    return primary


def find_best_result(match, rows):
    """
    1) Try exact normalized player pair.
    2) If that fails, use conservative fuzzy player matching plus tournament/date.
    Round is only a bonus, never a blocker.
    """
    a = match.get("player_a")
    b = match.get("player_b")
    target_pair = frozenset((norm(a), norm(b)))

    exact = [r for r in rows if row_pair(r) == target_pair]
    if exact:
        # Prefer same tournament if available.
        same_t = [r for r in exact if same_tournament(tournament_name(r), match.get("tournament"))]
        if same_t:
            exact = same_t
        return exact[-1], "exact", 1.0

    md = parse_iso_date(match.get("date"))
    best = None
    best_score = 0.0

    for r in rows:
        ps = pair_similarity(
            a, b,
            r.get("winner_name"),
            r.get("loser_name"),
        )
        if ps < 0.87:
            continue

        ts = tournament_similarity(match.get("tournament"), tournament_name(r))

        rd = row_date_obj(r)
        date_score = 0.5
        if md and rd:
            delta = abs((md - rd).days)
            if delta <= 1:
                date_score = 1.0
            elif delta <= 7:
                date_score = 0.85
            elif delta <= 21:
                date_score = 0.65
            else:
                date_score = 0.25

        round_bonus = 0.0
        if match.get("round") and round_code(r) == str(match.get("round")).upper().strip():
            round_bonus = 0.04

        # Player names dominate. Tournament/date prevent wrong historical matches.
        score = (0.72 * ps) + (0.18 * ts) + (0.10 * date_score) + round_bonus

        if score > best_score:
            best_score = score
            best = r

    # Conservative threshold to avoid false result assignment.
    if best is not None and best_score >= 0.86:
        return best, "fuzzy", round(best_score, 4)

    return None, None, 0.0


def find_existing_match_for_row(row, matches):
    """
    Used when ingesting a source result. This prevents creating a duplicate
    discovered result when we already have the same pre-match prediction.
    """
    best_idx = None
    best_score = 0.0
    rd = row_date_obj(row)

    for idx, m in enumerate(matches):
        if is_placeholder(m.get("player_a")) or is_placeholder(m.get("player_b")):
            continue

        ps = pair_similarity(
            m.get("player_a"),
            m.get("player_b"),
            row.get("winner_name"),
            row.get("loser_name"),
        )
        if ps < 0.87:
            continue

        ts = tournament_similarity(m.get("tournament"), tournament_name(row))
        md = parse_iso_date(m.get("date"))

        date_score = 0.5
        if md and rd:
            delta = abs((md - rd).days)
            if delta <= 1:
                date_score = 1.0
            elif delta <= 7:
                date_score = 0.85
            elif delta <= 21:
                date_score = 0.65
            else:
                date_score = 0.20

        score = 0.72 * ps + 0.18 * ts + 0.10 * date_score

        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is not None and best_score >= 0.86:
        return best_idx, round(best_score, 4)
    return None, 0.0




def oriented_pair_similarity(match, row):
    """Compare both A/B orientations and keep the stronger one."""
    a = match.get("player_a")
    b = match.get("player_b")
    w = row.get("winner_name")
    l = row.get("loser_name")

    direct_a = player_similarity(a, w)
    direct_b = player_similarity(b, l)
    swap_a = player_similarity(a, l)
    swap_b = player_similarity(b, w)

    direct = (direct_a + direct_b) / 2
    swapped = (swap_a + swap_b) / 2

    if direct >= swapped:
        return {
            "orientation": "direct",
            "pair_score": direct,
            "player_a_score": direct_a,
            "player_b_score": direct_b,
        }

    return {
        "orientation": "swapped",
        "pair_score": swapped,
        "player_a_score": swap_a,
        "player_b_score": swap_b,
    }


def pair_debug_summary(match, rows):
    """
    Search the loaded feeds by player identity first, without blocking on
    tournament/date. Used only for diagnostics when a stale result is missing.
    """
    a_hits = 0
    b_hits = 0
    candidates = []

    for row in rows:
        a_best = max(
            player_similarity(match.get("player_a"), row.get("winner_name")),
            player_similarity(match.get("player_a"), row.get("loser_name")),
        )
        b_best = max(
            player_similarity(match.get("player_b"), row.get("winner_name")),
            player_similarity(match.get("player_b"), row.get("loser_name")),
        )

        if a_best >= 0.88:
            a_hits += 1
        if b_best >= 0.88:
            b_hits += 1

        oriented = oriented_pair_similarity(match, row)
        if (
            oriented["player_a_score"] >= 0.88
            and oriented["player_b_score"] >= 0.88
            and oriented["pair_score"] >= 0.90
        ):
            candidates.append((row, oriented))

    candidates.sort(key=lambda x: x[1]["pair_score"], reverse=True)

    best = None
    if candidates:
        row, oriented = candidates[0]
        best = {
            "winner": row.get("winner_name"),
            "loser": row.get("loser_name"),
            "date": row_date(row),
            "tournament": tournament_name(row),
            "round": round_code(row),
            "surface": row.get("surface"),
            "score": row.get("score"),
            "pair_score": round(oriented["pair_score"], 4),
            "orientation": oriented["orientation"],
            "source": row.get("_source_url"),
        }

    return {
        "player_a_hits": a_hits,
        "player_b_hits": b_hits,
        "pair_candidates": len(candidates),
        "best_pair_candidate": best,
    }

def recovery_score(match, row):
    """V8 pair-first stale-result score."""
    oriented = oriented_pair_similarity(match, row)

    if (
        oriented["player_a_score"] < 0.88
        or oriented["player_b_score"] < 0.88
        or oriented["pair_score"] < 0.90
    ):
        return 0.0

    ts = tournament_similarity(match.get("tournament"), tournament_name(row))

    md = parse_iso_date(match.get("date"))
    rd = row_date_obj(row)

    date_score = 0.40
    if md and rd:
        delta = abs((md - rd).days)
        if delta <= 1:
            date_score = 1.0
        elif delta <= 3:
            date_score = 0.92
        elif delta <= 7:
            date_score = 0.80
        elif delta <= 14:
            date_score = 0.62
        elif delta <= 30:
            date_score = 0.35
        else:
            return 0.0

    surface_score = 0.5
    ms = norm(match.get("surface"))
    rs = norm(row.get("surface"))
    if ms and rs:
        surface_score = 1.0 if ms == rs else 0.0

    round_score = 0.5
    mr = str(match.get("round") or "").upper().strip()
    rr = round_code(row)
    if mr and rr:
        round_score = 1.0 if mr == rr else 0.35

    return (
        0.72 * oriented["pair_score"]
        + 0.12 * ts
        + 0.10 * date_score
        + 0.04 * surface_score
        + 0.02 * round_score
    )

def recover_pending_results(matches, rows, default_source, today_ec):
    """
    Recover only genuinely stale matches.

    Today's/future scheduled matches are NOT errors and are excluded from the
    unresolved count. pending_result remains eligible immediately.
    """
    recovered = 0
    unresolved = []
    skipped_not_stale = 0

    for m in matches:
        status = str(m.get("status") or "").lower()
        if status not in {"pending_result", "scheduled", "upcoming", "pre"}:
            continue
        if is_placeholder(m.get("player_a")) or is_placeholder(m.get("player_b")):
            continue

        d = parse_iso_date(m.get("date"))
        stale = status == "pending_result" or (
            status in {"scheduled", "upcoming", "pre"}
            and d is not None
            and d < today_ec
        )

        if not stale:
            skipped_not_stale += 1
            continue

        best_row = None
        best_score = 0.0

        for row in rows:
            score = recovery_score(m, row)
            if score > best_score:
                best_score = score
                best_row = row

        debug = pair_debug_summary(m, rows)

        if best_row is not None and best_score >= 0.88:
            row_source = best_row.get("_source_url") or default_source
            apply_result(m, best_row, row_source, "pending_recovery_v8")
            m["result_match_score"] = round(best_score, 4)
            m["result_pair_debug"] = debug
            m.pop("sync_warning", None)
            m["recovered_from_pending"] = True
            recovered += 1
        else:
            unresolved.append({
                "match_id": m.get("match_id"),
                "date": m.get("date"),
                "tournament": m.get("tournament"),
                "round": m.get("round"),
                "player_a": m.get("player_a"),
                "player_b": m.get("player_b"),
                "status": status,
                "best_score": round(best_score, 4),
                "debug": debug,
            })

    return recovered, unresolved, skipped_not_stale



def _http_json(url):
    """Small browser-like JSON fetcher used by V11 fixture discovery."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/142.0 Safari/537.36 SportsAI/11.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.sofascore.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def _event_tournament(event):
    t = event.get("tournament") or {}
    u = t.get("uniqueTournament") or event.get("uniqueTournament") or {}
    return canonical_tournament(
        u.get("name") or t.get("name") or "ATP Tournament"
    )


def _event_round(event):
    info = event.get("roundInfo") or {}
    raw = info.get("name") or info.get("round") or ""
    s = str(raw).strip()
    aliases = {
        "round of 128": "R128",
        "round of 64": "R64",
        "round of 32": "R32",
        "round of 16": "R16",
        "quarterfinal": "QF",
        "quarterfinals": "QF",
        "quarter-final": "QF",
        "quarter-finals": "QF",
        "semifinal": "SF",
        "semifinals": "SF",
        "semi-final": "SF",
        "semi-finals": "SF",
        "final": "F",
    }
    return aliases.get(norm(s), s.upper())


def _event_ec_datetime(event):
    ts = event.get("startTimestamp")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ECUADOR_TZ)
    except Exception:
        return None


def _looks_like_atp_singles(event):
    """Keep ATP men's singles; reject WTA, doubles, ITF women, etc."""
    home = ((event.get("homeTeam") or {}).get("name") or "").strip()
    away = ((event.get("awayTeam") or {}).get("name") or "").strip()
    if not home or not away or is_placeholder(home) or is_placeholder(away):
        return False

    t = event.get("tournament") or {}
    u = t.get("uniqueTournament") or event.get("uniqueTournament") or {}
    category = t.get("category") or u.get("category") or {}
    hay = " ".join(
        str(x or "") for x in [
            t.get("name"), t.get("slug"),
            u.get("name"), u.get("slug"),
            category.get("name"), category.get("slug"),
        ]
    )
    n = norm(hay)

    if any(x in n for x in ("wta", "women", "woman", "doubles", "double", "mixed")):
        return False

    # Prefer explicit ATP labeling. Grand Slams may be labeled without "ATP",
    # so known men's majors are accepted too.
    if "atp" in n:
        return True
    if any(x in n for x in ("us open", "wimbledon", "roland garros", "australian open")):
        return True
    return False


def fetch_tennis_events_for_date(day):
    """
    Fetch all tennis events for one date.

    Tennis schedules are paginated/grouped by scheduled-tournaments on the
    current endpoint. A legacy scheduled-events endpoint is retained as a
    fallback because provider behavior can vary.
    """
    events = []
    diagnostics = []

    # Preferred tennis-specific endpoint, paginated.
    for page in range(1, 12):
        url = (
            "https://www.sofascore.com/api/v1/sport/tennis/"
            f"scheduled-tournaments/{day.isoformat()}/page/{page}"
        )
        try:
            payload = _http_json(url)
            diagnostics.append(f"OK::{url}")

            # Different responses observed in the wild: tournaments may carry
            # nested events, while some mirrors expose a flat events array.
            page_events = list(payload.get("events") or [])
            for block in payload.get("scheduledTournaments") or payload.get("tournaments") or []:
                page_events.extend(block.get("events") or [])

            events.extend(page_events)

            has_next = payload.get("hasNextPage")
            if has_next is False:
                break
            if not page_events and page > 1:
                break
        except Exception as e:
            diagnostics.append(f"ERROR::{url}::{type(e).__name__}: {e}")
            break

    # Legacy fallback if the preferred endpoint yielded no events.
    if not events:
        for host in ("https://www.sofascore.com/api/v1", "https://api.sofascore.com/api/v1"):
            url = f"{host}/sport/tennis/scheduled-events/{day.isoformat()}"
            try:
                payload = _http_json(url)
                diagnostics.append(f"OK::{url}")
                events.extend(payload.get("events") or [])
                if events:
                    break
            except Exception as e:
                diagnostics.append(f"ERROR::{url}::{type(e).__name__}: {e}")

    # Event-id dedupe.
    unique = {}
    anonymous = []
    for e in events:
        eid = e.get("id")
        if eid is None:
            anonymous.append(e)
        else:
            unique[eid] = e
    return list(unique.values()) + anonymous, diagnostics


def fixture_existing_index(event, matches):
    home = ((event.get("homeTeam") or {}).get("name") or "").strip()
    away = ((event.get("awayTeam") or {}).get("name") or "").strip()
    tourney = _event_tournament(event)
    dt_ec = _event_ec_datetime(event)
    target_date = dt_ec.date() if dt_ec else None

    best_idx = None
    best_score = 0.0

    for idx, m in enumerate(matches):
        ps = pair_similarity(home, away, m.get("player_a"), m.get("player_b"))
        if ps < 0.90:
            continue

        ts = tournament_similarity(tourney, m.get("tournament"))
        md = parse_iso_date(m.get("date"))
        date_score = 0.5
        if target_date and md:
            delta = abs((target_date - md).days)
            if delta == 0:
                date_score = 1.0
            elif delta == 1:
                date_score = 0.90
            elif delta <= 3:
                date_score = 0.70
            else:
                date_score = 0.10

        score = 0.78 * ps + 0.14 * ts + 0.08 * date_score
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is not None and best_score >= 0.90:
        return best_idx, round(best_score, 4)
    return None, 0.0


def fixture_card_from_event(event):
    home = ((event.get("homeTeam") or {}).get("name") or "").strip()
    away = ((event.get("awayTeam") or {}).get("name") or "").strip()
    dt_ec = _event_ec_datetime(event)
    event_id = event.get("id")
    status_type = norm((event.get("status") or {}).get("type"))

    status = "scheduled"
    if status_type in {"inprogress", "live"}:
        status = "live"
    elif status_type in {"finished", "ended"}:
        status = "finished"

    date_str = dt_ec.date().isoformat() if dt_ec else datetime.now(ECUADOR_TZ).date().isoformat()
    time_str = dt_ec.strftime("%H:%M") if dt_ec else None

    m = {
        "match_id": f"SOFA-{event_id}" if event_id is not None else (
            "SOFA-" + "-".join(norm(x).replace(" ", "-") for x in [date_str, home, away])
        ),
        "date": date_str,
        "time": time_str,
        "time_ecuador": time_str,
        "timezone": "America/Guayaquil",
        "tournament": _event_tournament(event),
        "tournament_level": "ATP",
        "surface": "Unknown",
        "round": _event_round(event),
        "player_a": home,
        "player_b": away,
        "event_type": "Singles",
        "status": status,
        "source": "Sofascore fixture discovery",
        "fixture_source_url": (
            f"https://www.sofascore.com/api/v1/event/{event_id}"
            if event_id is not None else None
        ),
        "fixture_event_id": event_id,
        "fixture_discovered_automatically": True,
        "fixture_discovered_at": datetime.now(timezone.utc).isoformat(),
        # We do not fabricate model confidence for newly discovered cards.
        # The UI can show them immediately, while a separate prediction stage
        # can enrich these fields later.
        "prediction_status": "awaiting_prediction",
        "player_a_probability": 0.5,
        "player_b_probability": 0.5,
    }
    return m


def discover_scheduled_fixtures(matches, today_ec):
    """
    V11: add all discoverable ATP singles fixtures for today and tomorrow.

    Existing cards are enriched with event id / Ecuador time rather than
    duplicated. Finished events are not created here; result ingestion remains
    the responsibility of the result pipeline.
    """
    added = 0
    enriched = 0
    seen_events = 0
    diagnostics = []

    for day in (today_ec, today_ec + timedelta(days=1)):
        events, diag = fetch_tennis_events_for_date(day)
        diagnostics.extend(diag)

        for event in events:
            if not _looks_like_atp_singles(event):
                continue

            seen_events += 1
            status_type = norm((event.get("status") or {}).get("type"))
            if status_type in {"finished", "ended"}:
                continue

            idx, score = fixture_existing_index(event, matches)
            card = fixture_card_from_event(event)

            if idx is not None:
                existing = matches[idx]
                # Preserve all model-generated analysis/probabilities. Only add
                # authoritative scheduling metadata.
                for field in (
                    "fixture_event_id", "fixture_source_url",
                    "fixture_discovered_automatically", "fixture_discovered_at",
                    "time", "time_ecuador", "timezone"
                ):
                    if card.get(field) is not None:
                        existing[field] = card[field]

                if not existing.get("round") and card.get("round"):
                    existing["round"] = card["round"]
                if str(existing.get("status") or "").lower() in {"scheduled", "upcoming", "pre"}:
                    existing["status"] = card["status"]
                existing["fixture_match_score"] = score
                enriched += 1
                continue

            matches.append(card)
            added += 1

    return added, enriched, seen_events, list(dict.fromkeys(diagnostics))

def sofascore_rows_for_match(match, today_ec):
    """
    V9 fallback: query daily tennis event feeds around the card date.

    This is tournament-agnostic: it can recover ATP matches from Cincinnati,
    US Open, Roland Garros, Wimbledon, or any other tournament present in the
    daily tennis feed. It is only used for stale/pending cards that the CSV
    sources could not resolve.
    """
    target_date = parse_iso_date(match.get("date"))
    if target_date is None:
        return [], []

    # Match dates in historical CSVs are often tournament-start dates, while
    # Sports AI cards normally store the actual match date. Search a compact
    # window around the card to tolerate timezone/scheduling differences.
    dates = [target_date + timedelta(days=offset) for offset in range(-2, 3)]
    rows = []
    used = []

    for d in dates:
        if d > today_ec:
            continue

        url = f"https://api.sofascore.com/api/v1/sport/tennis/scheduled-events/{d.isoformat()}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 SportsAI/11.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))

            used.append(url)

            for event in payload.get("events", []):
                status_type = norm((event.get("status") or {}).get("type"))
                winner_code = event.get("winnerCode")

                # Only ingest completed events.
                if status_type not in {"finished", "ended"} and winner_code not in {1, 2}:
                    continue

                home = ((event.get("homeTeam") or {}).get("name") or "").strip()
                away = ((event.get("awayTeam") or {}).get("name") or "").strip()
                if not home or not away:
                    continue

                if winner_code == 1:
                    winner, loser = home, away
                    winner_score = event.get("homeScore") or {}
                    loser_score = event.get("awayScore") or {}
                elif winner_code == 2:
                    winner, loser = away, home
                    winner_score = event.get("awayScore") or {}
                    loser_score = event.get("homeScore") or {}
                else:
                    # A completed event without an explicit winner is not safe
                    # enough to assign automatically.
                    continue

                tournament = (
                    ((event.get("tournament") or {}).get("name"))
                    or (((event.get("uniqueTournament") or {}).get("name")))
                    or match.get("tournament")
                    or "ATP Tournament"
                )

                round_name = (
                    ((event.get("roundInfo") or {}).get("name"))
                    or ((event.get("roundInfo") or {}).get("round"))
                    or match.get("round")
                    or ""
                )

                score_tokens = []
                for n in range(1, 6):
                    wk = f"period{n}"
                    wv = winner_score.get(wk)
                    lv = loser_score.get(wk)
                    if wv is not None and lv is not None:
                        score_tokens.append(f"{wv}-{lv}")

                rows.append({
                    "winner_name": winner,
                    "loser_name": loser,
                    "tourney_name": tournament,
                    "round": str(round_name),
                    "tourney_date": d.strftime("%Y%m%d"),
                    "date": d.isoformat(),
                    "surface": match.get("surface") or "",
                    "score": " ".join(score_tokens),
                    "_source_url": url,
                    "_source_kind": "sofascore_v9",
                })

        except Exception as e:
            used.append(f"ERROR::{url}::{type(e).__name__}: {e}")
            continue

    return rows, used


def recover_pending_with_live_fallback(matches, unresolved, today_ec):
    """
    V9 second-stage recovery for cards left unresolved by the CSV resolver.

    Requires both player identities to match strongly. Tournament/round are
    supporting signals, not hard blockers, because providers name tournaments
    and rounds differently.
    """
    if not unresolved:
        return 0, unresolved, []

    by_id = {m.get("match_id"): m for m in matches}
    recovered = 0
    still_unresolved = []
    fallback_sources = []

    for item in unresolved:
        match = by_id.get(item.get("match_id"))
        if match is None:
            still_unresolved.append(item)
            continue

        rows, used = sofascore_rows_for_match(match, today_ec)
        fallback_sources.extend(used)

        best_row = None
        best_score = 0.0

        for row in rows:
            oriented = oriented_pair_similarity(match, row)

            # Pair-first safety gate: both names must independently agree.
            if (
                oriented["player_a_score"] < 0.88
                or oriented["player_b_score"] < 0.88
                or oriented["pair_score"] < 0.90
            ):
                continue

            ts = tournament_similarity(match.get("tournament"), tournament_name(row))
            rd = row_date_obj(row)
            md = parse_iso_date(match.get("date"))
            date_score = 0.5
            if rd and md:
                delta = abs((rd - md).days)
                date_score = 1.0 if delta <= 1 else 0.88 if delta <= 2 else 0.65

            score = 0.82 * oriented["pair_score"] + 0.10 * ts + 0.08 * date_score

            if score > best_score:
                best_score = score
                best_row = row

        if best_row is not None and best_score >= 0.88:
            apply_result(
                match,
                best_row,
                best_row.get("_source_url") or "Sofascore daily tennis feed",
                "pending_recovery_v11_live_fallback",
            )
            match["result_match_score"] = round(best_score, 4)
            match["recovered_from_pending"] = True
            match["recovery_version"] = "V11"
            match.pop("sync_warning", None)
            recovered += 1
        else:
            still_unresolved.append(item)

    # Keep diagnostics readable.
    fallback_sources = list(dict.fromkeys(fallback_sources))
    return recovered, still_unresolved, fallback_sources


def dedupe_after_recovery(matches):
    out = []
    merged = 0

    for m in matches:
        found = None

        for i, existing in enumerate(out):
            ps = pair_similarity(
                m.get("player_a"), m.get("player_b"),
                existing.get("player_a"), existing.get("player_b"),
            )
            ts = tournament_similarity(m.get("tournament"), existing.get("tournament"))

            md = parse_iso_date(m.get("date"))
            ed = parse_iso_date(existing.get("date"))
            close = True
            if md and ed:
                close = abs((md - ed).days) <= 14

            if ps >= 0.97 and ts >= 0.88 and close:
                found = i
                break

        if found is None:
            out.append(m)
            continue

        existing = out[found]
        m_pred = m.get("prediction_status") != "not_predicted_discovered"
        e_pred = existing.get("prediction_status") != "not_predicted_discovered"

        if m_pred and not e_pred:
            out[found] = merge_records(m, existing)
        elif e_pred and not m_pred:
            out[found] = merge_records(existing, m)
        elif record_quality(m) > record_quality(existing):
            out[found] = merge_records(m, existing)
        else:
            out[found] = merge_records(existing, m)

        merged += 1

    return out, merged

def main():
    matches = json.loads(MATCHES.read_text(encoding="utf-8"))
    rows, source_urls = download_rows()
    source_url = source_urls[0] if source_urls else "unknown"
    today_ec = datetime.now(ECUADOR_TZ).date()

    # V11 fixture discovery: populate today's/tomorrow's ATP singles schedule
    # before normal result synchronization.
    fixture_added, fixture_enriched, fixture_seen, fixture_diagnostics = discover_scheduled_fixtures(
        matches, today_ec
    )

    # 0) Normalize and remove all TBD/TBD cards.
    cleaned = []
    removed_placeholders = 0
    for m in matches:
        m["tournament"] = canonical_tournament(m.get("tournament"))
        if is_placeholder(m.get("player_a")) and is_placeholder(m.get("player_b")):
            removed_placeholders += 1
            continue
        cleaned.append(m)
    matches = cleaned

    # 1) Exact deduplication.
    deduped = {}
    duplicates_removed = 0
    for m in matches:
        key = match_identity(m)
        if key not in deduped:
            deduped[key] = m
            continue

        current = deduped[key]
        if record_quality(m) > record_quality(current):
            deduped[key] = merge_records(m, current)
        else:
            deduped[key] = merge_records(current, m)
        duplicates_removed += 1
    matches = list(deduped.values())

    # 2) Sync already-tracked predictions/results.
    synced_exact = 0
    synced_fuzzy = 0
    unmatched_old = 0

    for m in matches:
        if is_placeholder(m.get("player_a")) or is_placeholder(m.get("player_b")):
            continue

        row, method, score = find_best_result(m, rows)
        if row is None:
            # Only count stale/non-finished tracked cards for diagnostics.
            if str(m.get("status") or "").lower() != "finished":
                unmatched_old += 1
            continue

        state = make_state(m, row, source_url)
        old = m.get("live_state") or {}

        if (
            m.get("status") != "finished"
            or old.get("winner") != state.get("winner")
            or old.get("sets") != state.get("sets")
        ):
            apply_result(m, row, row.get("_source_url") or source_url, method)
            m["result_match_score"] = score
            if method == "exact":
                synced_exact += 1
            else:
                synced_fuzzy += 1

    # 3) Ingest source results, but first fuzzy-link them to an existing card.
    discovered = 0
    linked_existing = 0

    for row in rows:
        if not row.get("winner_name") or not row.get("loser_name"):
            continue

        idx, score = find_existing_match_for_row(row, matches)
        if idx is not None:
            m = matches[idx]
            old = m.get("live_state") or {}
            state = make_state(m, row, source_url)
            if (
                m.get("status") != "finished"
                or old.get("winner") != state.get("winner")
                or old.get("sets") != state.get("sets")
            ):
                apply_result(m, row, row.get("_source_url") or source_url, "source_link")
                m["result_match_score"] = score
                linked_existing += 1
            continue

        # Truly unseen result.
        candidate = discover_finished(row, row.get("_source_url") or source_url)
        matches.append(candidate)
        discovered += 1

    # 4) Mark stale scheduled cards correctly.
    pending_marked = 0
    for m in matches:
        status = str(m.get("status") or "").lower()
        d = parse_iso_date(m.get("date"))

        if (
            status in {"scheduled", "upcoming", "pre"}
            and d is not None
            and d < today_ec
        ):
            m["status"] = "pending_result"
            m["sync_warning"] = (
                "El partido ya pasó, pero la fuente automática todavía no "
                "ha confirmado o enlazado el resultado."
            )
            m["last_sync_check"] = datetime.now(timezone.utc).isoformat()
            pending_marked += 1

    # 5) V8 pair-first multi-source pending-result recovery.
    recovered_pending, unresolved_pending, skipped_not_stale = recover_pending_results(
        matches, rows, source_url, today_ec
    )

    # V9: if the CSV databases still do not contain a completed match, try a
    # tournament-agnostic daily tennis feed before leaving the card unresolved.
    recovered_v9, unresolved_pending, v9_sources = recover_pending_with_live_fallback(
        matches, unresolved_pending, today_ec
    )

    matches, recovery_dupes = dedupe_after_recovery(matches)

    # 6) Final fuzzy dedupe after ingestion.
    # Do not aggressively merge unrelated same-surname players.
    final = []
    fuzzy_dupes = 0

    for m in matches:
        merged = False
        for i, existing in enumerate(final):
            ps = pair_similarity(
                m.get("player_a"), m.get("player_b"),
                existing.get("player_a"), existing.get("player_b")
            )
            ts = tournament_similarity(m.get("tournament"), existing.get("tournament"))
            if ps >= 0.94 and ts >= 0.88:
                md = parse_iso_date(m.get("date"))
                ed = parse_iso_date(existing.get("date"))
                date_close = True
                if md and ed:
                    date_close = abs((md - ed).days) <= 14
                if date_close:
                    if record_quality(m) > record_quality(existing):
                        final[i] = merge_records(m, existing)
                    else:
                        final[i] = merge_records(existing, m)
                    fuzzy_dupes += 1
                    merged = True
                    break
        if not merged:
            final.append(m)

    matches = final

    matches.sort(
        key=lambda m: (
            str(m.get("date") or ""),
            str(m.get("tournament") or ""),
            str(m.get("round") or ""),
            str(m.get("player_a") or ""),
        ),
        reverse=True,
    )

    MATCHES.write_text(
        json.dumps(matches, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(f"V11 ATP fixture events seen today/tomorrow: {fixture_seen}")
    print(f"V11 scheduled fixtures added: {fixture_added}")
    print(f"V11 existing cards enriched with fixture time/id: {fixture_enriched}")
    print(f"Removed {removed_placeholders} TBD/TBD placeholders.")
    print(f"Removed/merged {duplicates_removed} exact duplicate match cards.")
    print(f"Removed/merged {fuzzy_dupes} fuzzy duplicate match cards.")
    print(f"Synced {synced_exact} tracked matches by exact player names.")
    print(f"Synced {synced_fuzzy} tracked matches by flexible name matching.")
    print(f"Linked {linked_existing} source results to existing tracked cards.")
    print(f"Discovered {discovered} truly new ATP results.")
    print(f"Marked {pending_marked} stale scheduled matches as pending_result.")
    print(f"Recovered {recovered_pending} pending/stale matches with V8 CSV resolver.")
    print(f"Recovered {recovered_v9} additional pending/stale matches with V11 live fallback.")
    print(f"Skipped {skipped_not_stale} scheduled matches that are today/future (not stale).")
    print(f"Removed/merged {recovery_dupes} result-only duplicates after recovery.")
    print(f"Unmatched stale/non-finished tracked cards before recovery: {unmatched_old}")
    print(f"Still unresolved after V11 recovery: {len(unresolved_pending)}")

    if unresolved_pending:
        print("----- UNRESOLVED MATCHES -----")
        for item in unresolved_pending:
            dbg = item["debug"]
            print(
                f"[UNRESOLVED] {item['date']} | {item['tournament']} | "
                f"{item['round']} | {item['player_a']} vs {item['player_b']} | "
                f"status={item['status']} | recovery_score={item['best_score']} | "
                f"A_hits={dbg['player_a_hits']} | B_hits={dbg['player_b_hits']} | "
                f"pair_candidates={dbg['pair_candidates']}"
            )
            best = dbg.get("best_pair_candidate")
            if best:
                print(
                    "  [BEST PAIR] "
                    f"{best['winner']} vs {best['loser']} | "
                    f"{best['date']} | {best['tournament']} | {best['round']} | "
                    f"pair_score={best['pair_score']} | orientation={best['orientation']} | "
                    f"score={best['score']} | source={best['source']}"
                )
            else:
                print("  [BEST PAIR] none found in loaded sources")
        print("------------------------------")

    print(f"Total tracked: {len(matches)}")
    print("Sources loaded:")
    for loaded_source in source_urls:
        print(f" - {loaded_source}")
    if v9_sources:
        print("V11 result-fallback diagnostics:")
        for loaded_source in v9_sources:
            print(f" - {loaded_source}")

    print("V11 fixture-discovery diagnostics:")
    for diagnostic in fixture_diagnostics:
        print(f" - {diagnostic}")


if __name__ == "__main__":
    main()
