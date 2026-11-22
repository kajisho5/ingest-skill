# ADR-015: Rename from a template, owned here, kept in step with the manifest and the ASC MHL history

2026-11-22. Status: Accepted. EXPERIMENTAL (`ingest.rename`, 1.1.0).

## Context

After a card offload of a conference or an event, the copy holds hundreds of camera names (`C0001.MP4`, `DSC_1234.MOV`). Editors want
sortable names built from what is known about each clip, for example `2026-11-22_ConfName_Room-A_CamB_S01_Speaker-Tanaka_0001.mp4`.

Where renaming lives follows from the ownership map (Velisa ADR-012, `ecosystem.json`). This repository owns 素材取り込み(検証付きコピー)
and 素材台帳: the verified copy, its `ingest-manifest.json` and its ASC MHL history. A rename inside a verified copy changes the paths
those records are keyed by, so a rename done anywhere else either breaks `verify` (every renamed file is missing, every new name
unlisted) or has to write this skill's manifest and ASC MHL formats from outside. Only this repository can rename and keep its own
records true. video-production-agent owns スキルの呼び分け and 制作計画: the speaker names (its entity memory, diarization-skill's
proposals) and any analysis it orchestrates. Those are inputs to a name, not the rename.

## Decision

1. **`ingest rename plan | apply | undo`**, a new `provides` entry `ingest.rename`, `EXPERIMENTAL`. The `STABLE` tools keep their
   behaviour; the additions they see are listed under Consequences.
2. **Fields.** Per inventory clip (one per recording): `date` / `time` (the wall-clock start of ADR-008: the camera's local clock;
   else a UTC time moved by `--utc-offset`, or kept in UTC and said so; the file's modification time only with `--date-from-mtime`),
   `reel` and `camera` (the card's reel; a fingerprint reel is never a camera), `model`, `seq` (capture order, untimed clips after the
   timed ones by name), `orig`, `ext` (lower case). Everything else comes from the caller: `--set field=value` for every clip, and a
   fields file `ingest/rename-fields@1` keyed by clip path, clip id or manifest hash, each value optionally with its `source`. This
   skill does not choose speakers or scenes. Every value in the preview carries the reason it was chosen.
3. **Template.** `{name}` or `{name:spec}`; spec `[prefix][0N]` (`{scene:S02}` → `S01`, `{speaker:Speaker-}` → `Speaker-Tanaka`) or a
   strftime format for `date` / `time`. It must end with `{ext}`. A literal made only of separators joins two present fields, so a
   missing field leaves no doubled, leading or trailing separator. A clip with no value except `seq` / `orig` / `ext` keeps its name.
4. **Safe on macOS, Windows and Linux.** NFC; whitespace, `<>:"/\|?*` and control characters become `-`; Windows device names
   (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`, with the superscript digits) get `_` after them; no trailing dot or space; the name
   of the media file and of every sidecar at most 255 UTF-8 bytes (the smallest of the three systems' limits), with room kept for a
   counter. A path over 259 characters is warned about (Windows tools without long-path support).
5. **Preview first.** `plan` writes nothing and returns every row (old → new, fields, reasons, status) and a `plan_id`: a digest of
   the folder, the mode, the template and every move with each source's size and mtime. `apply` takes `--plan-id` and recomputes the
   plan; a different one is `PLAN_CHANGED` and nothing is done. The caller (a person on the CLI, the agent's confirmation step) agrees
   to exactly the rows it saw.
6. **Never overwrites.** Names are compared without case and in NFC (macOS and Windows file systems ignore case). A name already in
   the folder, or given to another clip of the plan, gets `_2`, `_3`, ...; a clip may take its own name in another case (through a
   temporary name). Every current name counts as taken, so no rename waits on another one. A name given to one clip is never given to another, even when it is that clip's own name in another case. A rename onto an existing name is allowed only when both names are the same file (a case change on a case-insensitive file system); the new name is made with a hard link where the file system has them, which fails when the name exists, so a file created meanwhile is never replaced.
7. **Sidecars stay with their media.** Files in the clip's folder whose name starts with the clip's basename (the clip with the longest such basename: `Interview2.xml` goes with `Interview2.mp4`, not `Interview.mp4`) (its sidecars, its related
   files, and XML / XMP / LRF / THM / SRT / ... found beside it) get the new basename with the rest of their name kept
   (`C0001M01.XML` → `<new>M01.xml`). Other files (card indexes, notes) are left alone.
8. **In place** only on an offloaded copy (a manifest: the card itself is never modified), refused while the copy is incomplete
   (`failed` or `growing` in the manifest), and only for `generic` clips: a Sony / AVCHD / XDCAM EX / BRAW structure names its clips in
   index files that NLEs read, so those rows are `skipped` with the reason. Before anything moves, every file of the plan is re-hashed
   against the manifest; one mismatch stops the run (`VERIFY_MISMATCH`, exit 3). Then: the undo manifest `ingest-rename/<time>.json`
   (`ingest/rename-undo@1`, status `pending`), the renames (a failure puts back those done), the manifest with the new paths and an
   optional `renames` log (and a `card` record when it had none, so a clip-name or fingerprint reel does not change with the names),
   and one ASC MHL generation, process `in-place`, each renamed file at its new path with `<previousPath>` and its hash `verified`.
   Reading a history, the records of a previous path move to the new path from that generation on, so `verify` checks every earlier
   generation's hash against the renamed file. `undo` re-hashes the renamed files, renames them back and writes the manifest and a
   generation the same way. `ingest-rename/` is a top-level folder that `scan` skips (status `skipped_rename`), like `ingest-proxies/`.
   A damaged ASC MHL history refuses the rename before anything moves; a manifest that cannot be written puts the names back; a generation that cannot be written makes the run `ok: false` (undo restores the names).
9. **Copy** (`--copy-to DIR`, outside the folder): the clips (spanned members as `_p01`, `_p02`, ...) and their sidecars are copied
   flat into DIR under the new names, each through the offload's verified copy (temporary file, fsync, read-back), compared with the
   manifest hash when there is one. DIR gets its own manifest (with `renamed_from`) and ASC MHL history, so `verify DIR` passes. This
   is the way to rename a structured card's clips. Every clip is copied, a clip without metadata under its own name. There is nothing to undo in the folder; the undo record lists the copies.

## Consequences

- Additive contract change: provides `ingest.rename`, tool `ingest/rename`, error codes `TEMPLATE_INVALID`, `FIELDS_INVALID`,
  `PLAN_CHANGED`, `RENAME_REFUSED`, `UNDO_INVALID` (exit 2), the top-level `rename` key, two `not_provided` lines; one `not_provided`
  line no longer lists rename. The manifest keeps `ingest/manifest@1` with two new optional keys (`renames`, `renamed_from`); readers
  that do not know them ignore them, as with `failed` and `growing`.
- `verify` (STABLE) honours `previousPath` in an ASC MHL history. A history without it reads exactly as before. The reference ASC MHL
  implementation was not run against a history written here with `previousPath`; the element is placed right after `<path>`.
- A later `offload` into a renamed copy does not follow the renames: it would copy the card again under the original names. Rename
  once the offload is complete. Renaming one destination does not rename the second backup; the same plan applied there gives the
  same names when its facts are the same.
- Proxies made by `offload --proxy` keep their file names; the inventory still links them by hash.
- The agent's smart rename (video-production-agent) gathers speakers from its entity memory and writes the fields file; it calls
  `plan`, shows the rows, and calls `apply` with the `plan_id` only after confirmation.
