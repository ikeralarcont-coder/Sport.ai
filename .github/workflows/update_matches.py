#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATCHES = ROOT / "matches.json"
URL = "https://stats.tennismylife.org/data/ongoing_tourneys.csv"

def norm(value):
    s = str(value or "").lower().strip()
    table = str.maketrans("áéíóúüñ", "aeiouun")
    s = s.translate(table)
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
        token = token.upper()
        if token in {"RET", "W/O", "WO", "DEF", "ABD"}:
            continue
        m = re.match(r"^(\d+)-(\d+)", token)
        if m:
            sets.append({"winner": int(m.group(1)), "loser": int(m.group(2))})
    return sets

def download_csv():
    req = urllib.request.Request(URL, headers={"User-Agent": "SportsAI-GitHubAction/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))

def row_pair(row):
    return frozenset((norm(row.get("winner_name")), norm(row.get("loser_name"))))

def make_state(match, row):
    a_norm = norm(match["player_a"])
    winner_is_a = a_norm == norm(row.get("winner_name"))
    raw_sets = parse_score(row.get("score"))
    sets = []
    for s in raw_sets:
        if winner_is_a:
            sets.append({"a": s["winner"], "b": s["loser"]})
        else:
            sets.append({"a": s["loser"], "b": s["winner"]})

    a_sets = sum(1 for s in sets if s["a"] > s["b"])
    b_sets = sum(1 for s in sets if s["b"] > s["a"])

    w_second_total = (as_float(row.get("w_svpt")) or 0) - (as_float(row.get("w_1stIn")) or 0)
    l_second_total = (as_float(row.get("l_svpt")) or 0) - (as_float(row.get("l_1stIn")) or 0)

    W = {
        "aces": as_int(row.get("w_ace")),
        "double_faults": as_int(row.get("w_df")),
        "first_serve_in_pct": ratio(row.get("w_1stIn"), row.get("w_svpt")),
        "first_serve_won_pct": ratio(row.get("w_1stWon"), row.get("w_1stIn")),
        "second_serve_won_pct": ratio(row.get("w_2ndWon"), w_second_total),
        "bp_saved_pct": ratio(row.get("w_bpSaved"), row.get("w_bpFaced")),
    }
    L = {
        "aces": as_int(row.get("l_ace")),
        "double_faults": as_int(row.get("l_df")),
        "first_serve_in_pct": ratio(row.get("l_1stIn"), row.get("l_svpt")),
        "first_serve_won_pct": ratio(row.get("l_1stWon"), row.get("l_1stIn")),
        "second_serve_won_pct": ratio(row.get("l_2ndWon"), l_second_total),
        "bp_saved_pct": ratio(row.get("l_bpSaved"), row.get("l_bpFaced")),
    }

    return {
        "status": "finished",
        "player_a": match["player_a"],
        "player_b": match["player_b"],
        "sets": sets,
        "set_score": [a_sets, b_sets],
        "winner": row.get("winner_name"),
        "duration_minutes": as_int(row.get("minutes")),
        "statistics": {
            "player_a": W if winner_is_a else L,
            "player_b": L if winner_is_a else W,
        },
        "source_note": "TennisMyLife ongoing_tourneys.csv",
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }

def main():
    matches = json.loads(MATCHES.read_text(encoding="utf-8"))
    rows = download_csv()

    indexed = {}
    for row in rows:
        key = (row_pair(row), str(row.get("round") or "").upper())
        indexed.setdefault(key, []).append(row)

    changed = 0

    for m in matches:
        if not m.get("player_a") or not m.get("player_b"):
            continue

        pair = frozenset((norm(m["player_a"]), norm(m["player_b"])))
        round_code = str(m.get("round") or "").upper()

        found = indexed.get((pair, round_code), [])
        if not found:
            found = [r for r in rows if row_pair(r) == pair]
        if not found:
            continue

        row = found[-1]
        new_state = make_state(m, row)
        old_state = m.get("live_state") or {}

        is_new = (
            m.get("status") != "finished"
            or old_state.get("winner") != new_state.get("winner")
            or old_state.get("sets") != new_state.get("sets")
        )
        if not is_new:
            continue

        m["status"] = "finished"
        m["live_state"] = new_state
        m["result_winner"] = new_state["winner"]

        predicted = m["player_a"] if float(m.get("player_a_probability", 0.5)) >= 0.5 else m["player_b"]
        m["prediction_correct"] = norm(predicted) == norm(new_state["winner"])

        score_text = " ".join(str(s["a"]) + "-" + str(s["b"]) for s in new_state["sets"])
        verdict = "acertó" if m["prediction_correct"] else "falló"
        m["postmatch_summary"] = (
            f"{new_state['winner']} ganó {score_text}. "
            f"La predicción previa {verdict} y queda registrada para auditoría."
        )
        changed += 1

    MATCHES.write_text(
        json.dumps(matches, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Updated {changed} completed matches. Total tracked: {len(matches)}")

if __name__ == "__main__":
    main()
