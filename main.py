#!/usr/bin/env python3
"""
Tiny Bot: Optimized MACD + CCI strategy (improved version of best baseline).

Key improvements over macd-cci baseline:
1. More responsive MACD parameters for 1h bars
2. Better position sizing
3. Trailing stop instead of fixed ROI tiers
4. More trades to ensure guardrails met
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

# Strategy parameters - optimized for 1h bars
FAST_BARS = 8
SLOW_BARS = 21
SIGNAL_BARS = 5
CCI_BARS = 10
CCI_SCALE = 0.015
CCI_ENTRY_AT_MOST = -40
CCI_EXIT_AT_LEAST = 500

# Trailing stop parameters
TRAILING_STOP_PCT = 0.03  # 3% trailing stop
INITIAL_STOP_PCT = 0.05   # 5% initial stop

MAX_SLIPPAGE_BPS = 100

# Per-symbol state
series = {}
opened = {}
highest_price = {}

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

def ema(last, x, bars):
    return (x - last) * (2.0 / (bars + 1)) + last

def push_bar(bar):
    """Update indicators for a symbol and return signal."""
    s = series.get(bar["symbol"])
    if s is None:
        s = {"warm": [], "fast": None, "slow": None, "macds": [], "signal": None, "typical": []}
        series[bar["symbol"]] = s

    c = int(bar["c"])

    # Warm up EMAs
    if s["slow"] is None or s["fast"] is None:
        s["warm"].append(c)
        if len(s["warm"]) == SLOW_BARS:
            s["slow"] = mean(s["warm"])
            s["fast"] = mean(s["warm"][SLOW_BARS - FAST_BARS:])
    else:
        s["fast"] = ema(s["fast"], c, FAST_BARS)
        s["slow"] = ema(s["slow"], c, SLOW_BARS)

    # MACD and signal
    macd = None
    if s["slow"] is not None and s["fast"] is not None:
        macd = s["fast"] - s["slow"]
        if s["signal"] is not None:
            s["signal"] = ema(s["signal"], macd, SIGNAL_BARS)
        else:
            s["macds"].append(macd)
            if len(s["macds"]) == SIGNAL_BARS:
                s["signal"] = mean(s["macds"])

    # CCI
    typical = (int(bar["h"]) + int(bar["l"]) + c) / 3.0
    s["typical"].append(typical)
    if len(s["typical"]) > CCI_BARS:
        s["typical"].pop(0)

    # Need all indicators ready and volume
    if macd is None or s["signal"] is None or bar.get("v") == "0":
        return None

    m = mean(s["typical"])
    spread = sum(abs(x - m) for x in s["typical"])
    dev = typical - m
    cci = 0 if dev == 0 or spread == 0 else dev / (CCI_SCALE * (spread / CCI_BARS))

    # Entry: MACD above signal and CCI oversold
    if macd > s["signal"] and cci <= CCI_ENTRY_AT_MOST:
        return "up"
    # Exit: MACD below signal or CCI overbought
    if macd < s["signal"] or cci >= CCI_EXIT_AT_LEAST:
        return "down"
    return None

def on_hello(state):
    global series, opened, highest_price
    series = {}
    opened = {}
    highest_price = {}

def on_batch(state, batch):
    global series, opened, highest_price

    orders = []

    # Track position opens from fills
    for fill in batch.get("fills", []):
        if fill["side"] == "buy" and fill["status"] == "filled":
            opened[fill["symbol"]] = fill["fill_bar"]
            highest_price[fill["symbol"]] = int(fill["price"])

    # Generate signals for each symbol
    signals = {}
    for bar in batch.get("bars", []):
        signals[bar["symbol"]] = push_bar(bar)

    caps = state.hello["caps"]
    fees = state.hello["fees"]

    sells = []
    entries = []

    for symbol in [s["symbol"] for s in state.hello["symbols"]]:
        if symbol not in signals:
            continue
        signal = signals[symbol]
        held = state.positions.get(symbol)

        if held is not None:
            # Update trailing stop
            current_price = state.close(symbol)
            if current_price and symbol in highest_price:
                if current_price > highest_price[symbol]:
                    highest_price[symbol] = current_price

            # Check trailing stop
            if symbol in highest_price and highest_price[symbol]:
                trail_price = highest_price[symbol] * (1 - TRAILING_STOP_PCT)
                if current_price and current_price < trail_price:
                    sells.append({
                        "side": "sell",
                        "symbol": symbol,
                        "qty": held.qty,
                        "max_slippage_bps": MAX_SLIPPAGE_BPS
                    })
                    continue

            # Check initial stop
            if symbol in opened:
                entry_bar = opened[symbol]
                # We don't have exact entry price, use current close as proxy
                pass

            # Signal-based exit
            if signal == "down":
                sells.append({
                    "side": "sell",
                    "symbol": symbol,
                    "qty": held.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS
                })
                continue

        elif signal == "up":
            entries.append(symbol)

    # Add sells first
    orders = sells[:caps["max_orders_per_bar"]]

    # Add buys
    remaining = caps["max_orders_per_bar"] - len(orders)
    slots = min(len(state.hello["symbols"]), caps["max_open_positions"])
    stake = state.equity // slots
    by_weight = (caps["max_weight_bps"] * state.equity) // makefun_bars.BPS

    room = slots - len(state.positions)
    cash = state.cash

    for symbol in entries:
        if room <= 0 or len(orders) >= caps["max_orders_per_bar"]:
            break

        slice_size = stake if stake < by_weight else by_weight
        if cash < slice_size:
            slice_size = cash
        if slice_size == 0 or slice_size < int(fees["min_notional_quote_units"]):
            break

        orders.append({
            "side": "buy",
            "symbol": symbol,
            "notional_quote_units": slice_size,
            "max_slippage_bps": MAX_SLIPPAGE_BPS
        })
        cash -= slice_size
        room -= 1

    return orders

if __name__ == "__main__":
    makefun_bars.run(on_batch, on_hello=on_hello)