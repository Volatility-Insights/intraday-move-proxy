# Intraday MOVE Proxy

A provider-neutral calculator for monitoring changes in Treasury
futures-option implied volatility during the trading day.

## Why Build This?

A daily MOVE close is a useful benchmark, but yesterday's close cannot show
what bond-option volatility is doing right now. This tool tracks an independent
intraday basket between daily benchmark observations. The goal is to notice
sudden increases in bond volatility early enough to reassess exposure, hedge
coverage, and position sizing, rather than wait for the next daily close.

This is **not the proprietary ICE MOVE Index**, an official intraday MOVE feed,
or a claim that official MOVE is available only at day-end. It is a separate
research indicator expressed relative to a user-supplied official close.

Bond volatility can provide useful context before broader market stress, but
it does not always lead equity volatility. This repository does not establish
predictive accuracy, causation, a hit rate, or avoided losses.

## Video Companion

The Volatility Insights episode walks through the calculation, monitoring
alerts, and selected historical MOVE/VIX examples. Its YouTube description
and timestamped chapters are in [docs/youtube-description.txt](docs/youtube-description.txt).
The historical examples are context, not an intraday proxy backtest.

## Quick Start

Python 3.10 or newer, with an IANA time-zone database containing
`America/New_York`. There are no third-party Python package dependencies.
On macOS and typical Linux installations, the time-zone database is provided
by the operating system.

```sh
git clone https://github.com/Volatility-Insights/intraday-move-proxy.git
cd intraday-move-proxy
python3 demo.py
python3 -m unittest -v
python3 move_proxy.py examples/baseline.json --state .move-proxy-state.json
python3 move_proxy.py --help
```

The demo uses synthetic data, not market observations: a basket increasing
from 5.00% to 5.25% moves the displayed proxy from 100 to 105, producing the
configured high-priority tier. The example contract symbols are placeholders,
not tradable identifiers.

For actual monitoring, export normalized snapshots from your own licensed
data feed and run the CLI repeatedly using **the same state file**. No data
subscription, authentication, API integration, scheduler, or notification
service is included. This calculator does not expose the channel's supplier.

## Methodology

For each family $i$, select an active standard expiration nearest 30 days,
excluding expirations with fewer than 7 days remaining:

$$
e_i^*=\arg\min_{e\in\mathcal E_i,\ D_e\geq7}|D_e-30|.
$$

Expiry ties prefer smaller DTE, then expiry date and underlying identifier.
There is no constant-maturity interpolation. Inspect the three distinct
strikes nearest that expiration's underlying futures price; equidistant
strike ties prefer the lower strike for reproducibility.

Within those strikes, select the valid call and put closest to absolute
fifty delta, independently:

$$
q_{i,s}^*=\arg\min_{q\in\mathcal Q_{i,s}}
\left||\Delta_q|-\tfrac12\right|,\qquad s\in\{C,P\}.
$$

Delta must be finite; IV and option mark must be finite and positive.
Positive signed delta identifies calls; nonpositive signed delta identifies
puts. Supply conventional signed deltas, not absolute or percentage deltas.
Equal delta scores retain the first supplied candidate. The selected call
and put need not share a strike. Inactive and nonstandard contracts are
excluded from the candidate set.

$$
\bar\sigma_{i,t}=\frac{\sigma^C_{i,t}+\sigma^P_{i,t}}2,
\qquad B_t=\sum_i w_i\bar\sigma_{i,t}.
$$

| Family | Treasury futures family | Weight |
| --- | --- | ---: |
| ZT | Two-year notes | 20% |
| ZF | Five-year notes | 20% |
| ZN | Ten-year notes | 40% |
| ZB | Long bonds | 20% |

All four families are required. Use consistent decimal annualized IV units:
`0.05` means 5%, not 0.05%. The basket is **futures-price implied volatility**,
not official annualized yield volatility in basis points. These families are
not four exact cash-bond maturities.

The first processed snapshot of each New York date establishes $B_0$ and
the latest official MOVE close supplied at that time, $M_a$:

$$
P_t=M_a\frac{B_t}{B_0},\qquad P_0=M_a,
\qquad r_t=\frac{P_t}{M_a}-1=\frac{B_t}{B_0}-1.
$$

That anchor and its original date remain unchanged for the date, even if a
later snapshot supplies a different official close. A new New York date
creates a new baseline. Overnight changes and moves before the first
collected snapshot are not captured.

Configured tiers are `warning` for $0.03\leq r_t<0.05$, `high` for
$r_t\geq0.05$, and `none` below 3%. A tolerance of $10^{-12}$ in the relative
change handles floating-point noise at exact boundaries. These are monitoring
settings, not universal danger levels or instructions to trade. The CLI
returns a tier on every run; notification delivery and deduplication are left
to the caller.

## Input Contract

See `examples/baseline.json` for a complete four-family snapshot.

- `timestamp`: ISO 8601 timestamp with a UTC offset, converted to New York time.
- `move_close`: `date` in `YYYY-MM-DD` form and positive finite `value`.
  Supply a legally obtained close already published by the snapshot time.
  Future dates are rejected, but publication time cannot be inferred from a date.
- `tenors`: entries for `ZT`, `ZF`, `ZN`, and `ZB`.
- Each tenor's `futures`: mapping from underlying identifier to a positive
  finite futures price using the same price convention as its option strikes.
- Each tenor's `options`: list of normalized objects with `symbol`, `underlying`,
  `expiry`, `dte`, `strike`, signed `delta`, decimal `iv`, positive `mark`, and
  boolean `active` and `vanilla` fields. DTE and expiry consistency are the
  adapter's responsibility.

Output includes the proxy level, raw basket, relative change, tier, anchor
date and age, baseline timestamp, and selected options for each family.

The state file retains each date's baseline and last processed timestamp.
Snapshots must be processed in time order within each date. Invalid inputs
do not replace the state. State replacement is atomic, but there is no
multi-process locking: use one writer per state file. Preserve it across
restarts; losing it resets the baseline to the next processed observation.
Do not use input snapshots as state files.

## Limitations And Risk Controls

- This is an educational research calculator, not an investment recommendation.
- No historical intraday proxy backtest or forecasting performance is included.
- Daily MOVE/VIX anecdotes cannot validate an intraday proxy's lead time.
- Expiry rolls, contract changes, stale quotes, wide spreads, and changed
  pricing conventions can create apparent jumps unrelated to new uncertainty.
- This version does not enforce quote age, spread limits, delta tolerance,
  contract continuity, exchange holidays, or a collection session window.
  Add those controls in your adapter and monitoring service before live use.
- An old anchor is reported through `anchor_age_days`, not rejected
  automatically. Validate the close's age and all feed permissions yourself.
- If your provider supplies option prices but not IV and delta, derive them
  using an appropriate futures-option model before constructing the snapshot.
- Protect your credentials and respect exchange and provider licensing terms.
  The repository contains only synthetic examples and no vendor API clients.

## Verification

The standard-library tests cover expiry and strike selection, invalid quote
values, missing families, averaging, weights, anchoring, exact alert
boundaries, New York date conversion, baseline rollover, out-of-order input,
and CLI persistence/error handling.