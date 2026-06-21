from __future__ import annotations

import asyncpg
import logging
from typing import Optional

log = logging.getLogger(__name__)

class EngineeringDBService:
    def __init__(self, dsn: str):
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self.pool: Optional[asyncpg.Pool] = None

    async def setup(self) -> None:
        """Create pool and initialize table."""
        try:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS engineering_tickets (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        course_do_id VARCHAR(255),
                        content_do_id VARCHAR(255),
                        status VARCHAR(50) DEFAULT 'OPEN',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
            log.info("✅ EngineeringDBService: Table 'engineering_tickets' initialized.")
        except Exception as e:
            log.error(f"⚠️ EngineeringDBService setup failed: {e}")
            self.pool = None

    async def insert_ticket(self, user_id: str, course_do_id: str, content_do_id: str) -> None:
        if not self.pool:
            log.warning("EngineeringDBService pool not initialized, skipping insert.")
            return
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO engineering_tickets (user_id, course_do_id, content_do_id)
                    VALUES ($1, $2, $3)
                ''', user_id, course_do_id, content_do_id)
                log.info(f"Inserted engineering ticket for user {user_id}, course {course_do_id}")
        except Exception as e:
            log.error(f"Failed to insert engineering ticket: {e}")

    async def aclose(self) -> None:
        if self.pool:
            await self.pool.close()
