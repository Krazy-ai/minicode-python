# Memory System

The memory subsystem lets the agent retain knowledge across sessions.

## Scopes

There are three memory scopes: user (cross-project), project (shared), and
local (project-only, not committed).

## Retrieval

Memory retrieval uses BM25 relevance scoring combined with usage frequency and
recency to rank entries. Chinese and English terms are expanded via a
bilingual dictionary.
