# HostKeeper for Home Assistant

Turns the things your house notices into work someone actually does.

Home Assistant is good at knowing a battery is flat, a cistern is low, or a
filter is overdue. It is not where a property manager looks for their jobs. This
integration files those conditions as tasks in [HostKeeper](https://hostkeeper.app),
keeps them in step as conditions change, and closes the loop when the work is
marked done.

The division of responsibility is the whole design:

> **Home Assistant owns whether a condition is true.
> HostKeeper owns whether work happened.**

Conflating those two is what makes this kind of integration miserable, so this
one keeps them apart.

## What it does

**Files alerts as tasks.** An automation reports a condition under a stable
*alert key*. HostKeeper keeps at most one open task per key, so re-reporting a
live alert does nothing — the automations can re-assert as often as they like.

**Handles self-resolution.** A dry spell trips the cistern alarm; a week of rain
clears it. The task is **cancelled**, not completed — nobody did the work, and
recording it as done would corrupt the maintenance history the host later relies
on.

**Verifies completions.** In HostKeeper, `done` is not terminal. When a task is
marked done, this integration fires an event and your automation decides what
done means locally:

- **Sensor-backed work** — replacing a battery, fixing a door contact. Wait for
  the sensor to settle, then check. Cleared, and the task is confirmed. Still
  asserting, and the completion is *refuted* with a reason: the work was
  attempted and did not take, which is a different fact from nobody having
  tried.
- **Work no sensor can see** — changing a filter, switching which cistern the
  house draws from. Home Assistant has to be *told*. Your automation runs
  whatever you already use to record it — a script, a date helper, an
  `input_select` — and then confirms the task.

## Setup

### 1. An API key

In the HostKeeper iOS app: **Settings → API keys → Create**.

- Preset: **Property operations** — open, update and close tasks, and read
  assets and vendors for context. It cannot message guests, change pricing, or
  dispatch a vendor.
- Turn on **Restrict to specific properties** and tick only the property this
  Home Assistant looks after.

The key is shown once. Note that Home Assistant stores config-entry credentials
in `.storage/core.config_entries` as plain JSON protected by file permissions —
the same as every other integration, but a reason to grant the narrowest key
that works.

### 2. The integration

Copy `custom_components/hostkeeper/` into your `config/custom_components/`, or
add this repository to HACS as a custom repository. Restart, then
**Settings → Devices & services → Add integration → HostKeeper** and paste the
key. If it can reach more than one property you will be asked which.

### 3. The blueprints

Copy `blueprints/automation/hostkeeper/` into your
`config/blueprints/automation/`, then create automations from them.

- **HostKeeper — sensor alert (verified)** for anything Home Assistant can see.
  Point it at a binary sensor, give it a title, set a settling time.
- **HostKeeper — manual task (actuated)** for anything it cannot. Same, plus a
  **When HostKeeper marks this done** action where you nominate the script,
  button or helper that records the work.

That action input is why the integration stays generic: it never needs to know
that `script.filter_sediment_mark_changed` exists on your system.

## Reference

### Services

| Service | What it means |
|---|---|
| `hostkeeper.report` | This condition is true. Idempotent. |
| `hostkeeper.resolve` | It is no longer true. Confirms a done task, cancels an open one. |
| `hostkeeper.block` | The task was marked done but the condition persists. Takes a reason. |

`property_id` is only needed when several properties are configured.

### Event

`hostkeeper_task_completed` fires when a task reaches `done`:

```yaml
alert_key: binary_sensor.cistern_left_low
task_id: 062d485a-...
title: Call for a water delivery
property_id: 5b66a67c-...
```

Nothing is fired for tasks already `done` when Home Assistant starts, so a
restart never re-runs your completion actions.

### Entity

A `todo` list mirrors the property's open tasks. Ticking an item marks it done,
which starts the same verification loop. It is a view, not the lifecycle —
HostKeeper has states (`blocked`, `verified`, `parts_ordered`) that the `todo`
domain cannot express.

## Design notes

**It polls.** No webhook, so nothing inbound needs to reach your Home Assistant —
which for a box on a domestic connection is the difference between working and
requiring a tunnel. Default is every two minutes.

**No local state.** HostKeeper holds the alert-key correlation itself
(`source_system` + `external_id`), so this integration keeps no mapping that
could drift or be lost when the host is reflashed.

**Automations re-assert on a heartbeat and at startup.** A missed state change
or an outage heals on the next pass instead of stranding a task open forever.

**It calls the agent-tool API** (`/api/v1/agent/tools/{tool}/invoke`) rather
than the REST resource routes. That is the surface HostKeeper publishes to
external agents, and the one whose filters are kept in parity with the tool
catalog.

## Licence

MIT.
