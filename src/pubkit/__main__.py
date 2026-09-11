# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Allow `python -m pubkit` as well as the `pubkit` console script."""
from .cli import main

if __name__ == "__main__":
    main()
