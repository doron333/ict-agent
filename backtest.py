#!/usr/bin/env python3
"""Repo-root entry for the NQ backtester: `python backtest.py --days 30`.
See nq_agent/backtest.py for options and the intrabar caveat."""
import sys

from nq_agent.backtest import main

if __name__ == "__main__":
    sys.exit(main())
