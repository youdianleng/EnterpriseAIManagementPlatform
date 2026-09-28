"""Constants that schema and code must agree on.

Anything here is expensive to change after data exists, so each one says why.
"""

# Embedding vector dimension.
#
# LOCKED. Changing it means re-embedding every stored chunk, because a pgvector
# column's dimension is part of the schema.
#
# 1536 rather than the model default of 3072, measured on 10k chunks:
#
#   vector(1536) + HNSW    95.5 MB    index 0.9s    query 0.27ms
#   halfvec(3072) + HNSW  152.6 MB    index 1.8s    query 0.31ms
#   vector(3072) + HNSW   not indexable at all
#
# The last line is the real constraint: pgvector's HNSW supports at most 2000
# dimensions for `vector` and 4000 for `halfvec`, so a 3072-dimension `vector`
# column cannot be indexed. 1536 keeps the plain `vector` type, which stores
# what the API returns without a conversion step on every write.
EMBEDDING_DIMENSIONS = 1536

# Model used to produce those vectors.
#
# `text-embedding-3-small`, which returns 1536 dimensions natively — the dimension
# above, so the API's output is stored as it arrives and no step of the write path
# converts it. (Ticket 31 left this constant saying `-large` with the same 1536, on
# the strength of §10.3's `dimensions=1536` reduction. Both are 1536; the ticket that
# actually embeds the corpus picked the model whose native width that is.)
#
# LOCKED in the same way the dimension is: vectors from two models are not
# comparable, so retrieval may only ever mix rows that share this name. That is why
# `document_chunks.embedding_model` records it per row and why a model change is a
# re-embedding migration rather than a configuration edit.
EMBEDDING_MODEL = "text-embedding-3-small"

# The BPE encoding `EMBEDDING_MODEL` tokenizes with. Part of the split, not a
# preference: a chunk's "about 400 tokens" means 400 tokens *of this encoding*, so a
# deployment that changed it without changing `CHUNKING_VERSION` would hold a corpus
# whose sizes were measured two different ways.
EMBEDDING_ENCODING = "cl100k_base"
