# Jeju Cafe Chroma Seed

This folder contains the RAG seed docs generated only from:

```text
data/share/jeju_cafe_pipeline_share.json
```

Files:

- `jeju_cafe_public.jsonl`: public recommendation docs, 410 rows.
- `jeju_cafe_review.jsonl`: internal review docs, 58 rows.
- `manifest.json`: source and document-count manifest.

Build local Chroma embeddings:

```bash
python pipeline/embed.py
```

The Chroma persistent directory is:

```text
chroma_db/
```

`chroma_db/` is generated locally and is intentionally ignored by git. Team members can rebuild it from the checked-in JSONL seed files.
