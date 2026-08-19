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
    "https://stats.tennismylife.org/data/ongoing_tourneys.csv",
    "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/ongoing_tourneys.csv",
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
            req = urllib.request.Request(url, headers={"User-Agent": "SportsAI/6.0"})
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
        "source_note": "TML ATP live database",
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
        "source": "TML ATP live database",
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



def recovery_score(match, row):
    """Specialized score for stale/pending matches."""
    ps = pair_similarity(
        match.get("player_a"),
        match.get("player_b"),
        row.get("winner_name"),
        row.get("loser_name"),
    )
    if ps < 0.91:
        return 0.0

    ts = tournament_similarity(match.get("tournament"), tournament_name(row))
    if ts < 0.60:
        return 0.0

    md = parse_iso_date(match.get("date"))
    rd = row_date_obj(row)

    date_score = 0.45
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

    surface_bonus = 0.0
    ms = norm(match.get("surface"))
    rs = norm(row.get("surface"))
    if ms and rs and ms == rs:
        surface_bonus = 0.03

    return (0.78 * ps) + (0.14 * ts) + (0.08 * date_score) + surface_bonus


def recover_pending_results(matches, rows, default_source):
    recovered = 0
    unresolved = []

    for m in matches:
        status = str(m.get("status") or "").lower()
        if status not in {"pending_result", "scheduled", "upcoming", "pre"}:
            continue
        if is_placeholder(m.get("player_a")) or is_placeholder(m.get("player_b")):
            continue

        best_row = None
        best_score = 0.0

        for row in rows:
            score = recovery_score(m, row)
            if score > best_score:
                best_score = score
                best_row = row

        if best_row is not None and best_score >= 0.90:
            row_source = best_row.get("_source_url") or default_source
            apply_result(m, best_row, row_source, "pending_recovery")
            m["result_match_score"] = round(best_score, 4)
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
            })

    return recovered, unresolved


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

    # 5) V6 pending-result recovery.
    recovered_pending, unresolved_pending = recover_pending_results(
        matches, rows, source_url
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

    print(f"Removed {removed_placeholders} TBD/TBD placeholders.")
    print(f"Removed/merged {duplicates_removed} exact duplicate match cards.")
    print(f"Removed/merged {fuzzy_dupes} fuzzy duplicate match cards.")
    print(f"Synced {synced_exact} tracked matches by exact player names.")
    print(f"Synced {synced_fuzzy} tracked matches by flexible name matching.")
    print(f"Linked {linked_existing} source results to existing tracked cards.")
    print(f"Discovered {discovered} truly new ATP results.")
    print(f"Marked {pending_marked} stale scheduled matches as pending_result.")
    print(f"Recovered {recovered_pending} pending/stale matches with V6 resolver.")
    print(f"Removed/merged {recovery_dupes} result-only duplicates after recovery.")
    print(f"Unmatched stale/non-finished tracked cards before recovery: {unmatched_old}")
    print(f"Still unresolved after V6 recovery: {len(unresolved_pending)}")

    if unresolved_pending:
        print("----- UNRESOLVED MATCHES -----")
        for item in unresolved_pending:
            print(
                f"[UNRESOLVED] {item['date']} | {item['tournament']} | "
                f"{item['round']} | {item['player_a']} vs {item['player_b']} | "
                f"status={item['status']} | best_score={item['best_score']}"
            )
        print("------------------------------")

    print(f"Total tracked: {len(matches)}")
    print("Sources loaded:")
    for loaded_source in source_urls:
        print(f" - {loaded_source}")


if __name__ == "__main__":
    main()
