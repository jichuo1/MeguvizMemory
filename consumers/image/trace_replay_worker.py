#!/usr/bin/env python3
"""Isolated bounded replay worker; a valid capture receipt is mandatory."""
import argparse
import json
from pathlib import Path
import sys
import time

import trace_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--core', type=Path)
    parser.add_argument('--timeout-seconds', type=float, required=True)
    args = parser.parse_args()
    if not 0 < args.timeout_seconds <= 7200:
        parser.error('bounded timeout required')
    try:
        result = trace_replay.replay(args.receipt, args.trace, args.input, args.core,
                                     time.monotonic() + args.timeout_seconds)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        reason = str(error)
        if not reason or len(reason) > 100 or not all(c.isascii() and (c.isalnum() or c == '_') for c in reason):
            reason = type(error).__name__
        print(json.dumps({'state': 'INCOMPLETE_OR_FAILED', 'reason': reason,
                          'cleanup_outcome': getattr(error, 'cleanup_outcome', 'NOT_STARTED_OR_UNAVAILABLE'),
                          'performance': 'NOT_MEASURED', 'device': 'NOT_RUN'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
