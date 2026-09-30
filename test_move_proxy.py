import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

import move_proxy as proxy


def option(
    symbol="call",
    delta=0.5,
    iv=0.05,
    strike=100,
    dte=30,
    expiry="2026-10-30",
    **changes
):
    result = dict(
        symbol=symbol,
        delta=delta,
        iv=iv,
        mark=1,
        strike=strike,
        dte=dte,
        expiry=expiry,
        underlying="ZN-example",
        active=True,
        vanilla=True,
    )
    result.update(changes)
    return result


class CalculationTests(unittest.TestCase):
    def test_expiry_selection(self):
        options = [
            option(dte=3),
            option(dte=24),
            option(dte=31),
            option(active=False),
            option(vanilla=False),
        ]
        self.assertEqual(proxy.select_expiry(options)[1], 31)
        self.assertEqual(
            proxy.select_expiry([option(dte=29), option(dte=31)])[1], 29
        )
        with self.assertRaises(ValueError):
            proxy.select_expiry([option(dte=6)])

    def test_average_selects_nearest_fifty_delta(self):
        result = proxy.select_atm_iv(
            [
                option(delta=0.51, iv=0.06),
                option(delta=0.7, iv=0.1),
                option("put", delta=-0.49, iv=0.07),
                option("put-far", delta=-0.3, iv=0.1),
            ]
        )
        self.assertAlmostEqual(result["iv"], 0.065)
        self.assertEqual(result["put"]["symbol"], "put")

    def test_invalid_quotes(self):
        for field, value in [
            ("iv", 0),
            ("iv", float("nan")),
            ("mark", -1),
            ("delta", None),
            ("iv", True),
        ]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    proxy.select_atm_iv(
                        [
                            option(),
                            (
                                option("put", delta=-0.5, **{field: value})
                                if field != "delta"
                                else option("put", delta=value)
                            ),
                        ]
                    )

    def test_three_nearest_strikes_exclude_far_fifty_delta(self):
        result = proxy.calculate_tenor(
            {
                "futures": {"ZN-example": 100},
                "options": [
                    option(strike=99),
                    option(strike=100, delta=0.52),
                    option("put", strike=101, delta=-0.51),
                    option("far-put", strike=110, delta=-0.5, iv=0.9),
                ],
            }
        )
        self.assertEqual(result["nearby_strikes"], [100, 99, 101])
        self.assertEqual(result["put"]["symbol"], "put")

    def test_weights_and_anchor(self):
        tenors = {
            name: {"iv": value}
            for name, value in zip(proxy.WEIGHTS, (0.02, 0.04, 0.06, 0.12))
        }
        self.assertAlmostEqual(proxy.weighted_iv(tenors), 0.06)
        self.assertAlmostEqual(proxy.anchored_level(0.0525, 100, 0.05), 105)
        for value in [0, -1, None, True, "bad", float("inf")]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    proxy.anchored_level(value, 100, 0.05)

    def test_threshold_boundaries(self):
        for change, expected in [
            (-0.1, "none"),
            (0.0299, "none"),
            (0.03, "warning"),
            (0.0499, "warning"),
            (0.05, "high"),
            (0.2, "high"),
        ]:
            self.assertEqual(proxy.alert_tier(change), expected)


def snapshot(iv=0.05, time="2026-09-30T09:30:00-04:00"):
    return {
        "timestamp": time,
        "move_close": {"date": "2026-09-29", "value": 100},
        "tenors": {
            name: {
                "futures": {"ZN-example": 100},
                "options": [option(iv=iv), option("put", delta=-0.5, iv=iv)],
            }
            for name in proxy.WEIGHTS
        },
    }


class SnapshotTests(unittest.TestCase):
    def test_baseline_then_five_percent_change(self):
        first, state = proxy.calculate_snapshot(snapshot())
        self.assertEqual(first["proxy_level"], 100)
        original_state = copy.deepcopy(state)
        second = snapshot(0.0525, "2026-09-30T12:00:00-04:00")
        second["move_close"]["value"] = 150
        result, updated = proxy.calculate_snapshot(second, state)
        self.assertAlmostEqual(result["proxy_level"], 105)
        self.assertEqual(result["alert_tier"], "high")
        self.assertEqual(result["anchor_move"], 100)
        self.assertEqual(state, original_state)
        self.assertNotEqual(updated, state)

    def test_new_date_resets_baseline(self):
        _, state = proxy.calculate_snapshot(snapshot())
        result, updated = proxy.calculate_snapshot(
            snapshot(0.07, "2026-10-01T09:30:00-04:00"), state
        )
        self.assertEqual(result["proxy_level"], 100)
        self.assertEqual(len(updated["sessions"]), 2)

    def test_new_york_date_not_utc_date(self):
        result, _ = proxy.calculate_snapshot(
            snapshot(time="2026-10-01T00:30:00+00:00")
        )
        self.assertEqual(result["trade_date"], "2026-09-30")

    def test_naive_timestamp_and_future_close_rejected(self):
        with self.assertRaises(ValueError):
            proxy.calculate_snapshot(snapshot(time="2026-09-30T12:00:00"))
        data = snapshot()
        data["move_close"]["date"] = "2026-10-01"
        with self.assertRaises(ValueError):
            proxy.calculate_snapshot(data)

    def test_missing_family_and_out_of_order_rejected(self):
        data = snapshot()
        del data["tenors"]["ZB"]
        with self.assertRaises(KeyError):
            proxy.calculate_snapshot(data)
        _, state = proxy.calculate_snapshot(
            snapshot(time="2026-09-30T12:00:00-04:00")
        )
        with self.assertRaises(ValueError):
            proxy.calculate_snapshot(snapshot(), state)

    def test_cli_persistence_and_invalid_input_preserves_state(self):
        with tempfile.TemporaryDirectory() as folder:
            input_path = Path(folder) / "snapshot.json"
            state_path = Path(folder) / "state.json"
            args = [str(input_path), "--state", str(state_path)]
            for data, expected in [
                (snapshot(), 100),
                (snapshot(0.0525, "2026-09-30T12:00:00-04:00"), 105),
            ]:
                input_path.write_text(json.dumps(data))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    proxy.main(args)
                self.assertAlmostEqual(
                    json.loads(output.getvalue())["proxy_level"], expected
                )
            preserved = state_path.read_bytes()
            input_path.write_text("{}")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as error:
                proxy.main(args)
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(state_path.read_bytes(), preserved)

    def test_cli_rejects_same_input_and_state_path(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ):
            proxy.main(["same.json", "--state", "same.json"])


if __name__ == "__main__":
    unittest.main()
