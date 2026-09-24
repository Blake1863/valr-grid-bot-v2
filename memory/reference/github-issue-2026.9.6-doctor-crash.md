# GitHub issue draft (pending: GitHub credentials unavailable for agent — file via connected account)
# Repo: openclaw/openclaw. Filed status: NOT YET FILED (2026-09-24 ~13:55)

Title:
[Bug]: 2026.9.6 managed update fails at post-activation `openclaw doctor` lint with RangeError: Maximum call stack size exceeded; rollback then leaves DB schema ahead of package → exit-78 boot-refusal loop

Body:

## Summary
Two consecutive managed updates (chat `/update` → `gateway update.run` → handoff `openclaw update --yes`) from 2026.9.5 to 2026.9.6 both failed at the same post-activation validation step: `doctor --lint --json --severity-min error` crashed with `RangeError: Maximum call stack size exceeded` in two independent paths (config-promotion authority check; update-history reconciliation). The updater rolled back correctly, but on the second attempt the rollback left the state DB at schema 18 (migrated by a boot inside the swap window) while restoring the 2026.9.5 package (supports max schema 17), making the service unbootable (exit 78/CONFIG, also in `RestartPreventExitStatus`) for 2h14m until update recovery rolled forward to 2026.9.6.

## Environment
- Linux 6.8.0-63-generic x64 (Ubuntu), Node v24.21.0, npm 11.19.0
- npm global install (`~/.local/lib/node_modules/openclaw`), systemd --user unit
- From 2026.9.5 (ec9c1a1) → target 2026.9.6 (eb377ac), stable channel

## Failure detail (attempt 1, runId ef64c74b-488e-4442-b0af-7254d827395e)
All steps completed: staging, canary validation, global update (npm exit 0), data migrations, update health, configuration, plugins, update recovery, gateway startup checks, previous-gateway verification, activation.

failedStep `openclaw doctor` (exit 1):
- stderr: `Doctor config promotion refused for top-level keys: meta, models, wizard. authority-check-failed: Maximum call stack size exceeded`
- stdout: `Update history reconciliation could not complete: RangeError: Maximum call stack size exceeded`
- failureFacts: check=doctor, code=doctor-failed, message="Maximum call stack size exceeded"; configWriteRefusal.reason=authority-check-failed, keys=[meta, models, wizard]

Run outcome: status=rolled-back, reason=repair-requires-config-change; packageRollbackVerified=true; service settled healthy on 2026.9.5.

Attempt 2 (~2h later): failed identically at the same doctor step (per `openclaw update status` run record). A concurrent external watchdog restart during the swap window additionally produced: premature boot → pre-startup migration (schema 17→18) → crash on half-swapped tree (ERR_MODULE_NOT_FOUND) → package rollback to 2026.9.5 → boot-refusal loop (see secondary issue). Recovery eventually rolled forward to 2026.9.6; it now boots and runs clean (`openclaw doctor --non-interactive` exit 0).

## Config characteristics (rules out data-size trigger)
- openclaw.json: 9.8KB, max nesting depth 7
- meta = {lastTouchedVersion, migrations:{2 bool flags}} (112 bytes); wizard = 123 bytes; models = {mode:"merge", providers×3 with explicit model arrays}
- Direct `openclaw doctor --non-interactive` on 2026.9.6 outside the update context: exit 0 clean — crash only reproduces in the update's doctor-lint gate.

## Secondary issue: rollback does not cover DB schema state
- During attempt 2's swap window, a boot ran the pre-startup migration advancing `~/.openclaw/state/openclaw.sqlite` to schema 18 (`.pre-startup-migration-*.bak` created).
- The updater then rolled the package back to 2026.9.5 → every boot refused: "database schema(s) are newer than this build... uses schema 18; this build supports 17" → exit 78, with `RestartPreventExitStatus=78` also disabling systemd auto-restart → 2h14m unbootable until roll-forward recovery.
- Suggestion: rollback should restore (or offer to restore) the pre-migration DB snapshots it already captures, or refuse rollback once startup migrations have been applied by any boot of the new package.

## Possibly related
- #153674 (rewriteModelRefs RangeError — unguarded recursion in doctor --fix migration path)
- #153430 (deep $include config crashes deep-merge/migration probes with RangeError)
Our config is small and shallow, so the recursion crash appears triggerable on ordinary configs in the update-lint context.

## Evidence
Full managed-update handoff log preserved (24KB, includes step ledger, failureFacts, verification block). Available on request; can attach if maintainers want it.
