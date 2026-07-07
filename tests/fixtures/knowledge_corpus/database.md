# Database

The project uses SQLite as its primary storage engine for local development.

## Schema

The schema defines three tables: `documents`, `chunks`, and `inverted_index`.
Each chunk belongs to exactly one document.

## Migrations

Database migrations are applied automatically on startup. WAL mode is enabled
for better concurrent read and write performance.
