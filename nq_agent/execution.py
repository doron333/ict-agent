"""Execution stub — INTENTIONALLY NOT IMPLEMENTED.

The NQ agent is alert-only by policy. `NQ_MODE=paper` only means tracker.py
simulates fills against completed bars; `NQ_MODE=live` is rejected in
config.py. If order routing is ever added, it goes here behind an explicit
opt-in, with its own risk checks, and the README policy section must change
first.
"""


class ExecutionStub:
    enabled = False

    def submit(self, setup):
        raise NotImplementedError("NQ agent is alert-only — order execution is not implemented by policy")

    def cancel(self, sid):
        raise NotImplementedError("NQ agent is alert-only — order execution is not implemented by policy")
