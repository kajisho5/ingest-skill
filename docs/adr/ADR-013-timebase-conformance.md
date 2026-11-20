# ADR-013: Rates and timecode by the ecosystem timebase spec

2026-11-20. Amends ADR-008. The ecosystem rule is Velisa ADR-026 (decision D40, spec `velisa/timebase@1`). Issue #26.

## Context

The spec lists these places where `sync.py` differs from it:

- `tc_seconds` read labels that do not exist and reported no error:
  - a skipped drop-frame label (`00:01:00;00` at 29.97 became 59.993 s, the time of `00:00:59;28`);
  - a frame number at or above the nominal rate (`00:00:00:30`);
  - `;` at a rate with no drop-frame (25 fps), which it read as non-drop.

  A wrong start time moves a clip in a multicam sync group. It also returned a float.
- `parse_rate` sent names outside its table through `limit_denominator(1001)`, so `29.970` was 2997/100 and `47.952` was 5994/125. It also accepted rates above 1000.
- `tc_label` rounded through floats and wrapped at 24 hours.

## Decision

- **Rates (TB-2).** `N/D` is taken as written. A decimal is read exactly. A decimal that is not an integer and lies within 0.005 of base x 1000/1001 (base 24, 30, 48, 60, 120 or 240) is that NTSC rate. The `p` / `i` suffix of Sony `formatFps` stays: `i` names fields. A rate outside (0, 1000] is not a rate and gives `None`, as an unreadable one always did.
- **Timecode (TB-6).** `tc_frames` reads a label exactly. It raises `TimecodeError` with the spec's code in these cases:
  - `INVALID_TIMECODE`: a malformed label, hours above 23, or a frame number at or above the nominal rate;
  - `DROP_FRAME_UNSUPPORTED`: drop-frame at a rate other than 30000/1001 or 60000/1001;
  - `NONEXISTENT_LABEL`: a skipped drop-frame label.

  `tc_seconds` returns the exact `Fraction`. The nominal rate rounds a half up. `tc_label` counts the nearest frame on fractions, a half up. It gives no label for 24 hours or more and never wraps.
- **In the inventory.** When the start timecode is refused, the clip's `sync.start_timecode` still reports the label as the source states it. `start_tod_s` stays null, so the clip is not placed on the timecode basis. A new key, `sync.start_timecode_error` (`{code, message}`), says why. The key is present only on such clips.
- **Conformance.** `tests/fixtures/timebase/timebase-vectors.json` is a byte-for-byte copy of Velisa's `docs/specs/timebase-vectors.json` at Velisa commit `b7df1d82586f0b7c00fed4074aecac75cbadf893`, and its sha256 is checked. `tests/test_timebase_vectors.py` runs the 98 vectors of the functions this skill has, the optional `p` / `i` ones included. It counts the 36 vectors this skill does not offer.

## Consequences

- The contract file does not change. The inventory output gains one optional key, `sync.start_timecode_error`. This is an additive change.
- A clip whose start timecode does not exist is left out of timecode-basis sync groups instead of being placed at a neighbouring frame's time. It can still be grouped by wall clock.
