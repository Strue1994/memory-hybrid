"""Neo4j graph client for L2 persona/relationship memory."""

import os
from typing import Any
from uuid import uuid4

from neo4j import AsyncGraphDatabase

from .config import settings


class Neo4jClient:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def ping(self) -> bool:
        async with self.driver.session(database="neo4j") as session:
            await session.run("RETURN 1")
        return True

    async def create_constraints(self):
        """Ensure indexes for efficient graph queries."""
        async with self.driver.session(database="neo4j") as session:
            try:
                await session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE")
            except Exception:
                pass
            try:
                await session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Skill) REQUIRE s.name IS UNIQUE")
            except Exception:
                pass
            try:
                await session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE")
            except Exception:
                pass

    async def save_memory(
        self,
        content: str,
        people: list[str] | None = None,
        skills: list[str] | None = None,
        source: str = "manual",
    ) -> str:
        me_name = settings.me_name
        memory_id = str(uuid4())

        base_query = """
        MERGE (me:Person {name: $me_name})
        MERGE (m:Memory {id: $memory_id})
        SET m.content = $content, m.source = $source, m.created_at = datetime()
        MERGE (me)-[:RECORDED]->(m)
        RETURN m.id AS id
        """
        people_query = """
        MATCH (me:Person {name: $me_name}), (m:Memory {id: $memory_id})
        UNWIND $people AS person_name
          MERGE (p:Person {name: person_name})
          MERGE (me)-[:KNOWS]->(p)
          MERGE (m)-[:MENTIONS]->(p)
        """
        skills_query = """
        MATCH (me:Person {name: $me_name}), (m:Memory {id: $memory_id})
        UNWIND $skills AS skill_name
          MERGE (s:Skill {name: skill_name})
          MERGE (me)-[:HAS_SKILL]->(s)
          MERGE (m)-[:RELATES_TO_SKILL]->(s)
        """

        async with self.driver.session(database="neo4j") as session:
            records, _, _ = await session.execute_query(
                base_query,
                me_name=me_name,
                memory_id=memory_id,
                content=content,
                source=source,
            )
            if people:
                await session.execute_query(
                    people_query,
                    me_name=me_name,
                    memory_id=memory_id,
                    people=[p for p in (people or []) if p],
                )
            if skills:
                await session.execute_query(
                    skills_query,
                    me_name=me_name,
                    memory_id=memory_id,
                    skills=[s for s in (skills or []) if s],
                )
        return memory_id

    async def search_memory(
        self, query_text: str, top_k: int = 5, sources: list[str] | None = None
    ) -> list[dict[str, Any]]:
        cypher = """
        MATCH (m:Memory)
        OPTIONAL MATCH (m)-[:MENTIONS]->(p:Person)
        OPTIONAL MATCH (m)-[:RELATES_TO_SKILL]->(s:Skill)
        WHERE toLower(m.content) CONTAINS toLower($q)
           OR toLower(coalesce(p.name, "")) CONTAINS toLower($q)
           OR toLower(coalesce(s.name, "")) CONTAINS toLower($q)
        WITH m, p, s
        WHERE $sources_count = 0 OR m.source IN $sources
        RETURN m.id AS id,
               m.content AS content,
               m.source AS source,
               m.created_at AS created_at,
               collect(DISTINCT p.name) AS people,
               collect(DISTINCT s.name) AS skills
        ORDER BY m.created_at DESC
        LIMIT $limit
        """

        src = [s for s in (sources or []) if s]
        async with self.driver.session(database="neo4j") as session:
            records, _, _ = await session.execute_query(
                cypher,
                q=query_text,
                limit=top_k,
                sources=src,
                sources_count=len(src),
            )

        return [
            {
                "id": r.get("id") or "",
                "score": 0.75,
                "layer": "L2",
                "content": r.get("content") or "",
                "metadata": {
                    "created_at": str(r.get("created_at") or ""),
                    "source": r.get("source") or "",
                    "people": [p for p in (r.get("people") or []) if p],
                    "skills": [s for s in (r.get("skills") or []) if s],
                },
            }
            for r in records
        ]

    async def get_stats(self) -> dict[str, int]:
        cypher = """
        MATCH (n) WITH count(n) AS nodes
        MATCH ()-[r]->() RETURN nodes, count(r) AS relationships
        """
        async with self.driver.session(database="neo4j") as session:
            records, _, _ = await session.execute_query(cypher)
            if not records:
                return {"nodes": 0, "relationships": 0}
            return {
                "nodes": int(records[0].get("nodes", 0)),
                "relationships": int(records[0].get("relationships", 0)),
            }

    async def close(self):
        await self.driver.close()
