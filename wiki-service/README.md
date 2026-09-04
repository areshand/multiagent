# Multiagent Wiki Service

This directory contains the minimal organization-knowledge service. Its durable
state is ordinary UTF-8 Markdown under `WIKI_ROOT`; the service has no database,
embedding model, GitHub credential, or repository synchronization process.

## Corpus contract

`index.md` is the catalog entry point. It links to reviewed repository and topic
pages. A small corpus can use this layout:

```text
index.md
repos/
  internal-services.md
topics/
```

Each reviewed page should state the repository and resolved source commit and
cite source paths plus SHA-256 digests. The catalog and pages remain directly
auditable in S3, Git, or any filesystem snapshot. The in-memory lexical index is
derived and disposable.

The deployed MVP is read-only. Query activity belongs in the surrounding
multiagent session trace; the service does not expose feedback or source-writing
HTTP APIs.

## Run and query

Node.js 20 or newer is required.

```sh
WIKI_ROOT=/var/lib/wiki PORT=8080 npm start
MULTIAGENT_WIKI_URL=http://wiki-service:8080 wiki-query \
  --query "InternalServices architecture" --max-results 3
```

The stable agent-facing command is `wiki-query`. To install it into an image,
run `npm install --global /path/to/wiki-service`; because the package has no
dependencies, an image build may instead create an executable symlink from
`/usr/local/bin/wiki-query` to this directory's `bin/wiki-query.mjs`. The latter
requires this whole directory to remain present at the same path so its module
imports resolve. The CLI is independent of any model provider or agent runtime.

## One-shot catalog bootstrap

`wiki-seed` is an explicit administrative bootstrap command. It is not a
repository sync process, is not scheduled, and is never invoked by the query
service. It only reads a prepared local JSON manifest; it makes no GitHub or
other network calls and has no credential integration.

```sh
WIKI_STAGING_ROOT=/tmp/wiki-seed
WIKI_ROOT="${WIKI_STAGING_ROOT}" wiki-seed --manifest /work/repository-catalog.json
```

Seed only a local staging directory. The deployed query mount is read-only, and
S3-style object mounts do not provide the rename semantics used for atomic local
writes. Upload `repos/*.md` with the deployment owner's object-storage tooling,
then upload `index.md` last as the catalog commit marker. The concrete AWS
commands and bucket are owned by the InternalServices deployment runbook.

The manifest is bounded to 2 MiB and 1,000 repositories:

```json
{
  "schema": "wiki-catalog-seed/v1",
  "repositories": [
    {
      "catalogId": "internal-services",
      "repository": "MoveIndustries/InternalServices",
      "url": "https://github.com/MoveIndustries/InternalServices.git",
      "visibility": "private",
      "defaultBranch": "main",
      "sourceStatus": "verified",
      "resolvedCommitSha": "0123456789012345678901234567890123456789",
      "summary": "Owns shared infrastructure and deployment configuration.",
      "description": "A detailed, source-grounded description.",
      "sources": [
        {
          "path": "docs/architecture.md",
          "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        }
      ]
    }
  ]
}
```

Records and citations are sorted deterministically. The command atomically
replaces each `repos/<catalogId>.md` page and publishes `index.md` last as the
catalog commit marker. Re-running the same normalized manifest produces
byte-identical Markdown. It does not run as part of `serve` or any steward job.

An authenticated but empty GitHub repository is represented without fabricated
evidence by setting `sourceStatus` to `empty`, `resolvedCommitSha` to `null`, and
`sources` to `[]`. Generated Markdown explicitly records `source_status: empty`,
the null commit, and that no repository source exists; validation does not
depend on English-language keywords in the human-authored text. A `verified` record
requires an exact 40-character commit SHA and at least one path/SHA-256 citation.

## HTTP API

- `POST /v1/query`: `{ "query": string, "limit"?: 1..10, "maxExcerptChars"?: 100..4000 }`
- `POST /v1/refresh`: atomically rebuild the in-memory index from Markdown
- `GET /healthz`
- `GET /readyz`

Retrieval searches pages explicitly linked by `index.md` first. If there are not
enough matching results, it scans a deterministic, configured bound of remaining
Markdown pages. Results include path, content SHA-256, bounded excerpt, score,
and whether the page came through the catalog or fallback.

Important bounds can be configured with `WIKI_MAX_REQUEST_BYTES`,
`WIKI_MAX_CORPUS_FILES`, `WIKI_MAX_CORPUS_BYTES`,
`WIKI_MAX_FALLBACK_FILES`, and `WIKI_MAX_FALLBACK_BYTES`. Agent requests
have a five-second deadline by default; set `WIKI_QUERY_TIMEOUT_MS` to an integer
from 100 through 60000 to tune it.

## Test

```sh
npm test
```
