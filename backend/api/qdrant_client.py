"""Qdrant vector store client for memory layers L3, L4, L5."""

from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams


class QdrantClient:
    """Thin async wrapper around Qdrant vector DB."""

    def __init__(self, url: str):
        self.client = AsyncQdrantClient(url)

    async def ping(self) -> bool:
        await self.client.get_collections()
        return True

    async def ensure_collection(self, name: str, vector_size: int = 768):
        """Create collection if not exists. Default size for e5 / bge models."""
        collections = await self.client.get_collections()
        existing = [c.name for c in collections.collections]
        if name not in existing:
            await self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    async def embed(self, text: str) -> list[float]:
        """Simple embedding via Ollama endpoint (or fall back to random).
        
        Override this method with a proper embedding model for production.
        """
        import httpx
        from .config import settings

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{settings.ollama_base_url}/api/embeddings",
                    json={"model": settings.gen_model, "prompt": text},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("embedding", [0.0] * 768)
        except Exception:
            pass
        # Fallback: deterministic pseudo-embedding (not for production)
        import hashlib
        seed = hashlib.sha256(text.encode()).digest()
        vec = [b / 255.0 for b in seed[:768]]
        vec += [0.0] * max(0, 768 - len(vec))
        return vec[:768]

    async def upsert(
        self, collection: str, content: str, payload: dict[str, Any]
    ) -> str:
        vector = await self.embed(content)
        await self.ensure_collection(collection, len(vector))
        import hashlib
        point_id = int(hashlib.sha256(content.encode()).hexdigest()[:16], 16) % (10**12)
        point = PointStruct(
            id=point_id,
            vector=vector,
            payload=payload,
        )
        await self.client.upsert(collection_name=collection, points=[point])
        return str(point.id)

    async def search(
        self,
        collection: str,
        query: str,
        top_k: int = 5,
        query_filter: Any = None,
    ) -> list[dict[str, Any]]:
        vector = await self.embed(query)
        await self.ensure_collection(collection, len(vector))
        hits = await self.client.search(
            collection_name=collection,
            query_vector=vector,
            limit=top_k,
            query_filter=query_filter,
        )
        return [
            {
                "id": str(h.id),
                "score": h.score,
                "layer": (h.payload or {}).get("layer", collection),
                "content": (h.payload or {}).get("content", ""),
                "metadata": h.payload or {},
            }
            for h in hits
        ]

    async def close(self):
        await self.client.close()
