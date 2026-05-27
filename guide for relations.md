# General Concept: Resolving Relationships in Large GraphRAG Databases

In a large GraphRAG system, the LLM usually **does not know the whole graph**. It only sees one chunk, or a small group of chunks, at a time. Therefore, it should not be expected to directly create perfect links to existing database nodes.

The solution is to separate the work into two parts:

```text
LLM extraction → reference resolution → graph update
```

## 1. What the LLM does

The LLM reads a chunk and extracts what is mentioned in the text.

For example, if a legal text says:

```text
According to § 5 Abs. 2 EnWG, the operator must ...
```

The LLM should extract something like:

```json
{
  "source": "current chunk or provision",
  "relationship": "REFERS_TO",
  "target_reference": "§ 5 Abs. 2 EnWG",
  "evidence": "According to § 5 Abs. 2 EnWG..."
}
```

The important point is that the LLM extracts the **reference as written**, not necessarily the final database node ID.

It should answer:

```text
What does this text mention or refer to?
```

not:

```text
What exact graph node ID does this point to?
```

## 2. What the resolver does

A separate resolver then tries to match the extracted reference to an existing graph node.

For example:

```text
"§ 5 Abs. 2 EnWG"
```

might be resolved to a canonical node such as:

```text
DE/EnWG/§5/Abs2
```

This resolver can use several methods:

- exact matching
- aliases
- abbreviation tables
- regular expressions
- fuzzy search
- embeddings
- metadata filters
- candidate search plus LLM reranking

For legal texts, many references are structured enough that a parser or rule-based resolver may be more reliable than an LLM.

## 3. What the graph database stores

Once the target is resolved, the system creates the actual graph edge.

Example:

```text
(CurrentProvision)-[:REFERS_TO]->(DE/EnWG/§5/Abs2)
```

The edge should store provenance:

```text
source chunk
source document
evidence text
extraction method
confidence
```

This makes the graph auditable. The system can always trace a relationship back to the text that produced it.

## 4. What if the target node does not exist yet?

There are two common options.

### Option A: create a placeholder node

If the referenced node is not in the graph yet, the system can create a temporary placeholder:

```text
(Unresolved: DE/EnWG/§5/Abs2)
```

The edge can already be created:

```text
(CurrentProvision)-[:REFERS_TO]->(Unresolved: DE/EnWG/§5/Abs2)
```

Later, when the real target is ingested, the placeholder is filled in or merged.

### Option B: store a pending reference

Instead of creating a placeholder, the system can store the reference in a separate queue:

```text
source = current provision
target reference = § 5 Abs. 2 EnWG
status = unresolved
```

A later job tries to resolve it once more data is available.

## 5. Why canonical IDs matter

The system should avoid relying on internal database IDs such as:

```text
node_839201
```

Those IDs may change if the graph is rebuilt.

Instead, use stable canonical IDs when possible:

```text
DE/EnWG/§5/Abs2
EU/GDPR/Art6/Para1
DOI/10.xxxx/example
WIKIDATA/Q12345
```

The LLM can extract the human-readable reference. The resolver maps it to the stable canonical ID.

## 6. Candidate search for ambiguous references

Sometimes the reference is not exact.

Example:

```text
The method follows the Smith protocol.
```

The graph may contain several possible nodes:

```text
Smith DNA extraction protocol
Smith-Waterman algorithm
Smith et al. 2018 sequencing protocol
```

In that case, the system first retrieves a small candidate set. Then an LLM can help choose the most likely match.

So the LLM is not asked to search the whole graph. It is only asked to decide between a few plausible candidates.

## 7. The main design pattern

The robust pattern is:

```text
1. LLM extracts references and relationships from local text.
2. Resolver maps references to canonical entities.
3. Graph database creates or updates nodes and edges.
4. Unresolved references are stored as placeholders or pending tasks.
```

The LLM does local interpretation. The database and resolver handle global identity.

That is how large GraphRAG systems avoid needing the entire graph inside the LLM context window.

