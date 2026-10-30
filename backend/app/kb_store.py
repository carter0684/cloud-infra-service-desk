"""
Knowledge Base store for RAG-style context injection into Bedrock prompts.

Design:
  - Documents are chunked into ~500-char overlapping windows on upload.
  - Chunks are stored alongside each KB document record (SQLite locally,
    DynamoDB in production — see sqlite_repo.py / dynamo_repository.py).
  - Retrieval uses simple TF-style keyword overlap scoring — no extra
    dependencies. This module is backend-agnostic: it takes an already-
    fetched list of KB document dicts (each with a "chunks" list), so it
    works identically regardless of which repository fetched them.
  - Top-K chunks are injected into the analyse_job prompt as context.

For production, swap retrieve_relevant_chunks() with an Amazon Bedrock
Knowledge Base (bedrock-agent-runtime RetrieveAndGenerate) call.
"""

import re
from typing import List, Dict

# ── Chunking ──────────────────────────────────────────────────────────────────

CHUNK_SIZE = 500       # characters per chunk
CHUNK_OVERLAP = 100    # overlap between consecutive chunks


def chunk_text(text: str) -> List[str]:
    """Split text into overlapping character windows."""
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end].strip())
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if len(c) > 30]   # drop tiny tail fragments


# ── Retrieval — keyword overlap (TF-IDF-lite) ─────────────────────────────────

def _tokenize(text: str) -> List[str]:
    """Lowercase words, strip punctuation, remove stopwords."""
    STOPWORDS = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'shall',
        'should', 'may', 'might', 'can', 'could', 'to', 'of', 'in', 'on',
        'at', 'by', 'for', 'with', 'about', 'as', 'into', 'through', 'during',
        'and', 'or', 'but', 'not', 'if', 'then', 'so', 'this', 'that', 'it',
        'its', 'i', 'we', 'you', 'he', 'she', 'they', 'them', 'their', 'our',
        'your', 'my', 'from', 'all', 'also', 'no', 'up', 'out', 'when', 'how',
    }
    tokens = re.findall(r'[a-z0-9]+', text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def _score(query_tokens: List[str], chunk: str) -> float:
    """Fraction of unique query tokens that appear in the chunk."""
    if not query_tokens:
        return 0.0
    chunk_lower = chunk.lower()
    hits = sum(1 for t in set(query_tokens) if t in chunk_lower)
    return hits / len(set(query_tokens))


def retrieve_relevant_chunks(
    query: str,
    docs: List[Dict],
    top_k: int = 5,
    min_score: float = 0.05,
) -> List[Dict]:
    """
    Return the top-K most relevant KB chunks for a query.

    `docs` is a list of KB document dicts (each shaped like
    dynamo_repository.kb_doc_to_dict() / the local SQLite equivalent), i.e.
    with "id"/"filename"/"description"/"chunks" keys — the caller is
    responsible for fetching them from whichever backend is active.

    Each result: {"doc_id", "filename", "description", "chunk_index", "text", "score"}
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scored = []
    for doc in docs:
        for chunk in (doc.get("chunks") or []):
            score = _score(query_tokens, chunk.get("text", ""))
            if score >= min_score:
                scored.append({
                    "doc_id":      doc.get("id"),
                    "filename":    doc.get("filename"),
                    "description": doc.get("description") or doc.get("filename"),
                    "chunk_index": chunk.get("chunk_id", 0),
                    "text":        chunk.get("text", ""),
                    "score":       score,
                })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def format_kb_context(chunks: List[Dict]) -> str:
    """
    Format retrieved chunks as a readable block for prompt injection.
    Returns empty string if no chunks.
    """
    if not chunks:
        return ""
    parts = ["=== Relevant Knowledge Base Context ==="]
    for i, c in enumerate(chunks, 1):
        source = c["description"]
        parts.append(f"\n[KB-{i}] Source: {source}\n{c['text']}")
    parts.append("=== End of KB Context ===")
    return "\n".join(parts)
