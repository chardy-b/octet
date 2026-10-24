# Tidal CLI on Sonata

Standalone playlist management using `tidalapi==0.8.11`, matching the working
Octet installation. This CLI contacts Tidal directly. It does not import the
Octet server, use SSH, or require Octet to be switched on.

From `D:\dev\octet` in PowerShell:

```powershell
.\tools\tidal\tidal.cmd status
.\tools\tidal\tidal.cmd search "Yazz Ahmed Jamil Jamal"
.\tools\tidal\tidal.cmd playlists
.\tools\tidal\tidal.cmd show
```

`show` and `plan` default to **agape 2026**, playlist
`18f798bf-9650-417c-9dcd-98ee1c15793f`. Pass another UUID to work on another
playlist. `show` returns every track in saved order, including unavailable
tracks and duplicate occurrences.

## Local authentication

The imported session is at
`%LOCALAPPDATA%\OctetTidal\session.json`, outside the repository. The directory's
Windows ACL permits only the signed-in Sonata user. Credentials and refreshed
tokens are never printed by the CLI or included in playlist exports. The local
session refreshes and saves itself independently after the initial import.

For a fresh authorization directly with Tidal:

```powershell
.\tools\tidal\tidal.cmd login
```

Follow the displayed Tidal login link. Octet is not involved. `TIDAL_STATE_DIR`
can select an alternative state directory; create it with private permissions
before using it. Concurrent commands using the same state directory serialize
their access, including token refreshes.

## Updating a playlist

Create a fresh plan immediately before editing:

```powershell
.\tools\tidal\tidal.cmd plan --main-tracks 80 --output out\agape-2026\next-change.json
```

Edit the JSON:

- Keep `expected_ids` and `expected_metadata` unchanged. They identify the exact
  state reviewed when making the plan.
- Put the complete desired sequence in `ordered_ids`, including encore tracks.
  Track IDs must be strings. Preserve each intended duplicate occurrence.
- For new catalog IDs, include them in both `addition_ids` and `ordered_ids`.
- To remove a track entirely, include its ID in `remove_ids` and remove it from
  `ordered_ids`. Removals must refer to a single unambiguous occurrence.
- To move a track into the encore, change only its position in `ordered_ids`.
- Set `description` to the desired playlist description.
- If present, update `main_tracks` and `main_seconds` to match the new program.
  These are checked before any mutation, including during a dry run.

Review and apply:

```powershell
.\tools\tidal\tidal.cmd apply out\agape-2026\next-change.json --dry-run
.\tools\tidal\tidal.cmd apply out\agape-2026\next-change.json
```

Apply backs up the original order and the plan under
`%LOCALAPPDATA%\OctetTidal\backups\<timestamp>\`, verifies additions/removals
and each move against uncached, fully paginated Tidal reads, then verifies the
final order and metadata through a new playlist object. It preserves the title
and visibility. An error stops further edits; inspect the current playlist and
the saved `before.json`/`plan.json` before preparing a fresh continuation plan.
Do not blindly rerun a partially applied plan.

The CLI does not clear playlists or delete/recreate them. A dry run reads Tidal
and may refresh the local login, but never modifies the playlist. An unchanged
plan can be applied to verify the write workflow without changing its order or
description.

## Agape 2026 handoff

Latest approved program before this installation:

- Main program: **80 tracks, 9:00:09**, starting 9 p.m. and ending 6:00:09 a.m.
- Encore: **38 tracks**, starting at track 81 with Sakamoto's “Andata.”
- “Dream” replaces “Ranjani” and plays once; “Shofukan” remains in the jazz section.
- Main closes with Beverly Glenn-Copeland's original “Ever New.”
- Tayos Caves i–iii → Ascending, Dawn Sky → Mohabbat remains the dawn sequence.
- Complete *Promises* is together in the optional encore.

Local files already on Sonata:

- `out/agape-2026/dream-program-and-encore.txt`: latest approved timed running order.
- `out/agape-2026/sonata-current-playlist.json`: direct Sonata/Tidal export.
- `out/agape-2026/sonata-current-plan.json`: fresh no-op plan exported on Sonata.
- `out/agape-2026/dream-verified-20261021.json`: prior verified Octet readback.
- Other `out/agape-2026/*-plan-*.json` and snapshots: historical plans and backups.
  Those plans describe old before-states; generate a fresh plan for new edits.

Main program versus encore is a sequencing convention documented in the playlist
description; Tidal does not provide section headers. In the Tidal app use
**Custom order**, shuffle off. For Octet playback, use its **Refresh playlists**
button to update cached metadata after local edits. The CLI itself never uses
the Octet cache or changes the active playback queue.

## Installation and validation

```powershell
.\tools\tidal\install-local.ps1
# Optional one-time session import; requires Octet only for this initial copy:
.\tools\tidal\install-local.ps1 -ImportOctetSession
# Checks without network access or real playlist mutations:
.\.venv-tidal\Scripts\python.exe -m unittest discover -s tools/tidal -p test_tidal_cli.py
```

Installation creates an isolated `.venv-tidal` environment, ignored by Git.
The import refuses to overwrite an existing local session. On another platform,
install `requirements.txt`, create a private `TIDAL_STATE_DIR`, and run
`python tools/tidal/tidal_cli.py` instead of the Windows launcher.

Library references: [authentication](https://tidalapi.netlify.app/login) and
[playlist editing](https://tidalapi.netlify.app/playlist).
