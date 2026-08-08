"""Constants for the HostKeeper integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hostkeeper"

# The system name recorded on every task this integration files. HostKeeper
# scopes external-id uniqueness to (property, source_system), so this string is
# load-bearing — changing it orphans every task already filed.
SOURCE_SYSTEM: Final = "home_assistant"

DEFAULT_BASE_URL: Final = "https://hostkeeper-api-qwrp3cu23q-ue.a.run.app"
DEFAULT_SCAN_INTERVAL_SECONDS: Final = 120

CONF_BASE_URL: Final = "base_url"
CONF_PROPERTY_ID: Final = "property_id"
CONF_PROPERTY_NAME: Final = "property_name"

# Services
SERVICE_REPORT: Final = "report"
SERVICE_RESOLVE: Final = "resolve"
SERVICE_BLOCK: Final = "block"
SERVICE_SYNC: Final = "sync"

ATTR_ALERT_KEY: Final = "alert_key"
ATTR_TITLE: Final = "title"
ATTR_DESCRIPTION: Final = "description"
ATTR_TASK_TYPE: Final = "task_type"
ATTR_REASON: Final = "reason"
ATTR_TASK_ID: Final = "task_id"
ATTR_ITEMS: Final = "items"
ATTR_DUE_DAYS: Final = "due_days"
ATTR_DOMAIN: Final = "domain"

# Fired on the HA event bus when a task this integration owns reaches `done`
# in HostKeeper. Automations listen for this to run the local half of the
# work — clear an ack, fire a "mark changed" script, re-read a sensor.
EVENT_TASK_COMPLETED: Final = "hostkeeper_task_completed"

# HostKeeper task lifecycle. `done` is deliberately not terminal: HA is the
# verifier and moves it on to `verified` or `blocked`.
STATUS_OPEN: Final = "open"
STATUS_DONE: Final = "done"
STATUS_VERIFIED: Final = "verified"
STATUS_BLOCKED: Final = "blocked"
STATUS_CANCELLED: Final = "cancelled"

# Statuses that mean "this alert is still live work".
OPEN_STATUSES: Final = frozenset(
    {"pending", "open", "triaged", "assigned", "in_progress", "parts_ordered", "blocked"}
)
