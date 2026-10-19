#!/usr/bin/env python3
import datetime as dt
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POSITIONS_PATH = ROOT / "positions.json"
RESULTS_PATH = ROOT / "results.jsonl"
SUMMARY_PATH = ROOT / "summary.json"
BASE_URL = "https://www.chessdb.cn/cdb.php"
PACE_SECONDS = 4.2
RATE_LIMIT_BACKOFF_SECONDS = 30.0
TIMEOUT_SECONDS = 25.0
USER_AGENT = "Chess2200-CPlus-Migration/2.2"


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def query_url(fen):
    params = {
        "action": "queryall",
        "board": fen,
        "json": "1",
        "showall": "1",
        "_nonce": uuid.uuid4().hex,
    }
    # Deliberately omit learn: production default learn=1 is the v2.2 measurement mode.
    return BASE_URL + "?" + urllib.parse.urlencode(params)


def parse_numeric_count(obj):
    if not isinstance(obj, dict):
        return None
    moves = obj.get("moves")
    if not isinstance(moves, list):
        return None
    n = 0
    for m in moves:
        if not isinstance(m, dict):
            continue
        score = m.get("score")
        if isinstance(score, (int, float)):
            n += 1
        elif isinstance(score, str):
            try:
                float(score)
                n += 1
            except ValueError:
                pass
    return n


def is_rate_limited(body_text):
    s = body_text.lower()
    return "rate limit" in s or "ratelimit" in s or "too many requests" in s


def execute_once(position, seq, attempt):
    fen = position["fen"]
    url = query_url(fen)
    sent = utc_now()
    record = {
        "seq": seq,
        "attempt": attempt,
        "fen": fen,
        "depth_key": position.get("depth_key"),
        "refs": position.get("refs", []),
        "t_send_utc": sent,
        "request": {
            "endpoint": BASE_URL,
            "action": "queryall",
            "params": {"board": fen, "json": "1", "showall": "1", "learn": "OMITTED", "nonce": "random-per-request"},
            "headers": {"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": USER_AGENT},
        },
    }
    req = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.1",
        },
        method="GET",
    )
    raw = b""
    status = None
    headers = {}
    error = None
    may_have_executed = False
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.status
            headers = dict(resp.headers.items())
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        error = f"HTTPError: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        # A timeout/network failure can occur after the request reached the server.
        may_have_executed = True

    received = utc_now()
    text = raw.decode("utf-8", errors="replace")
    parsed = None
    parse_error = None
    if raw:
        try:
            parsed = json.loads(text)
        except Exception as exc:
            parse_error = f"{type(exc).__name__}: {exc}"

    record.update({
        "t_recv_utc": received,
        "http_status": status,
        "response_headers": headers,
        "raw_body": text,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "parsed_json": parsed,
        "json_parse_error": parse_error,
        "numeric_move_count": parse_numeric_count(parsed),
        "error": error,
        "may_have_executed": may_have_executed,
        "rate_limited": is_rate_limited(text),
    })
    return record


def append_record(record):
    with RESULTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def main():
    payload = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    positions = list(payload["positions"])
    positions.sort(key=lambda p: p.get("depth_key", 0), reverse=True)

    # Fresh run only. A partial run is preserved rather than implicitly resumed/requeried.
    if RESULTS_PATH.exists():
        RESULTS_PATH.unlink()

    started = utc_now()
    counts = {"positions": len(positions), "executed_attempts": 0, "success": 0, "rate_limited": 0, "errors": 0, "may_have_executed": 0, "retried_rate_limit": 0}

    for idx, pos in enumerate(positions, start=1):
        rec = execute_once(pos, idx, 1)
        counts["executed_attempts"] += 1
        append_record(rec)
        if rec["may_have_executed"]:
            counts["may_have_executed"] += 1
        if rec["rate_limited"]:
            counts["rate_limited"] += 1
            # Confirmed rate-limit response is clean to retry after backoff under canonical v2.2.
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
            retry = execute_once(pos, idx, 2)
            counts["executed_attempts"] += 1
            counts["retried_rate_limit"] += 1
            append_record(retry)
            rec = retry
        if rec["error"] or rec["may_have_executed"] or rec["http_status"] != 200:
            counts["errors"] += 1
        elif not rec["rate_limited"] and rec["parsed_json"] is not None:
            counts["success"] += 1

        # Pace after every executed position observation. Do not rapid-poll.
        if idx != len(positions):
            time.sleep(PACE_SECONDS)

    summary = {
        "protocol": "Chess 2200 canonical v2.2 one-time corpus C+ migration",
        "mode": "ChessDB Live Context (active/self-influencing)",
        "started_utc": started,
        "finished_utc": utc_now(),
        "query_discipline": {
            "action": "queryall",
            "json": 1,
            "showall": 1,
            "learn": "omitted/default learn=1",
            "deduplicated": True,
            "ordering": "depth_key descending / deepest-first",
            "pace_seconds": PACE_SECONDS,
            "no_child_enrichment": True,
            "no_polling": True,
        },
        "counts": counts,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
