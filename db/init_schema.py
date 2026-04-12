"""Database initialisation — creates tables from schema.sql."""

import asyncio
from pathlib import Path

import asyncpg
import yaml


async def init_database():
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    db = config["database"]

    print(f"Connecting to PostgreSQL: {db['host']}:{db['port']}/{db['name']}")

    conn = await asyncpg.connect(
        host=db["host"],
        port=db["port"],
        database=db["name"],
        user=db["user"],
        password=db["password"],
    )

    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    try:
        await conn.execute(schema_sql)
        print("✓ Tables created successfully")

        # Verify
        tables = await conn.fetch(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename
            """
        )
        print(f"✓ Tables in DB: {', '.join(t['tablename'] for t in tables)}")

    except Exception as e:
        print(f"✗ Error: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(init_database())
