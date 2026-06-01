# dead-db phase 3 — Deadcast fetcher (corpus #3, local-HTML path)

Supersedes SPEC_deadcast_DEFERRED.md. Local-file fetcher — no network, no Whisper, no iGPU.
Pages saved to NAS by hand. 10 *.html files (8 WD50 + 2 BONUS) in initial batch.

Key decisions: largest field--name-body div (script/style-aware sizing); strip bold tags
before html_to_text (speaker labels); sections=None (flat chunking); published=None;
source_id = canonical URL; lead line [Deadcast Season N, Episode M: title].

Files: lore/fetchers/deadcast.py, lore/build_deadcast.py
