#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATCHES = ROOT / "matches.json"

SOURCES = [
    "https://stats.tennismylife.org/data/ongoing_tourneys.csv",
    "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/ongoing_tourneys.csv",
]


def norm(v):
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


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
    n = as_float(n)
    d = as_float(d)
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
            req = urllib.request.Request(
                url, headers={"User-Agent": "SportsAI/2.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text)))
            if rows:
                return rows, url
        except Exception as e:
            last_error = e

    raise RuntimeError(f"No ATP source available: {last_error}")


def row_pair(row):
    return frozenset(
        (norm(row.get("winner_name")), norm(row.get("loser_name")))
    )


def tournament_name(row):
    return (
        row.get("tourney_name")
        or row.get("tournament")
        or row.get("tourney")
        or "ATP Tournament"
    )


def round_code(row):
    return str(row.get("round") or "").upper()


def row_date(row):
    raw = str(row.get("tourney_date") or row.get("date") or "")
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return datetime.now(timezone.utc).date().isoformat()


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
            (as_float(row.get("w_svpt")) or 0)
            - (as_float(row.get("w_1stIn")) or 0),
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
            (as_float(row.get("l_svpt")) or 0)
            - (as_float(row.get("l_1stIn")) or 0),
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


def discover_match(row, source_url):
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

    m["live_state"] = make_state(m, row, source_url)
    m["result_winner"] = winner
    m["prediction_correct"] = None
    m["postmatch_summary"] = (
        f"{winner} ganó este partido. Fue descubierto automáticamente "
        "después de publicarse el resultado, por lo que no se cuenta "
        "como acierto ni fallo del modelo."
    )
    return m


def main():
    matches = json.loads(MATCHES.read_text(encoding="utf-8"))
    rows, source_url = download_rows()

    synced = 0
    discovered = 0

    # 1) Sincroniza cualquier partido ya seguido, sin importar torneo.
    for m in matches:
        if not m.get("player_a") or not m.get("player_b"):
            continue

        p = frozenset((norm(m["player_a"]), norm(m["player_b"])))
        candidates = [r for r in rows if row_pair(r) == p]

        if m.get("round"):
            same_round = [
                r for r in candidates
                if round_code(r) == str(m.get("round")).upper()
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
            m["status"] = "finished"
            m["live_state"] = state
            m["result_winner"] = state["winner"]

            if m.get("prediction_status") != "not_predicted_discovered":
                predicted = (
                    m["player_a"]
                    if float(m.get("player_a_probability", 0.5)) >= 0.5
                    else m["player_b"]
                )
                m["prediction_correct"] = (
                    norm(predicted) == norm(state["winner"])
                )
            synced += 1

    # 2) Descubre resultados ATP que aún no existían en matches.json.
    existing_ids = {m.get("match_id") for m in matches}
    existing_keys = {
        (
            frozenset((norm(m.get("player_a")), norm(m.get("player_b")))),
            str(m.get("round") or "").upper(),
            norm(m.get("tournament")),
        )
        for m in matches
    }

    for row in rows:
        if not row.get("winner_name") or not row.get("loser_name"):
            continue

        key = (
            row_pair(row),
            round_code(row),
            norm(tournament_name(row)),
        )
        rid = row_id(row)

        if rid in existing_ids or key in existing_keys:
            continue

        matches.append(discover_match(row, source_url))
        existing_ids.add(rid)
        existing_keys.add(key)
        discovered += 1

    matches.sort(
        key=lambda m: (
            str(m.get("date") or ""),
            str(m.get("tournament") or ""),
            str(m.get("round") or ""),
        ),
        reverse=True,
    )

    MATCHES.write_text(
        json.dumps(matches, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(
        f"Synced {synced} tracked matches. "
        f"Discovered {discovered} new ATP results. "
        f"Total tracked: {len(matches)}"
    )
    print(f"Source: {source_url}")


if __name__ == "__main__":
    main()
