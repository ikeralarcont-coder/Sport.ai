#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATCHES = ROOT / "matches.json"

ECUADOR_TZ = timezone(timedelta(hours=-5))

SOURCES = [
    "https://stats.tennismylife.org/data/ongoing_tourneys.csv",
    "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/ongoing_tourneys.csv",
]

PLACEHOLDER_NAMES = {
    "", "tbd", "to be determined", "unknown", "bye", "n/a", "na", "-", "pending"
}

TOURNAMENT_ALIASES = {
    "cincinnati": "Cincinnati Open",
    "cincinnati open": "Cincinnati Open",
    "cincinnati masters": "Cincinnati Open",
    "western southern open": "Cincinnati Open",
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


def is_placeholder(v):
    return norm(v) in PLACEHOLDER_NAMES


def canonical_tournament(v):
    n = norm(v)
    if n in TOURNAMENT_ALIASES:
        return TOURNAMENT_ALIASES[n]

    # Flexible aliases for names that contain sponsor/edition text.
    if "cincinnati" in n:
        return "Cincinnati Open"
    if n in {"us open tennis", "u s open tennis"}:
        return "US Open"
    if "roland garros" in n or "french open" in n:
        return "Roland Garros"

    return str(v or "ATP Tournament").strip()


def tournament_key(v):
    return norm(canonical_tournament(v))


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
    last_error = None
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SportsAI/4.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text)))
            if rows:
                return rows, url
        except Exception as e:
            last_error = e

    raise RuntimeError(f"No ATP source available: {last_error}")


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
    return tournament_key(a) == tournament_key(b)


def make_state(match, row, source_url):
    a_is_winner = norm(match["player_a"]) == norm(row.get("winner_name"))
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
        "player_a": match["player_a"],
        "player_b": match["player_b"],
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
        "source_note": "TML ATP live database",
        "source_url": source_url,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_result(match, row, source_url):
    state = make_state(match, row, source_url)

    match["status"] = "finished"
    match["live_state"] = state
    match["result_winner"] = state["winner"]
    match["result_synced_at"] = datetime.now(timezone.utc).isoformat()

    if match.get("prediction_status") != "not_predicted_discovered":
        predicted = (
            match["player_a"]
            if float(match.get("player_a_probability", 0.5)) >= 0.5
            else match["player_b"]
        )
        match["prediction_correct"] = norm(predicted) == norm(state["winner"])

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
        "source": "TML ATP live database",
        "prediction_status": "not_predicted_discovered",
        "player_a_probability": 0.5,
        "player_b_probability": 0.5,
        "discovered_automatically": True,
    }

    apply_result(m, row, source_url)
    m["prediction_correct"] = None
    m["postmatch_summary"] = (
        f"{winner} ganó este partido. Se descubrió después de terminar, "
        "por lo que no cuenta como acierto ni fallo del modelo."
    )
    return m


def match_identity(m):
    """Identity independent of aliases and player order."""
    pair = tuple(sorted((norm(m.get("player_a")), norm(m.get("player_b")))))
    return (
        pair,
        tournament_key(m.get("tournament")),
        str(m.get("round") or "").upper().strip(),
    )


def record_quality(m):
    """
    Prefer a real pre-match prediction over a result discovered after the fact.
    Within that, prefer finished > pending_result > live > scheduled.
    """
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
    """
    Keep prediction fields from the stronger record, but copy verified result
    information from the other record when available.
    """
    result_fields = [
        "live_state",
        "result_winner",
        "result_synced_at",
        "prediction_correct",
        "postmatch_summary",
    ]

    if str(secondary.get("status")).lower() == "finished":
        primary["status"] = "finished"

    for field in result_fields:
        if secondary.get(field) is not None and primary.get(field) is None:
            primary[field] = secondary[field]

    # Fill missing metadata without overwriting a real pre-match analysis.
    for key, value in secondary.items():
        if key not in primary or primary.get(key) in (None, "", []):
            primary[key] = value

    return primary


def main():
    matches = json.loads(MATCHES.read_text(encoding="utf-8"))
    rows, source_url = download_rows()
    today_ec = datetime.now(ECUADOR_TZ).date()

    # 0) Canonicalize tournament names and REMOVE EVERY TBD/TBD placeholder.
    cleaned = []
    removed_placeholders = 0

    for m in matches:
        m["tournament"] = canonical_tournament(m.get("tournament"))

        if is_placeholder(m.get("player_a")) and is_placeholder(m.get("player_b")):
            removed_placeholders += 1
            continue

        cleaned.append(m)

    matches = cleaned

    # 1) Deduplicate aliases / repeated cards, preserving real predictions.
    deduped = {}
    duplicates_removed = 0

    for m in matches:
        key = match_identity(m)

        if key not in deduped:
            deduped[key] = m
            continue

        current = deduped[key]
        if record_quality(m) > record_quality(current):
            m = merge_records(m, current)
            deduped[key] = m
        else:
            deduped[key] = merge_records(current, m)

        duplicates_removed += 1

    matches = list(deduped.values())

    # 2) Sync existing real matches using player pair + canonical tournament.
    synced = 0

    for m in matches:
        if is_placeholder(m.get("player_a")) or is_placeholder(m.get("player_b")):
            continue

        p = frozenset((norm(m["player_a"]), norm(m["player_b"])))

        candidates = [
            r for r in rows
            if row_pair(r) == p
            and same_tournament(tournament_name(r), m.get("tournament"))
        ]

        # Tournament feeds can use different round labels; round is a preference,
        # not a hard requirement.
        if m.get("round"):
            same_round = [
                r for r in candidates
                if round_code(r) == str(m.get("round")).upper().strip()
            ]
            if same_round:
                candidates = same_round

        if not candidates:
            continue

        row = candidates[-1]
        state = make_state(m, row, source_url)
        old = m.get("live_state") or {}

        if (
            m.get("status") != "finished"
            or old.get("winner") != state.get("winner")
            or old.get("sets") != state.get("sets")
        ):
            apply_result(m, row, source_url)
            synced += 1

    # 3) Discover unseen finished ATP results.
    existing = {match_identity(m) for m in matches}
    discovered = 0

    for row in rows:
        if not row.get("winner_name") or not row.get("loser_name"):
            continue

        candidate = discover_finished(row, source_url)
        key = match_identity(candidate)

        if key in existing:
            continue

        matches.append(candidate)
        existing.add(key)
        discovered += 1

    # 4) A match whose date is already in the past must NOT remain "scheduled".
    # Do not invent the result: mark it as waiting for source synchronization.
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
                "ha confirmado el resultado."
            )
            m["last_sync_check"] = datetime.now(timezone.utc).isoformat()
            pending_marked += 1

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

    print(f"Removed {removed_placeholders} TBD/TBD placeholders.")
    print(f"Removed/merged {duplicates_removed} duplicate match cards.")
    print(f"Synced {synced} tracked matches.")
    print(f"Discovered {discovered} new ATP results.")
    print(f"Marked {pending_marked} stale scheduled matches as pending_result.")
    print(f"Total tracked: {len(matches)}")
    print(f"Source: {source_url}")


if __name__ == "__main__":
    main()
