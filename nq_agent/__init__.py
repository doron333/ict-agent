"""NQ futures agent — sweep → MSS → FVG on 1-min bars inside ET session windows.

Alert-only by policy. `NQ_MODE=paper` means the tracker simulates fills against
bars; nothing in this package places orders (see execution.py for the stub).
"""
