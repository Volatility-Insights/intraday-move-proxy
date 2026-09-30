"""Demonstrate a 100-to-105 proxy change using synthetic option snapshots."""

import copy
import json
from pathlib import Path

from move_proxy import calculate_snapshot


def main():
    baseline = json.loads(
        (Path(__file__).parent / "examples/baseline.json").read_text()
    )
    first, state = calculate_snapshot(baseline)
    later = copy.deepcopy(baseline)
    later["timestamp"] = "2026-09-30T12:00:00-04:00"
    for tenor in later["tenors"].values():
        for option in tenor["options"]:
            option["iv"] = 0.0525
    second, _ = calculate_snapshot(later, state)
    for result in (first, second):
        print(
            json.dumps(
                {
                    name: result[name]
                    for name in (
                        "timestamp",
                        "raw_iv",
                        "proxy_level",
                        "relative_change",
                        "alert_tier",
                    )
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
