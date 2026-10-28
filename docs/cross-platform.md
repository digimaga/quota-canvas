# Cross-platform core refactor

## Goal and scope

Reuse the existing Python core so macOS and a future Windows front end can share
configuration and notification decisions. Preserve the current macOS Swift UI,
CLI contracts and user state. This document describes the approved direction;
it does not claim that Windows support has been implemented or validated.

Out of scope for this phase: a Windows UI, unknown Windows credential or USB
integrations, and a replacement of the macOS UI. Keep the existing `macpower`
and USB boundaries in place unless an actual dependency requires a change.

## Responsibilities and dependencies

- Python owns platform-independent configuration validation, merge semantics,
  quota decisions and the pure notification planner. It must not depend on Swift.
- Small `platform_support` boundaries isolate OS-dependent paths, process behavior
  and credential access as needed. Existing provider logic consumes those
  boundaries; do not introduce a general plugin framework for hypothetical OSes.
- The macOS Swift UI remains responsible for presentation, localization, notification
  authorization and delivery, notification preferences and `UserDefaults`.
  The UI calls the Python CLI for shared decisions; it does not maintain a second
  implementation of the basic notification rules.
- Existing screen and sleep integrations remain behind their current interfaces.
  Python core tests use fakes rather than USB, a real login or network access.

## Shared configuration

Add `config --apply-json`, accepting `{original, edited}` on stdin. Load the latest
configuration, recursively apply only the changes from `original` to `edited`,
validate the result, atomically save it and return the effective JSON. Unchanged
known fields and independent concurrent edits survive. Missing keys mean deletion,
`null` remains a value, and arrays are replaced as a unit; a later save wins for
the same field. Unknown TOML keys retain the existing warning/ignore behavior and
are not guaranteed to survive a save.

Cooperating config writers use a stable sibling lock: POSIX `flock` or Windows
`LockFileEx`. The lock spans read, merge, validation and atomic replacement; manual
writers that do not take it are not serialized. Windows execution is unverified.
Validation or save failure is reported without replacing the prior file.

Keep `config --json` and `config --set-json` compatible. Existing callers and their
JSON shapes must continue to work. A staged rollout must not assume all callers
switch at once or claim that atomic replacement alone prevents lost updates.

## Shared notification planning

The pure Python `notifications-plan` CLI accepts `{previous, current, now, notified}`
as JSON on stdin and returns `{candidates: [...]}`. `now` is Unix seconds or an ISO
8601 timestamp; `notified` contains acknowledged keys. Each candidate has a stable
`key` and `kind`: `status`, `reset` and `burst` carry `card`; `credit` carries
`provider`, `name` and `expires_at`. Planning has no delivery or state side effects.

Preserve existing Mac decisions and notified-key compatibility, including status,
reset, expiring credit and burst candidates; first-load behavior, reset tolerance,
period bucketing and deduplication must remain equivalent. Swift formats and posts
candidates and retains authorization, preferences and `UserDefaults` ownership.
When notifications are enabled, record a key only after delivery is accepted; an
error or denied or undetermined permission must not mark it delivered. When disabled
or the app delivery gate is unavailable, candidates are recorded without posting.
Acceptance checks must cover these distinctions and existing stored keys to avoid
repeated notices.

The Mac client processes snapshots in order. A failed planning request remains at
the head of the queue until the next normal status read starts another attempt;
there is no immediate retry loop. Successful planning clears its own error even
when no candidates are returned. Delivery failures remain separate and are cleared
when the corresponding key is accepted. Sharing the planner adds a local CLI
invocation per status read; it does not add service requests or a background server.

## Compatibility and state preservation

Preserve macOS paths, credential lookup order, CLI JSON and existing data: config,
status, history, notification keys and other runtime state. Do not reset, relocate,
overwrite or migrate user data merely to make the refactor work. Preserve the old
interfaces while the Swift client changes. Keep secrets out of arguments and logs.

Use isolated fixtures for verification. Before any separately authorized live
migration or rollout, identify affected state and a restorable backup; on failure,
report the failing operation and retain the previous data. Local tests alone do
not authorize a live rollout or prove real-device behavior.

## OS boundaries and Windows unknowns

Paths, executable discovery and process lifecycle, and credential reads/writes are
OS-specific seams. Mac implementations must preserve existing behavior and lookup
order. Define Windows behavior only from verified platform and provider evidence;
do not infer working authentication, storage locations, process handling or USB
support from an abstraction. Windows UI, notifications, packaging, installation,
path and permission semantics, real credentials, sleep and USB remain unverified.

## Stages and acceptance

1. Isolate the necessary OS-specific seams while retaining Mac behavior. Accept
   when existing contracts and isolated Python and scripts tests pass, and Mac
   paths and credential order are covered by regression checks.
2. Add and exercise the shared configuration operation. Accept when independent
   edits, deletions, ordering, validation failures and atomic-save failures are
   tested, prior state survives errors, and old CLI operations remain compatible.
3. Move notification decisions into Python and have Swift consume the CLI. Accept
   when shared decision and stable-key fixtures match existing behavior, Swift
   keeps delivery/recording semantics, and existing Swift regression checks pass.
4. Run the agreed isolated full Python, scripts and Swift tests after integration.
   Report skips and failures separately. Mac UI, real devices and Windows execution
   require their own authorized acceptance; do not present them as tested here.

## Rationale and remaining validation

Reuse avoids a second core and concentrates shared decisions in one testable place.
Small platform seams reduce OS-specific edits without moving working integrations
for their own sake. A full UI rewrite now would add migration and behavior risk
without being necessary to share the core; the existing Mac UI remains usable.
The tradeoff is a CLI boundary and staged compatibility work between Python and
Swift. Windows behavior and real OS delivery remain subject to separate validation.
