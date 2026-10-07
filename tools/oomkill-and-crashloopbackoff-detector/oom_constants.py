from __future__ import annotations

from re import Pattern

RED = "\033[1;31m"

GREEN = "\033[1;32m"

YELLOW = "\033[1;33m"

BLUE = "\033[1;34m"

RESET = "\033[0m"

TIME_WINDOWS = {
    "last_1h": 1,
    "last_3h": 3,
    "last_6h": 6,
    "last_24h": 24,
    "last_48h": 48,
    "last_3d": 72,
    "last_5d": 120,
    "last_7d": 168,
}

DEFAULT_RETRIES = 3

DEFAULT_OC_TIMEOUT = 45  # seconds

RETRY_DELAY_SECONDS = 3

KONFLUX_RELEASE_DATA_REPO = "git@gitlab.cee.redhat.com:releng/konflux-release-data.git"

_CODEOWNERS_TEMP_DIR: str | None = None

_CODEOWNERS_ATEXIT_REGISTERED = False

DEFAULT_NS_BATCH_SIZE = 10

DEFAULT_NS_WORKERS = 5

DEFAULT_BATCH_SIZE = 2

_CLI_TOOL: str | None = None  # Cached CLI tool (kubectl or oc)

_MONTH_ABBR_TO_NUM = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_INCLUDE_PATTERNS: list[Pattern] | None = None

_EXCLUDE_PATTERNS: list[Pattern] | None = None

_VERBOSE: bool = False

_LIST_NAMESPACES: bool = False
