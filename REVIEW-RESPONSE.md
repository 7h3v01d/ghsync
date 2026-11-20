# ghsync — responses to independent adversarial reviews

## 0.2.0: response to the first review

Each finding was first reproduced against the 0.1 code with a behavioural test, then fixed. The same suite runs against both builds (`GHSYNC_SRC=<folder of 0.1> python -m pytest tests`). Result: **0.2 passes 51/51 tests. 0.1 fails 39**, every one on the behaviour it targets, apart from the three noted under GHS-007. Tests that pass on both builds are guards for behaviour 0.1 already got right (e.g. replacing a symlinked file, recovering from an interrupted pull) so the fixes can't regress it.

| ID | Finding | Fix | Tests (failing on 0.1 / total) |
|---|---|---|---|
| GHS-001 | Remote paths bypass exclusions (`.ghsync/`, `.env`) | One `check_repo_path()` policy for every path from either side: rejects `..`, absolute paths, `.ghsync/` and `.git/` at any depth, Windows-illegal names and characters. Ignore rules are now applied to GitHub's files. Held paths can't be targeted by conflict resolution. GitHub symlinks and submodules are held, not materialised. | `TestGHS001RemotePathPolicy` (6 / 7) |
| GHS-002 | Writes can escape the project via links | `_check_parents()` walks every parent with `lstat` and refuses symlinks and name-surrogate reparse points (junctions, mount points) but not OneDrive placeholders. Checked before temp creation and again right before `os.replace`. Temp files use `O_EXCL` + `O_NOFOLLOW`. Deletes and upload reads use the same checks. | `TestGHS002Containment` (2 / 3), `TestGHS002Units` (2 / 2) |
| GHS-003 | Ignored/oversized files treated as deletions | The scan reports exclusions with reasons. `compute_held()` takes out ignored, excluded, unsafe and not-confirmed-absent paths before classification, and their base is preserved. `_probe_absent()` handles link parents and case-only renames. | `TestGHS003ExcludedIsNotDeleted` (5 / 5) |
| GHS-004 | Local edits during pull overwritten | Each pulled file is downloaded to a temp file, then its current content is checked against the scanned sha (or absence) immediately before replace or delete. A mismatch skips that file and keeps its base, so it is reclassified next run. Per-file failures no longer abort the whole pull. | `TestGHS004PullPreconditions` (5 / 5) |
| GHS-005 | No interprocess lock | `ProjectLock`: OS lock on `.ghsync/lock` (`flock` / `msvcrt.locking`), re-entrant per thread, released by the OS on process death. Owner PID/host is recorded for the busy message. Covers sync/push/pull/link/init; the cache write is opportunistic. | `TestGHS005Lock` (2 / 3) |
| GHS-006 | Non-atomic state; silent default on corruption | `write_json` uses temp + fsync + `os.replace` (with Windows sharing-violation retry). Damaged `state.json`/`config.json` stops the run with recovery instructions; a damaged cache just rehashes. Pull results are persisted before pushing. Leftover temp files are cleaned under the lock. | `TestGHS006AtomicState` (3 / 5) |
| GHS-007 | GUI confirms a stale plan | `plan_actions()` + `plan_digest()`: the GUI computes the exact push/pull set for the mode, shows the deletions by name, and passes the digest. The engine recomputes after taking the lock and refuses on mismatch; the GUI then refreshes. | `TestGHS007PlanBinding` (3 / 3, engine), `TestGuiPlanBinding` (3 / 4, real window offscreen) |
| GHS-008 | No test evidence shipped | `tests/` with a fake GitHub (in-process and over real HTTP), CLI subprocess tests, offscreen GUI tests, and `smoke_real_github.py` for a disposable real repo. | — |

The engine-level GHS-007 tests error on 0.1 because their helper calls the new API. The GUI tests reproduce GHS-007 behaviourally on 0.1: "a deletion the user never saw was executed."

## Additional defects found while verifying (not in the review)

