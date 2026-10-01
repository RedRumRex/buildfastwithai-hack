"""
Notes retrieval: sentence-transformers embeddings + Chroma (in-memory).
Falls back to TF-IDF automatically if the packages/model are missing, so the demo never breaks.

.env (optional)
    EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
    RAG_BACKEND=embeddings        # or tfidf
    RAG_MIN_RELEVANCE=0.25        # cosine similarity cut-off for embeddings
"""
import os
import uuid

import pandas as pd

_model = None

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"), device="cpu")
    return _model


class NotesIndex:
    def __init__(self, notes: pd.DataFrame):
        self.notes = notes.reset_index(drop=True)
        self.docs = (self.notes["company"].fillna("").astype(str) + ": " + self.notes["text"].fillna("").astype(str)).tolist()
        self.engine, self.error = "none", None
        if not self.docs:
            return
        if os.getenv("RAG_BACKEND", "embeddings").lower() != "tfidf":
            try:
                self._build_chroma(); self.engine = "embeddings"; return
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"[:200]
        self._build_tfidf(); self.engine = "tfidf"

    def _build_chroma(self):
        import chromadb
        from chromadb.config import Settings
        emb = _get_model().encode(self.docs, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
        client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
        self.col = client.create_collection(f"notes_{uuid.uuid4().hex[:10]}", metadata={"hnsw:space": "cosine"})
        ids = [str(i) for i in range(len(self.docs))]
        for s in range(0, len(ids), 4000):
            self.col.add(ids=ids[s:s + 4000], embeddings=emb[s:s + 4000].tolist(), documents=self.docs[s:s + 4000],
                         metadatas=[{"note_id": str(n)} for n in self.notes["note_id"].iloc[s:s + 4000]])

    def _build_tfidf(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vec = TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
        self.mat = self.vec.fit_transform(self.docs)

    def search(self, query: str, k: int = 8) -> pd.DataFrame:
        if self.engine == "none":
            return self.notes.head(0).assign(relevance=[])
        if self.engine == "embeddings":
            q = _get_model().encode([query], normalize_embeddings=True).tolist()
            r = self.col.query(query_embeddings=q, n_results=min(k * 3, len(self.docs)))
            idx = [int(i) for i in r["ids"][0]]
            res = self.notes.iloc[idx].assign(relevance=[round(1 - d, 4) for d in r["distances"][0]])
            res = res[res["relevance"] >= float(os.getenv("RAG_MIN_RELEVANCE", "0.25"))]
        else:
            from sklearn.metrics.pairwise import linear_kernel
            res = self.notes.assign(relevance=linear_kernel(self.vec.transform([query]), self.mat).ravel())
            res = res[res["relevance"] > 0]
        return res.sort_values(["relevance", "note_date"], ascending=[False, False]).head(k)
