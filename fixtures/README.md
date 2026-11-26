# Fixtures

All fixtures are **synthetic**. No Google Drive text, private transcript,
account, token or key is stored here.

- The authoritative fixture definitions live in `src/nexus/fixtures.py`
  (`build_dlc`, `build_creator`, `build_creative`).
- `generated/t7_long_doc.txt` is exported by `scripts/build_db.py`; it is the
  synthetic 18,660-character "living document" used for the T7 measurement.
- Source refs use `SIM_DRIVE` / `SIM_EXTERNAL` / `SIM_VIDEO` and `sim://…`
  identifiers. Anchors are synthetic: `§…` for sections, `NO_TIMECODE` where a
  real timestamp is unavailable and must not be invented.
- Rebuild with `python scripts/build_db.py --db var\nexus.db`; the SQLite file
  is git-ignored and fully reconstructible.