- **Windows junctions were scanned and pushed.** Before Python 3.12, `os.walk` treats a junction as a folder, and 0.1 only filtered `islink()`. Files from wherever a junction pointed were uploaded. The scan now uses `lstat` + reparse tags. (`TestWindowsLinks`)
- **GitHub symlinks became text files.** A mode-120000 entry was pulled as a regular file containing the link target. These entries are now held.
- **Executable bit lost on pull.** The replacement file took default permissions; it now inherits the existing file's mode.
- **Hashing a FIFO/special file would hang the scan.** Only regular files are hashed now.
- **A file growing during hashing produced a wrong sha**, which then aborted the push. A size mismatch now marks the file as unreadable for that run.
- **Ambiguous ref update.** A retried PATCH that had already landed was reported as "remote changed". The engine now re-reads the ref before failing.
- **Config values went into URLs unvalidated.** Owner/repo/branch are now validated and URL-quoted.
- **Racy mtime cache.** Files modified within the last 2 s are no longer cached by size+mtime.

## Acceptance gates (milestone 0.2)

| # | Gate | Status |
|---|---|---|
| 1 | Reject unsafe remote paths and protected internal directories | Done, tested |
| 2 | Prevent symlink, junction and reparse-point escapes | Done. Symlinks tested on Linux; junction handling tested by simulating Windows `lstat` results. **Needs a real Windows check.** |
| 3 | Distinguish ignored, oversized and unreadable files from deleted files | Done, tested |
| 4 | Revalidate local files immediately before pull replacement or deletion | Done, tested |
| 5 | One mutating operation per project across processes | Done, tested with a real second process (POSIX `flock`). The Windows `msvcrt` path is the same code shape but **untested on Windows**. |
| 6 | Atomic state persistence with crash-recovery handling | Done, tested |
| 7 | Bind GUI deletion approval to the actual execution plan | Done, tested in the real GUI offscreen |
| 8 | Regression tests + real GitHub disposable-repository smoke test | Regression suite done. The smoke test script is written and validated against the HTTP fake (11/11), but **has not been run against real GitHub**: that needs your token. |

## Residual risks, stated plainly

- Between the final parent check and `os.replace` there is a tiny window. On Windows (no `O_NOFOLLOW`/`openat`), a local process actively racing ghsync could swap a folder for a junction in that window. A hostile repository alone cannot do this.
- Push is atomic (one commit, fast-forward only). Pull is per-file, not a directory transaction; an interrupted pull leaves some files updated, and the next run finishes the job.
- The token is still a plain-text file. Fine-grained tokens are now the documented default; Credential Manager is deferred to keep this milestone feature-free.

---

# 0.2.1: response to the follow-up review

## GHS-009: temp cleanup deleted user files (HIGH, confirmed)

**Reproduced first.** Against the shipped 0.2.0, the new regression test fails with `notes.ghsync-tmp was deleted`. The old test `test_leftover_temp_files_are_cleaned` asserted exactly the buggy behaviour, so it has been removed.

**Fix: ownership is proven by a journal, never by a name.**
- Each pull gets a random 16-hex run ID. Its temp files are named `.ghsync-<run id>-<n>.ghsync-tmp`.
- The run ID is written atomically to `.ghsync/journal.json` **before** any temp file is created, and removed once the pull finishes (every temp has by then been renamed into place or removed by the code that made it).
- Cleanup, under the project lock, deletes a file only if its name matches that exact pattern **and** its run ID is in the journal. Every other `*.ghsync-tmp` file is user data and is left alone. A damaged or missing journal reads as empty, so the failure mode is "leave it", never "delete it".
- Inside the reserved `.ghsync/` folder, only ghsync's own `.tmp-<12 hex>.ghsync-tmp` state-temp names are removed.
- The `.ghsync-tmp` suffix stays reserved: such files are not synced in either direction, and GitHub can't plant a tool-temp name.

