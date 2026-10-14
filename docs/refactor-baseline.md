# Console Refactor Baseline

Recorded: 2026-10-12 (Asia/Shanghai)

## Source and runtime

- The source checkout is a Git repository distinct from the installed application and the historical source copy.
- The checkout was clean on `local-course-recovery` at `e1b9317` before this work. Work continues on `console-refactor-v2`.
- The installed application has its own Python runtime and browser runtime. The existing desktop launcher still owns both the reader and manager startup paths; the manager and reader services were already running during this inspection.
- The configured workspace resolves to an existing local course library. No configuration was redirected or changed.
- The repository's applicable `AGENTS.md` was read. Project `sources/` material remains read-only.

## Test baseline

Commands were run with the installed Python runtime, UTF-8 mode enabled, and the installed browser path configured for the current PowerShell session:

```powershell
& "$env:PYTHONHOME\python.exe" -m unittest discover -s console -p 'test_*.py'
& "$env:PYTHONHOME\python.exe" -m unittest console.browser_smoke
```

Results:

- Console suite: 59 tests passed.
- Browser smoke suite: 28 tests passed.
- Both commands exited successfully. Temporary test fixtures were created under the system temporary directory and cleaned by the tests.

## Existing course-data baseline

- The target course has three lecture manifests.
- The three lecture trees contain 171 files totaling 32,684,016 bytes at baseline.
- A private, local inventory records each manifest identity and each file's relative path, size, and SHA-256. It contains no course text in this repository.
- Account/workspace configuration, the installed project's credential file, and vision-model configuration were copied to a private local backup. Each backup matched the source SHA-256; JSON backups parsed successfully.
- Original course data was only read. No migration or data edits were performed.

The private inventory and backups are intentionally kept outside the source repository. Do not copy their contents into public fixtures or documentation.

## T01 checklist

- [x] Confirm source, installed application, historical copy, and workspace are distinct.
- [x] Read applicable repository instructions.
- [x] Record Git status and recent history; create the refactor branch from the clean checkout.
- [x] Run both existing test suites and record actual results.
- [x] Record the three-lecture manifest/artifact baseline and keep hashes private.
- [x] Back up account and key settings locally and verify readability/integrity.
- [x] Confirm the source course trees were not modified.

Next: implement the SQLite persistence/contracts slice from T02, with isolated temporary-database tests before connecting it to workflows.
