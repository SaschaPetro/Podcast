-- Umstellung von OpenAI-Embeddings (1536 Dim.) auf Gemini text-embedding-004 (768 Dim.).
-- Spalte ist aktuell komplett leer (Backfill war nie erfolgreich), daher unkritisch.

drop index if exists themen_embedding_idx;

alter table themen
  alter column embedding type vector(768);

create index themen_embedding_idx
  on themen
  using hnsw (embedding vector_cosine_ops);

-- Funktion auf 768 Dimensionen umstellen, damit sie zur Spalte passt.
create or replace function finde_aehnliche_themen(
  such_embedding vector(768),
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
