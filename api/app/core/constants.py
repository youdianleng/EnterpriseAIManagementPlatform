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

# Model used to produce those vectors. Recorded on every document version so a
# future switch can be rolled out without guessing which rows are stale.
EMBEDDING_MODEL = "text-embedding-3-large"