| Test (`TestGHS009TempOwnership`) | 0.2.0 | 0.2.1 |
|---|---|---|
| user files named `*.ghsync-tmp` (including look-alikes of 0.2.0 and 0.2.1 temp names, and one inside `.ghsync/`) survive two full push+pull syncs | **fails** | passes |
| a real hard crash (`os._exit`) mid-pull leaves its temp; the next run removes only that temp, pulls the file, and keeps the user's `notes.ghsync-tmp` | **fails** | passes |
| a damaged journal never causes a deletion | **fails** | passes |
| GitHub can't plant a file with a tool-temp name | passes | passes |

Revert-proof: on 0.2.0, exactly these three tests fail and every other test passes. On 0.1, 40 fail.

## Windows verification (items 2 and 3 of the 0.2.1 plan)

These need your machine. New in 0.2.1: `TestRealWindowsJunctions` creates real junctions with `mklink /J` (no admin rights needed) and checks pull escape, push of junction contents, remote delete through a junction, and false deletion behind a junction. It also asserts that Python's `lstat` actually reports the junction as a link. The lock tests already use a real `msvcrt` holder process on Windows.

```
pip install PyQt6                 (so the 4 GUI tests run instead of skipping)
run_tests.bat
```
Expected on Windows 10/11 with Python 3.11: **52 passed, 6 skipped**. The 6 skips are POSIX-symlink and POSIX-permission tests; the real-junction tests cover the Windows equivalents. Any failure in `TestRealWindowsJunctions`, `TestGHS005Lock` or `TestGHS009TempOwnership` is a release blocker.

```
python tests\smoke_real_github.py you/throwaway --i-understand you/throwaway
```
Expected: `11/11 steps passed` (upload, download, remote delete, local delete, conflict + resolution, hostile paths, newly ignored file, concurrent run refused, restart with a fresh state).

## Gate status after 0.2.1

| Gate | Status |
|---|---|
| GHS-001, 003, 004 | Pass (reviewer confirmed) |
| GHS-002 containment | Linux pass; real-junction tests written, **awaiting Windows run** |
| GHS-005 lock | POSIX pass; `msvcrt` path **awaiting Windows run** |
| GHS-006 atomic state + recovery | Pass; GHS-009 fixed with journaled ownership |
| GHS-007 plan binding | Pass (engine and offscreen GUI); **awaiting Windows GUI run** |
| GHS-008 evidence | Suite supplied; **live GitHub smoke test awaiting run** |
| GHS-009 temp ownership | Fixed, revert-proven |

**Threat model (as the reviewer proposed):** ghsync defends against hostile or unexpected repository content and against your own concurrent use. It does not defend against a hostile local process actively racing filesystem changes (the residual check-then-rename window on Windows).

---

# 0.2.1: third review (CONDITIONAL ACCEPT)

## GHS-010: orphaned temp file in a newly ignored folder (LOW, confirmed, deferred)

Reproduced against 0.2.1 exactly as reported: the journaled run's temp file inside an ignored folder is not discovered, the run ID is dropped from the journal, and the file stays behind (`discovered: [] | temp still exists: True | journal: []`).

