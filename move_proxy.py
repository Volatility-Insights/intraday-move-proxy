"""Provider-neutral research proxy, not the proprietary ICE MOVE Index."""

import argparse
import json
import math
import os
from datetime import date, datetime
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo


WEIGHTS = {"ZT": 0.20, "ZF": 0.20, "ZN": 0.40, "ZB": 0.20}
TARGET_DTE = 30
MIN_DTE = 7
NEARBY_STRIKES = 3
ET = ZoneInfo("America/New_York")


def number(value):
    """Return a finite number, or None for invalid quote values."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def positive(value, label):
    result = number(value)
    if result is None or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


def select_expiry(options):
    """Choose nearest eligible expiry; ties prefer shorter maturity."""
    groups = set()
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("Each option must be an object")
        dte = number(option.get("dte"))
        expiry = option.get("expiry")
        underlying = option.get("underlying")
        if (
            option.get("active") is not True
            or option.get("vanilla") is not True
            or dte is None
            or dte < MIN_DTE
            or not isinstance(expiry, str)
            or not isinstance(underlying, str)
            or not underlying
        ):
            continue
        try:
            date.fromisoformat(expiry)
        except ValueError:
            continue
        groups.add((expiry, dte, underlying))
    if not groups:
        raise ValueError("No eligible active standard option expiration")
    return min(
        groups,
        key=lambda group: (
            abs(group[1] - TARGET_DTE),
            group[1],
            group[0],
            group[2],
        ),
    )


def select_atm_iv(options):
    """Average valid call and put IVs nearest absolute fifty delta."""
    selected = {}
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("Each option must be an object")
        delta = number(option.get("delta"))
        iv = number(option.get("iv"))
        mark = number(option.get("mark"))
        if delta is None or iv is None or iv <= 0 or mark is None or mark <= 0:
            continue
        side = "call" if delta > 0 else "put"
        score = abs(abs(delta) - 0.5)
        if side not in selected or score < selected[side][0]:
            selected[side] = (score, iv, option)
    if set(selected) != {"call", "put"}:
        raise ValueError(
            "Both valid call and put implied volatilities required"
        )
    return {
        "iv": (selected["call"][1] + selected["put"][1]) / 2,
        "call": selected["call"][2],
        "put": selected["put"][2],
    }


def calculate_tenor(tenor):
    """Filter the chosen expiry to three strikes nearest futures price."""
    options = tenor["options"]
    if not isinstance(options, list):
        raise ValueError("options must be a list")
    expiry, dte, underlying = select_expiry(options)
    future_price = positive(tenor["futures"][underlying], "Futures price")
    contracts = [
        option
        for option in options
        if (
            option.get("expiry"),
            number(option.get("dte")),
            option.get("underlying"),
        )
        == (expiry, dte, underlying)
        and option.get("active") is True
        and option.get("vanilla") is True
    ]
    strikes = sorted(
        {number(option.get("strike")) for option in contracts} - {None},
        key=lambda strike: (abs(strike - future_price), strike),
    )[:NEARBY_STRIKES]
    selected = select_atm_iv(
        [
            option
            for option in contracts
            if number(option.get("strike")) in strikes
        ]
    )
    return dict(
        selected,
        expiry=expiry,
        dte=dte,
        underlying=underlying,
        future_price=future_price,
        nearby_strikes=strikes,
    )


def weighted_iv(tenors):
    total = sum(
        positive(tenors[name]["iv"], f"{name} IV") * weight
        for name, weight in WEIGHTS.items()
    )
    return positive(total, "Weighted IV")


def anchored_level(raw_iv, anchor_move, baseline_iv):
    raw_iv = positive(raw_iv, "Current IV")
    anchor_move = positive(anchor_move, "MOVE anchor")
    baseline_iv = positive(baseline_iv, "Baseline IV")
    return positive(anchor_move * (raw_iv / baseline_iv), "Proxy level")


def alert_tier(relative_change):
    """Return the configured tier; this does not send notifications."""
    change = number(relative_change)
    if change is None:
        raise ValueError("Relative change must be finite")
    if change >= 0.05 or math.isclose(change, 0.05, rel_tol=0, abs_tol=1e-12):
        return "high"
    if change >= 0.03 or math.isclose(change, 0.03, rel_tol=0, abs_tol=1e-12):
        return "warning"
    return "none"


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset")
    return parsed.astimezone(ET)


def calculate_snapshot(snapshot, state=None):
    """Return an auditable result and updated state without mutating inputs."""
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be an object")
    now = timestamp(snapshot["timestamp"])
    trade_date = now.date().isoformat()
    move = snapshot["move_close"]
    move_date = date.fromisoformat(move["date"])
    move_level = positive(move["value"], "Official MOVE close")
    if move_date > now.date():
        raise ValueError("Official MOVE close date cannot be in the future")
    tenors = {
        name: calculate_tenor(snapshot["tenors"][name]) for name in WEIGHTS
    }
    raw_iv = weighted_iv(tenors)
    if state is None:
        state = {"version": 1, "sessions": {}}
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("sessions"), dict)
    ):
        raise ValueError("Invalid baseline state; expected version 1 sessions")
    sessions = dict(state["sessions"])
    baseline = sessions.get(trade_date)
    if baseline is None:
        baseline = {
            "baseline_timestamp": now.isoformat(),
            "last_timestamp": now.isoformat(),
            "baseline_iv": raw_iv,
            "anchor_move": move_level,
            "anchor_move_date": move_date.isoformat(),
        }
    else:
        if not isinstance(baseline, dict):
            raise ValueError("Invalid session baseline")
        if now < timestamp(baseline["last_timestamp"]):
            raise ValueError(
                "Snapshots must be processed in time order per date"
            )
        if timestamp(baseline["baseline_timestamp"]).date() != now.date():
            raise ValueError(
                "Baseline timestamp does not match its session date"
            )
        if date.fromisoformat(baseline["anchor_move_date"]) > now.date():
            raise ValueError("Baseline anchor date cannot be in the future")
    baseline_iv = positive(baseline["baseline_iv"], "Baseline IV")
    anchor = positive(baseline["anchor_move"], "MOVE anchor")
    level = anchored_level(raw_iv, anchor, baseline_iv)
    change = level / anchor - 1
    result = {
        "timestamp": now.isoformat(),
        "trade_date": trade_date,
        "proxy_level": level,
        "raw_iv": raw_iv,
        "baseline_iv": baseline_iv,
        "baseline_timestamp": baseline["baseline_timestamp"],
        "anchor_move": anchor,
        "anchor_move_date": baseline["anchor_move_date"],
        "anchor_age_days": (
            now.date() - date.fromisoformat(baseline["anchor_move_date"])
        ).days,
        "relative_change": change,
        "alert_tier": alert_tier(change),
        "tenors": tenors,
    }
    sessions[trade_date] = dict(baseline, last_timestamp=now.isoformat())
    return result, {"version": 1, "sessions": sessions}


def save_state(path, state):
    """Replace the state atomically; callers must serialize concurrent runs."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "snapshot", type=Path, help="Normalized option snapshot JSON"
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(".move-proxy-state.json"),
        help="Persistent daily baselines (single writer)",
    )
    args = parser.parse_args(argv)
    if args.snapshot.resolve() == args.state.resolve():
        parser.error("Snapshot and state paths must differ")
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        state = (
            json.loads(args.state.read_text(encoding="utf-8"))
            if args.state.exists()
            else None
        )
        result, updated = calculate_snapshot(snapshot, state)
        output = json.dumps(result, indent=2, allow_nan=False)
        save_state(args.state, updated)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"Cannot calculate proxy: {error}")
    print(output)


if __name__ == "__main__":
    main()
