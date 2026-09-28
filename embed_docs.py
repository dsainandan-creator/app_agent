"""
embed_docs.py – Chunk, embed, and store the three runbook text files into ChromaDB.

Files processed:
  dynatrace-alerts.txt   → collection "observability_docs"
  agent-runbook.txt      → collection "observability_docs"
  mcp-integration.txt    → collection "observability_docs"

Chunking strategy:
  Split on blank lines (paragraph boundaries), merge small paragraphs until
  each chunk is ~200 words. Chunks never cross section headings (lines of ===).

Embedding model: ChromaDB built-in (all-MiniLM-L6-v2 via ONNX — runs locally, no API key)
Vector store:    ChromaDB (local persistent storage at ./chroma_db/)
"""

import re
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

COLLECTION   = "observability_docs"
CHROMA_PATH  = Path(__file__).parent / "chroma_db"
TARGET_WORDS = 200
DOCS_DIR     = Path(__file__).parent

FILES = [
    "dynatrace-alerts.txt",
    "agent-runbook.txt",
    "mcp-integration.txt",
]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _is_heading(line: str) -> bool:
    return bool(re.fullmatch(r"[=\-]{3,}", line.strip()))


def _word_count(text: str) -> int:
    return len(text.split())


def chunk_text(text: str, target_words: int = TARGET_WORDS) -> list[str]:
    """
    Split text into chunks at paragraph boundaries.
    Merges consecutive paragraphs until the chunk reaches ~target_words,
    then starts a new chunk. Section headings (=== / ---) always force a new chunk.
    """
    raw_paragraphs = re.split(r"\n\s*\n", text.strip())
    paragraphs = [p.strip() for p in raw_paragraphs if p.strip()]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_words = 0

    for para in paragraphs:
        is_section_break = any(_is_heading(ln) for ln in para.splitlines())

        if is_section_break and current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts = [para]
            current_words = _word_count(para)
        else:
            current_parts.append(para)
            current_words += _word_count(para)
            if current_words >= target_words:
                chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_words = 0

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # ChromaDB built-in embedding function (all-MiniLM-L6-v2, runs locally via ONNX)
    ef = embedding_functions.DefaultEmbeddingFunction()

    chroma = chromadb.PersistentClient(path=str(CHROMA_PATH))

    # Delete and recreate so re-runs are idempotent
    try:
        chroma.delete_collection(COLLECTION)
        print(f"Deleted existing collection '{COLLECTION}'")
    except Exception:
        pass

    collection = chroma.create_collection(
        name=COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"Created collection '{COLLECTION}' in {CHROMA_PATH}\n")
    print("Embedding model: ChromaDB default (all-MiniLM-L6-v2, local ONNX)\n")

    total_chunks = 0

    for filename in FILES:
        filepath = DOCS_DIR / filename
        if not filepath.exists():
            print(f"  SKIP  {filename} (file not found)")
            continue

        text   = filepath.read_text(encoding="utf-8")
        chunks = chunk_text(text)

        print(f"  {filename}")
        print(f"    -> {len(chunks)} chunks, {sum(_word_count(c) for c in chunks)} words total")

        ids       = [f"{filename}::chunk_{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "source":      filename,
                "chunk_index": i,
                "word_count":  _word_count(chunks[i]),
            }
            for i in range(len(chunks))
        ]

        # ChromaDB embeds the documents automatically using the collection's EF
        collection.add(
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
        )

        for i, chunk in enumerate(chunks):
            preview = chunk.splitlines()[0][:70]
            print(f"      [{i}] {_word_count(chunk):>3} words  \"{preview}...\"")

        total_chunks += len(chunks)
        print()

    print(f"Done. {total_chunks} chunks embedded and stored in ChromaDB.")
    print(f"Collection '{COLLECTION}' at: {CHROMA_PATH.resolve()}\n")

    # Sanity query
    query = "how does the agent escalate a SEV-1 incident?"
    print(f"Sanity check -- querying: \"{query}\"")
    results = collection.query(
        query_texts=[query],
        n_results=3,
        include=["documents", "metadatas", "distances"],
    )
    for rank, (doc, meta, dist) in enumerate(zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ), 1):
        print(f"\n  Result {rank} | source={meta['source']} chunk={meta['chunk_index']} "
              f"| distance={dist:.4f}")
        print(f"  {doc[:200].replace(chr(10), ' ')}...")


if __name__ == "__main__":
    main()
