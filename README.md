# IPFS Observer Club

Observers sniff Bitswap WANTs, classify in-scope CIDs with an
OpenAI-compatible model, and gossip the signed metadata. The catalog is
that gossip. There is no central index.

Academic is the shipped club. Copy `clubs/academic/` to start another topic.

A CID is classified about once. A second node casts an independent vote
when it can. Everyone else reuses the signed record unless a node
reports a wrong classification. Search is local FTS over metadata, not
file bytes.

## How work is shared

1. **Skip hook** — if the club already has two independent classifies or a
   live skip for a CID, do not fetch, unless a `wrong` report asks nodes
   that have not classified it yet to do so. One classify still asks a
   second node to vote. `unprocessable` stays local and expires.
   `out_of_scope` and `directory` are gossiped.
2. **Claim** — a short lease before an LLM call. At most one in-flight
   claim per node. Your own lease does not park the CID.
3. **First-block MIME** — stop assembling UnixFS if the bytes are not
   PDF/HTML/plain.
4. **Origin prior** — cheap markers (DOI, university, repository, ORCID)
   decide likely / uncertain / skip before the model. Academic origin
   counts; the page does not have to be a paper.
5. **Fingerprint reuse** — same `text_sha256`, new CID, copy labels.

System: [docs/OVERVIEW.md](docs/OVERVIEW.md). Wire format:
[docs/PROTOCOL.md](docs/PROTOCOL.md). Canonical JSON is a contract —
`observer/protocol.py` and `clubd/internal/canon` must stay in
agreement; timestamps are Unix seconds, integers only. Academic field
slugs live in `clubs/academic/fields.txt`. Set `club.id` to a folder
under `clubs/`. Gossip, mDNS, snapshots, and `data/<id>/club.sqlite`
are per-club.

## Requirements

- Go 1.25+
- Python 3.9+
- An OpenAI-compatible `/v1` chat API. [LM Studio](https://lmstudio.ai)
  on localhost is the default. Groq, Cerebras, Academic Cloud, or another
  host can be added under `/admin`.

## Setup

```bash
make setup     # venv, deps, clubd, sniffer
# edit config.toml — club.id picks the club; [llm] seeds LM Studio
make start     # clubd + observer (sniffer, indexer, web)
```

- Search: http://127.0.0.1:8002
- Admin (alias, classifier, password, peers, reports, club): http://127.0.0.1:8002/admin
- clubd identity: http://127.0.0.1:8003/id

`config.toml`, `data/`, and `.secret` stay on the machine. They are not
in git. Treat `data/identity.key`, `data/admin.json`, `data/session.key`,
and `data/llm.json` as secrets. If `admin.json` is lost or corrupt, delete
it and set the password again.

Optional: `[fetch] ipfs_api = "http://127.0.0.1:5001"` to try a local
Kubo node before public gateways.

Join a friend from `/admin` → Peers: copy this node’s multiaddr, paste
theirs, and Save. That writes `club.bootstrap_peers` and dials clubd
now. On the same LAN, mDNS finds other Observers. A late joiner pulls a
signed snapshot (classifies, aliases, reports, and persistent skips).

```bash
make test
make stop
```

## Ports

| process | port |
| --- | --- |
| Bitswap sniffer | 4712 |
| Observer web | 8002 |
| clubd API / gossip | 8003 / 4713 |

Search (8002) may be public. Set an admin password first. Guests can
propose abusive or wrong classification; the admin accepts or rejects
it. Snapshot catch-up (`GET /api/snapshot`) is localhost-only.

clubd’s HTTP API (8003) stays on localhost. `POST /v1/publish` signs as
this node. Gossip (4713) and the sniffer (4712) listen on all interfaces.

## Trust

Open swarm. Signed classifies are accepted as they arrive. The skip-hook
uses the first-seen record. Display labels are a vote across publishers
(`reuse` copies do not vote). A report can hide abusive CIDs or ask for
another classify. Guest reports are proposals until the admin accepts.
Fetched blocks are checked against the CID.

v1 gossips metadata only (CIDs, MIME types, fingerprints, labels), not
file bytes. It does not run a chain or gossip search queries.

## Layout

- `clubs/` — one folder per club (`club.toml`, `fields.txt`, optional `prior.py`)
- `clubd/` — libp2p gossip and signing
- `sniffer/` — Bitswap WANT listener; writes JSONL spool files
- `observer/` — ingest, work queue, classify, search, web UI
- `docs/` — system overview and wire format
- `tests/` — Python tests; Go tests live next to the code they cover
- `scripts/smoke_live.py` — optional live stack check, not part of `make test`

## License

MIT.