**When it happens:** a pull is hard-killed while writing into folder X, and X is added to the ignore rules before the next run. (While X is ignored, ghsync never pulls into it, so it can't create a new temp there.)

**Impact:** one orphaned `.ghsync-<run id>-<n>.ghsync-tmp` file per affected file. It is never synced and never deleted, and no user data is lost or corrupted. You can delete it by hand.

**Disposition:** as the reviewer recommends, tracked as a maintenance issue rather than extending the 0.2.1 milestone. No code change in this release.

**Planned fix (next maintenance release):** record each run's target folders in the journal alongside its run ID, and have cleanup check those folders directly with the same exact-name and journal-ownership rules, independent of ignore rules. Keep a run ID until all of its recorded folders have been checked. Regression test: the reviewer's reproduction above.

## Release procedure (unchanged)

On Windows, from the extracted folder: `pip install PyQt6`, then `run_tests.bat` → expect **52 passed, 6 skipped**. Then `python tests\smoke_real_github.py you/throwaway --i-understand you/throwaway` → expect **11/11**. If both pass, freeze 0.2.1.

---

# 0.2.1: Windows and live GitHub verification (release gates)

Run by Leon on Windows 10 (build 19045.6466), Python 3.11, PyQt6 installed, from `G:\Projects\_Github_Tools\ghsync`.

**Live GitHub smoke test** against a disposable repository: **11/11 passed**: upload, download, remote and local deletes, conflict report and resolution, remote `.ghsync/` and ignored files refused, newly ignored file kept on GitHub, concurrent run refused, re-baseline deletes nothing.

**Test suite:** 58 run, 6 skipped (the 4 POSIX-symlink tests and 2 POSIX-only tests, as documented), **57 passed, 1 failed**. All four real NTFS junction tests, both `msvcrt` interprocess-lock tests, all GHS-009 tests (including the hard-crash recovery) and all four GUI tests passed.

**The one failure was a test defect, not a ghsync defect.** `test_remote_windows_unsafe_names_are_rejected` checked `os.path.lexists(<project>\CON.txt)`. On Windows 10, `CON.txt` in any folder names the console device, so that check is True whether or not a file exists. That is exactly why ghsync's path policy rejects the name. The test now checks the real directory listing (and that the folder holds only `.ghsync` and `normal.txt`). `ghsync.py` and `ghsync_gui.py` are byte-identical to the build that was tested; only `tests/` changed. The same edit also closes the test files, subprocess pipes and HTTP server sockets behind the ResourceWarnings in the Windows run (the suite now runs clean with `-W error::ResourceWarning`). The Qt "cannot find font directory" line comes from the offscreen test platform and is harmless.

**Expected on re-run:** `Ran 58 tests ... OK (skipped=6)`.

| Gate | Status |
|---|---|
| GHS-002 containment (native junctions) | **Pass on Windows** |
| GHS-005 interprocess lock (`msvcrt`) | **Pass on Windows** |
| GHS-007 GUI plan binding | **Pass on Windows** |
| GHS-008 live GitHub smoke test | **11/11** |
| GHS-010 | Documented, deferred (accepted) |

Recommendation: freeze ghsync 0.2.1 after the corrected suite re-runs clean on the same machine.

---

# 0.2.2: ignore files (feature change after the 0.2.1 freeze)

**Requested change:** ghsync must respect `.gitignore` and `.packagerignore` without merging them into one file and without taking on Project Packager code.

- `.ghignore`, `.gitignore` and `.packagerignore` in the project root are each read on their own; a path matched by any of them is not synced in either direction. Before, `.gitignore` was only read when there was no `.ghignore`, and `.packagerignore` wasn't read at all. ghsync never writes to these files.
- Reading follows Project Packager's rules: strict UTF-8 (a BOM is fine), and a file that exists but can't be read fails closed. Nothing is synced and the error names the file. Before, `errors="ignore"` could silently damage a pattern so the file it named was uploaded.
- `!` force-include lines are skipped: ghsync never un-ignores. That is the safe direction, because since GHS-003 an ignored file is held and never deleted. `\!` and `\#` escapes are honoured. `status` prints which ignore files are in effect.
- Engine change only: `load_ignore()` and a new `ignore_files()`. The GUI is untouched.

Tests (`TestIgnoreFiles`, 9): 7 fail on 0.2.1 and pass on 0.2.2. The other 2 are guards that pass on both: a newly ignored file is not deleted from GitHub, and force-include lines don't un-ignore. Full suite here: 63 passed, 4 skipped (the Windows-only junction tests). Expected on Windows: **67 run, OK (skipped=6)**. Not yet run on Windows or against live GitHub.

**Behaviour change to note:** for a project with both `.ghignore` and `.gitignore`, files matched only by `.gitignore` become held on the next run. They stop syncing but are not deleted on either side.
