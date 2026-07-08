"""Embed share-json based Jeju cafe RAG seed docs into local Chroma collections.

Input seed files are generated from data/share/jeju_cafe_pipeline_share.json only:
- data/rag/jeju_cafe_public.jsonl
- data/rag/jeju_cafe_review.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import chromadb
from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PUBLIC_JSONL = ROOT / "data" / "rag" / "jeju_cafe_public.jsonl"
DEFAULT_REVIEW_JSONL = ROOT / "data" / "rag" / "jeju_cafe_review.jsonl"
DEFAULT_CHROMA_DIR = ROOT / "chroma_db"
DEFAULT_MODEL = "text-embedding-3-large"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            docs.append(doc)
    return docs


def chroma_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    clean: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
        else:
            clean[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return clean


def batches(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def embed_texts(client: OpenAI, texts: list[str], model: str) -> list[list[float]]:
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


def upsert_docs(
    client: OpenAI,
    chroma_client: chromadb.PersistentClient,
    collection_name: str,
    docs: list[dict[str, Any]],
    model: str,
    batch_size: int,
    sleep_seconds: float,
) -> int:
    collection = chroma_client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine", "embedding_model": model},
    )
    if not docs:
        return 0

    for batch in batches(docs, batch_size):
        ids = [str(doc["id"]) for doc in batch]
        texts = [str(doc.get("text") or "") for doc in batch]
        metadatas = [
            chroma_metadata(
                {
                    **(doc.get("metadata") or {}),
                    "collection": collection_name,
                    "source": doc.get("source", ""),
                }
            )
            for doc in batch
        ]
        embeddings = embed_texts(client, texts, model)
        collection.upsert(ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return len(docs)


def api_key() -> str:
    load_dotenv(ROOT / ".env")
    key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY")
    if not key:
        raise RuntimeError("Missing OPENAI_API_KEY or OPENAI_KEY in environment/.env")
    return key


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed Jeju cafe RAG seed docs into Chroma")
    parser.add_argument("--public-jsonl", type=Path, default=DEFAULT_PUBLIC_JSONL)
    parser.add_argument("--review-jsonl", type=Path, default=DEFAULT_REVIEW_JSONL)
    parser.add_argument("--chroma-dir", type=Path, default=DEFAULT_CHROMA_DIR)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--skip-review", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    public_docs = load_jsonl(args.public_jsonl)
    review_docs = [] if args.skip_review else load_jsonl(args.review_jsonl)

    openai_client = OpenAI(api_key=api_key())
    chroma_client = chromadb.PersistentClient(path=str(args.chroma_dir))

    public_count = upsert_docs(
        openai_client,
        chroma_client,
        "jeju_cafe_public",
        public_docs,
        args.model,
        args.batch_size,
        args.sleep_seconds,
    )
    review_count = upsert_docs(
        openai_client,
        chroma_client,
        "jeju_cafe_review",
        review_docs,
        args.model,
        args.batch_size,
        args.sleep_seconds,
    )
    print(f"Embedded public docs: {public_count}")
    print(f"Embedded review docs: {review_count}")
    print(f"Chroma path: {args.chroma_dir}")


if __name__ == "__main__":
    main()
