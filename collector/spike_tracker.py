"""
collector/spike_tracker.py

Real-time per-market spike detection for WebSocket price streams.

SpikeTracker maintains state between consecutive WS price updates for a single
market.  Each call to update() returns:
  - None          — no spike in progress
  - spike_event   — dict describing the spike so far (while consecutive steps continue)
  - finalized     — dict with type='spike_finalized' when velocity drops (no new step)

The caller (ws_client) should:
  1. Call update() on every price tick.
  2. When a spike_in_progress dict is returned, log / buffer it.
  3. When spike_finalized is returned, persist the completed spike to spike_events.

Parameters (all tuneable from config):
  step_size        — size of one CLOB ladder step in price units (default 0.01 = 1¢)
  step_tolerance   — abs tolerance for "is this a full step?" (default 0.002)
  min_spike_steps  — consecutive same-direction steps to declare a spike (default 3)
"""

from datetime import datetime
from typing import Optional


class SpikeTracker:
    def __init__(
        self,
        market_id: str,
        step_size: float = 0.01,
        step_tolerance: float = 0.002,
        min_spike_steps: int = 3,
    ):
        self.market_id       = market_id
        self.step_size       = step_size
        self.step_tolerance  = step_tolerance
        self.min_spike_steps = min_spike_steps

        self._last_price:        Optional[float]    = None
        self._spike_start_price: Optional[float]    = None
        self._spike_start_ts:    Optional[datetime] = None
        self._peak_price:        Optional[float]    = None
        self._peak_ts:           Optional[datetime] = None
        self._consecutive_steps: int                = 0
        self._direction:         Optional[int]      = None  # +1 / -1

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def update(self, price: float, ts: datetime) -> Optional[dict]:
        """
        Process one price tick.

        Returns a dict (spike_in_progress or spike_finalized) or None.
        """
        if self._last_price is None:
            self._last_price = price
            return None

        delta = price - self._last_price
        prev_price = self._last_price
        self._last_price = price

        is_step = abs(abs(delta) - self.step_size) < self.step_tolerance
        step_dir = (1 if delta > 0 else -1) if is_step else 0

        if is_step and self._spike_start_price is None:
            # Begin a new candidate spike
            self._spike_start_price = prev_price
            self._spike_start_ts    = ts
            self._peak_price        = price
            self._peak_ts           = ts
            self._consecutive_steps = 1
            self._direction         = step_dir
            return None  # wait for min_spike_steps

        if is_step and step_dir == self._direction:
            # Continuing in same direction
            self._consecutive_steps += 1
            if abs(price) > abs(self._peak_price):
                self._peak_price = price
                self._peak_ts    = ts

            if self._consecutive_steps >= self.min_spike_steps:
                return self._build_event("spike_in_progress", ts, end_price=price)
            return None

        # Direction break or non-step — finalize if we had a qualifying spike
        result = None
        if (self._spike_start_price is not None
                and self._consecutive_steps >= self.min_spike_steps):
            result = self._build_event("spike_finalized", ts, end_price=prev_price)

        self._reset()

        if is_step:
            # Start a fresh candidate with this step
            self._spike_start_price = prev_price
            self._spike_start_ts    = ts
            self._peak_price        = price
            self._peak_ts           = ts
            self._consecutive_steps = 1
            self._direction         = step_dir

        return result

    def flush(self, ts: datetime) -> Optional[dict]:
        """
        Call when the WS stream goes idle to finalize any open spike.
        Returns spike_finalized dict or None.
        """
        if (self._spike_start_price is not None
                and self._consecutive_steps >= self.min_spike_steps):
            result = self._build_event("spike_finalized", ts,
                                       end_price=self._last_price or self._peak_price)
            self._reset()
            return result
        self._reset()
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_event(self, event_type: str, ts: datetime, end_price: float) -> dict:
        magnitude = abs(end_price - self._spike_start_price)
        return {
            "type":        event_type,
            "market_id":   self.market_id,
            "start_ts":    self._spike_start_ts,
            "peak_ts":     self._peak_ts,
            "end_ts":      ts if event_type == "spike_finalized" else None,
            "start_price": self._spike_start_price,
            "peak_price":  self._peak_price,
            "end_price":   end_price if event_type == "spike_finalized" else None,
            "magnitude":   round(magnitude, 4),
            "direction":   "up" if self._direction == 1 else "down",
            "n_steps":     self._consecutive_steps,
        }

    def _reset(self):
        self._spike_start_price = None
        self._spike_start_ts    = None
        self._peak_price        = None
        self._peak_ts           = None
        self._consecutive_steps = 0
        self._direction         = None
