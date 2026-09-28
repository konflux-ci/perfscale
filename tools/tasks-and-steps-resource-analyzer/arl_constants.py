from __future__ import annotations

import sys
from threading import Lock

try:
    import requests  # noqa: F401
    import urllib3
    import yaml  # noqa: F401
except ImportError:
    import sys

    print(
        "Error: Missing required library. Install with: pip install requests pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PROGRESS_IO_LOCK = Lock()

_MAX_ACTIVE_CLUSTERS_IN_SPINNER = 2

DEBUG_SKIP_SAMPLE_LIMIT = 15
