# ADR-003: Card-Structure-Aware Inventory, Spanned Clips, Camera Sidecars and Reel Naming

Status: Accepted

## Context

`ingest inventory` listed every media file on its own. Camera cards are not flat folders, and an event crew offloading Sony, AVCHD or Blackmagic cards needs more than a file list:

- **One recording can be several files.** XDCAM EX on FAT32 media splits a take into numbered clip folders. AVCHD splits a long recording into several `.MTS` streams.
- **Proxies and index files look like media.** A Sony `Sub/…S03.MXF` proxy, for example, was listed as a clip.
- **The camera writes useful facts in sidecars.** Examples are the Sony `…M01.XML` files, `MEDIAPRO.XML`, and the Blackmagic `.sidecar` files.
- **A card mounts as `Untitled` or `NO NAME`.** The source folder name is often no use as a reel name.

`ECOSYSTEM_ROADMAP.md` step 4 asks for spanned clips to be grouped, camera sidecars to be read, and reels / cards to be named. The bar is: "fixture card trees give one clip per recording".

This change must not affect what is copied. NLEs (Premiere, Resolve, Catalyst, Avid AMA) need the card's index files and sidecars next to the media. The offload must therefore stay a byte-for-byte copy of the whole card folder.

**Sources** (read on 2026-10-30). No layout below is guessed: each one comes from a camera vendor document or a long-established open-source reader.

