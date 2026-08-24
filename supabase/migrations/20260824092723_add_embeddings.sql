-- Vector-Erweiterung aktivieren (für Embeddings + Ähnlichkeitssuche)
create extension if not exists vector;

-- Embedding-Spalte für Themen (1536 Dimensionen = OpenAI text-embedding-3-small)
alter table themen
  add column embedding vector(1536);

-- Index für schnelle Cosine-Similarity-Suche.
-- HNSW statt IVFFlat: braucht keine Trainingsdaten und liefert auch bei
-- wenigen/wachsenden Zeilen gute Recall-Werte (bei uns kein riesiger,
-- fester Datenbestand, sondern laufend neue Themen).
create index themen_embedding_idx
  on themen
  using hnsw (embedding vector_cosine_ops);

-- Findet Themen, deren Embedding-Ähnlichkeit zu such_embedding über dem
-- Schwellenwert liegt (Cosine Similarity, 1 = identisch, 0 = orthogonal).
create or replace function finde_aehnliche_themen(
  such_embedding vector(1536),
  schwellenwert float
)
returns table (
  id uuid,
  titel text,
  status text,
  aehnlichkeit float
)
language sql
stable
as $$
  select
    themen.id,
    themen.titel,
    themen.status,
    1 - (themen.embedding <=> such_embedding) as aehnlichkeit
  from themen
  where themen.embedding is not null
    and 1 - (themen.embedding <=> such_embedding) > schwellenwert
  order by aehnlichkeit desc;
$$;
