# Legacy blind-listening migration

VNTTS owns generic blind model listening. The extractor no longer installs the
`r1999-listen` command or its Qt workbench. Existing sessions remain valid local
authoring evidence and do not need to regenerate model audio.

Use the VNTTS authoring importer to validate a session before copying it:

```bash
cd /path/to/VisualNovelTextToSpeach
uv run vntts-pregenerate inspect-listening /path/to/listening-session
uv run vntts-pregenerate import-listening /path/to/listening-session
```

Both commands emit JSON. Inspection is read-only. Import copies the session,
hidden model key, report, and neutral audio aliases into VNTTS application data;
it does not modify or delete the extractor-owned source directory. The import
result reports the exact destination. Resume that copy with its `session.json`:

```bash
uv run vntts-listen status --session /reported/destination/session.json
uv run vntts-listen ui --session /reported/destination/session.json
uv run vntts-listen report --session /reported/destination/session.json
```

VNTTS validates the legacy session, hidden key, report, relative audio aliases,
and available source audio by schema and SHA-256 before import or resume. It
preserves trial order, A/B assignment, recorded preferences, and the separate
blind key. Repeating an identical import is idempotent; a changed source or a
conflicting destination is rejected instead of overwritten.

The compatibility gate used for this ownership move loaded the completed
45-trial, six-model local session and all 90 neutral aliases without rewriting
any of its 93 session, key, report, or audio files. New listening work should use
`vntts-listen`; the extractor remains responsible only for game-derived source
artifacts and source-reference audition.