- **MediaInfoLib** ([MediaArea/MediaInfoLib](https://github.com/MediaArea/MediaInfoLib), commit `a8a2598`, 2026-10-30):
  - `Source/MediaInfo/Multiple/File_Xdcam_Clip.cpp`:
    - a `<clip>M01.XML` sidecar has the root element `NonRealTimeMeta`;
    - it describes the `<clip>.MXF` or `<clip>.MP4` beside it;
    - it is read for `CreationDate@value`, `LastUpdate@value`, `Duration@value` (frames), `LtcChangeTable@tcFps` and `Device@manufacturer` / `@modelName`.
  - `Reader/Reader_Directory.cpp` covers the XDCAM `Clip/` and BDMV directory detection.
- **Adobe XMP Toolkit SDK** ([adobe/XMP-Toolkit-SDK](https://github.com/adobe/XMP-Toolkit-SDK), commit `7093513`, 2025-11-03):
  - `XMPFiles/source/FileHandlers/XDCAM_Handler.cpp` documents the FAM layout:
    - `MEDIAPRO.XML`, `DISCMETA.XML`, `General/`;
    - `Clip/C0001.MXF` with `C0001M01.XML`, `…M01.XMP` and `…R01.BIM`;
    - `Sub/C0001S01.MXF`, `Edit/`, and `Take/` (XDCAM Memory SxS).
    - Its note on case: "Sony documentation uses the folder names General, Clip, Sub, and Edit … we want to be case insensitive."
  - `XDCAMFAM_Handler.cpp` (format check): `MEDIAPRO.XML` beside `Clip/`, and a clip is `<name>.MXF` with `<name>M01.XML`.
  - `XDCAMEX_Handler.cpp` documents the `BPAV/` layout:
    - `MEDIAPRO.XML`;
    - `CLPR/<take>_<nn>/<take>_<nn>.MP4` with `…M01.XML`, `.SMI`, `R01.BIM` and `I01.PPN`;
    - `TAKR/<take>/`.
    - "YY is ClipSuffix (as single take can be divided across multiple clips.) In case of spanning, all the clip folders starting from `MIXXXX_` are looked for."
  - `FormatSupport/XDCAM_Support.cpp`:
    - the `NonRealTimeMeta` fields it reads: `TargetMaterial@umidRef`, `Title@usAscii`, `VideoFormat/VideoFrame@formatFps` / `@videoCodec`, `VideoFormat/VideoLayout@pixel` / `@numOfVerticalLine` / `@aspectRatio`, `AudioFormat/AudioRecPort@audioCodec`, and `Device@serialNo`;
    - `MEDIAPRO.XML` is `MediaProfile/Contents/Material@uri` / `@umid` / `@title`.
  - `AVCHD_Handler.cpp`:
    - the `BDMV/` layout, with `PLAYLIST/`, `STREAM/`, `CLIPINF/`, `index.bdmv` and `MovieObject.bdmv`;
    - the 8.3 names used on cards (`.MPL`, `.MTS`, `.CPI`, `INDEX.BDM`, `MOVIEOBJ.BDM`);
    - `ReadAVCHDPlaylistMarkID`: the shot metadata of a clip is found through the **entry mark** (`mark_type` 1) that points at its PlayItem.
- **libbluray** ([videolan/libbluray](https://code.videolan.org/videolan/libbluray), commit `f33a738`, 2026-10-22). These give the byte layout of a playlist:
  - `src/libbluray/bdnav/bdmv_parse.c`: `MPLS` + version `0100` / `0200` / `0240` / `0300`.
  - `src/libbluray/bdnav/mpls_parse.c`:
    - the playlist and mark start addresses;
    - each PlayItem: a 16-bit length, a 5-character clip name, codec `M2TS` / `FMTS`, 11 reserved bits, `is_multi_angle`, and a 4-bit `connection_condition` (1, 5 or 6);
    - each PlayListMark: 14 bytes, made of reserved, `mark_type`, `play_item_ref`, time, PID and duration.
  - `mpls_data.h`: `BD_MARK_ENTRY` 0x01, `BD_MARK_LINK` 0x02.
- **Sony operating manual, PDW-F800** (Root directory / Clip directory; [manualslib copy](https://www.manualslib.com/manual/951325/Sony-Pdw-F800.html?page=229)):
  - `MEDIAPRO.XML` "Contains a list of materials on the disc, basic properties, related information, and information about access methods";
  - `C*M01.XML` is the "Metadata file generated automatically when C*.MXF file is created".
- **Sony Help Guide, BRC-AM7 "Proxy Recording Overview"** ([helpguide.sony.net](https://helpguide.sony.net/rc/brc-am7/v1/en/contents/proxy_recording_overview.html)):
  - proxy folders are `/PRIVATE/XDROOT/SUB` and `/XDROOT/SUB` for MXF, and `/PRIVATE/M4ROOT/Sub` and `/M4ROOT/Sub` for MP4;
  - the proxy file name is "the clip name … and an S03 suffix".
- **Sony Help Guides, Clip Name Format** ([ILME-FR7](https://helpguide.sony.net/ilc/2240/v1/en/contents/TP1000673465.html), [ILME-FX3A](https://helpguide.sony.net/ilc/2510/v1/cs/contents/0601_set_xavc_s_file_name.html)):
  - "Cam ID + Reel#" is "Camera ID + Reel Number + Shot Number + date + random string";
  - the Camera ID is A–Z and the Reel Number is 001–999;
  - "the first four characters of clip names are determined for each recording media". The example given is `A001C001_230101AB`.
- **Blackmagic Pocket Cinema Camera manual** ([documents.blackmagicdesign.com](https://documents.blackmagicdesign.com/UserManuals/BlackmagicPocketCinemaCameraManual.pdf), PDF dated 2025-03-23):
  - "File Naming Convention": `A001_08151512_C001` is camera index, reel number, month, day, hour, minute, clip number.
  - "Sidecar Files": the sidecar sits "in the same folder as the corresponding Blackmagic RAW file". It is "a human readable format", and the manual publishes no field list for it.
- **Panasonic AG-CX350 operating instructions, "Handling the recording data"** ([pro-av.panasonic.net](https://pro-av.panasonic.net/manual/html/AG-CX350EJ(DVQP1833RA)_E/chapter03_06_07.htm)): MOV file names begin with a camera index (A–Z) and a three-digit card number, followed by the clip number.
- **Grass Valley EDIUS Neo 3 user manual**, AVCHD export ([manualsdir copy](https://www.manualsdir.com/manuals/795914/grass-valley-edius-neo-3.html?page=730)):
  - the card tree is `AVCHD/BDMV/{CLIPINF,PLAYLIST,STREAM}`, plus `INDEX.BDM` and `MOVIEOBJ.BDM`;
  - "a PRIVATE folder is created between the ROOT and AVCHD folders" on SD cards;
  - "The limit on AVCHD stream files is 4GB … it is split into multiple files."

## Decision

1. **Grouping is inventory and reporting only.**
   - `offload` still copies every regular file of the folder byte-for-byte, under its own name and path. That includes sidecars, index files, playlists, proxies, thumbnails and unknown files.
   - Card structure never decides whether a file is skipped or what it is called inside the card folder. A test copies every fixture card and compares every file's sha256.
   - The one new effect on `offload` is opt-in: `--name-by reel` names the card's folder in each destination (item 5).
2. **Recognised structures** (new module `cards.py`). Folder names are matched case-insensitively, wherever they sit under the folder.

   | id | Detected by | One clip is | Sidecar read | Also listed with the clip |
   |---|---|---|---|---|
   | `sony_xdcam` | a folder with `MEDIAPRO.XML` and `Clip/`. The layout is reported as `XDROOT`, `M4ROOT` or `FAM` (e.g. a Professional Disc root) | `Clip/<name>.MXF` or `.MP4` | `Clip/<name>M01.XML` (`NonRealTimeMeta`), and its `MEDIAPRO.XML` entry | `Sub/<name>S<nn>.*` as **proxies** (not clips); `<name>[A-Z]<nn>.*` (e.g. `R01.BIM`, `M01.XMP`) as related |
   | `sony_xdcam_ex` | `BPAV/CLPR/` | every `CLPR/<take>_<nn>/` of one take, **in `<nn>` order** (spanned) | each folder's `<take>_<nn>M01.XML` | `.SMI` / `.BIM` / `.PPN` and `TAKR/<take>/` as related |
   | `avchd` | `BDMV/` with `STREAM/` (8.3 or long names) | the `STREAM/<n>.MTS` files of one recording, in playlist order (see 3) | — | `CLIPINF/<n>.CPI` (listed, not interpreted) |
   | `blackmagic_raw` | `*.braw` | `<name>.braw` | — | `<name>.sidecar` beside it (listed, not interpreted) |
   | `generic` | every other media file | the file | — | same-name non-media files beside it (e.g. `.THM`) |

   - Card-level files are listed under `card.index_files`. Examples: `MEDIAPRO.XML`, `DISCMETA.XML`, `CUEUP.XML`, `INDEX.BDM`, `MOVIEOBJ.BDM` and the playlists.
   - An essence file missing from `MEDIAPRO.XML` is reported as a warning. It is still listed.
3. **AVCHD recordings come from the playlists, and nothing is grouped on a guess.**
   - Each `PLAYLIST/*.MPL` is parsed at the libbluray byte layout, with bounds checks, and is refused above 4 MB.
   - A recording starts at a PlayItem that an **entry mark** (type 1) points to.
   - A following PlayItem joins that recording only when **both** of these hold:
     - no entry mark points to it;
     - it is joined **seamlessly** (`connection_condition` 5 or 6).
   - A non-seamless item (1) always starts a new recording.
   - A playlist with no entry marks, or one that cannot be read, is not used, and a warning says so.
   - Playlists that disagree about a clip leave that clip on its own, with a warning.
   - A stream that no playlist names is listed on its own, with a warning.
   - A recording whose playlist names a stream that is not in the folder (continued on another card) is marked `incomplete`. A warning names the missing file.
   - The rule follows the Adobe handler, which treats the entry mark on a PlayItem as the start of the shot (its shot metadata), and the EDIUS manual's statement that streams over 4 GB are split. No real-media sample was available to cross-check it, hence the conservative rule.
4. **Sidecar metadata: only what both references read, as the camera wrote it.**
   - From `NonRealTimeMeta` the inventory keeps:
     - `creation_date`, `last_update`;
     - `duration_frames`, `ltc_fps`;
     - `umid`, `title`;
     - `video` {`format_fps`, `codec`, `width`, `height`, `aspect_ratio`};
     - `audio_codec`;
     - `device` {`manufacturer`, `model`, `serial`}.
   - From `MEDIAPRO.XML` it keeps the clip's `umid` and `title`.
   - A field that is not in the file is **absent**. Nothing is computed or inferred.
     - There is no start timecode: the `LtcChange@value` encoding is not specified in either source.
     - There is no duration in seconds: `formatFps` values such as `59.94i` are kept as written.
   - Everything else in these files is not interpreted. That includes the AVCHD `.CPI` files and the Blackmagic `.sidecar`, whose fields Blackmagic does not publish in the manual read here.
   - The XML parser is the stdlib `ElementTree`, behind two guards, as for ASC MHL files in ADR-001:
     - a file containing `<!DOCTYPE` or `<!ENTITY` is refused;
     - a file over 16 MB is not read.
   - A sidecar that cannot be read is reported with `parsed: false` and a `detail`. Its clip is still listed.
5. **Reel / card id, in this precedence.**
   1. **`operator`**: `--reel ID` on `offload` or `inventory`.
   2. **`manifest`**: the reel that the offload which made this copy recorded in `ingest-manifest.json`. An operator-given reel therefore survives on the copy.
   3. **`clip_names`**: every recording's essence name starts with the same camera ID + reel number. The prefix covers:
      - Sony "Cam ID + Reel#" `A001C001_…`, which Panasonic's MOV naming also matches;
      - Blackmagic `A001_08151512_C001`.

      If **any** recording does not match, this source is not used. If they name more than one reel, it is not used either, and a warning lists the reels found.
   4. **`fingerprint`**: `CARD-` followed by the first 8 hex digits of the sha256 of the sorted list of `path<TAB>size` of the files offload copies. It is deterministic: the same on the card and on every complete copy, and different when a file is added.
      - The source folder name was rejected as a fallback: cards mount as `Untitled` / `NO NAME`.
      - The volume label was rejected because the standard library cannot read it portably.
   - The result is `card.reel = {"id", "source", "fingerprint", ...}` in the inventory and `card.reel` in the offload report.
   - An operator reel must be one safe folder name (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`). Anything else is refused as `REEL_INVALID` (exit 2) before anything is written.
   - `offload --name-by reel` names the destination folder `<dest>/<project>/<reel id>`. The default stays `--name-by folder` (the source folder's name), so existing commands behave as before.
   - The reel for naming is derived from names and sizes only, and no file is opened. A re-run therefore gets the same folder and resumes.
   - `ingest-manifest.json` gains an optional `"card": {"reel", "reel_source"}`. The schema stays `ingest/manifest@1`, because readers ignore unknown keys.
6. **The inventory document becomes `ingest/inventory@2`.**
   - There is one entry in `clips` per recording, with `clip_id`, `structure`, `spanned`, `incomplete`, `members` (path, size, manifest hash, probed, duration_s, in order), `sidecars`, `proxies`, `related` and `metadata`.
   - `path`, `size`, `sha256` / `hash` and the ffprobe facts stay at the top level of a clip:
     - for a single-file clip they are the same values as before;
     - for a spanned clip, `size` is the sum and `duration_s` is the sum of the members' durations (unknown unless every member was probed), while `sha256` / `hash` are `null` and the per-member hashes are in `members`.
   - New top-level keys: `card` {`reel`, `structures`, `index_files`}, `file_count`, `spanned_count` and `warnings`.
7. **Not in this step.** These are listed in the contract's `not_provided`:
   - Panasonic P2, Canon XF and RED structures. Their media are listed file by file, as `generic`.
   - Interpreting `.CPI` or `.sidecar` contents.
   - Grouping a recording that continues on another card. Each card is inventoried on its own; the AVCHD case is marked `incomplete`.

## Verification

- `tests/test_cards.py` builds tiny synthetic card trees, with no real media:
  - Sony XDROOT: FX6-style `Cam ID + Reel#` names, `MEDIAPRO.XML`, `M01.XML`, an `S03` proxy, `General/` and `Take/`;
  - Sony M4ROOT (XAVC S): upper-case `CLIP/`, `THMBNL/`, a clip without a sidecar and missing from `MEDIAPRO.XML`;
  - XDCAM EX: a take spanned over `_01` and `_02`, plus a single-folder take;
  - AVCHD: a spanned recording, the no-guess cases (all entry marks, non-seamless, no marks, a corrupt playlist), and a recording incomplete on this card;
  - Blackmagic RAW with a `.sidecar`;
  - a recorder card with no metadata at all;
  - a card whose clip names carry two reels;
  - a sidecar containing a DTD.

  The playlist fixtures are written at the libbluray byte layout. A truncated playlist must be refused at every cut.
- Each fixture card is offloaded, and every file must arrive byte-identical, with `verify` passing. The inventory of the copy must give the same clips and the same reel, now with source `manifest`.
- `--name-by reel` and `--reel` are tested, including a resume into the reel-named folder and the `REEL_INVALID` refusals.

## Consequences

- Consumers of `ingest/inventory@1` must read `clips[]` as recordings. A proxy or a later member of a spanned recording is no longer an entry of its own. No other repository reads the inventory yet: the agent adapter is roadmap step 6.
- `inventory` now opens small XML and playlist files on the card, read-only and size-capped. It still never writes.
- The reel of a card whose clips do not all carry a camera reel is a fingerprint. A crew that wants a readable name passes `--reel`.
