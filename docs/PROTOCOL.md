# Protocol v1

System overview (processes, skip-hook, labels, reports): [OVERVIEW.md](OVERVIEW.md).

This is the wire format for IPFS Observer Club. Python (`observer/protocol.py`)
and Go (`clubd/internal/canon`) **must** produce identical canonical JSON.
Change either only with a fixture test on both sides.

## Roles

Every Observer publishes **classify** / **skip** / **claim** / **report**.
There is no privileged catalog. Nodes keep every classify; the skip-hook
uses the first-seen record for a CID unless a `wrong` report asks nodes
that have not classified it yet to do so, or the CID still has only one
independent classify and this node is not that voter.

## Canonical JSON

Unsigned payload:

- UTF-8 JSON object
- keys sorted lexicographically
- separators `,` and `:` (no spaces)
- the `sig` field is omitted
- integers have no trailing `.0`
- `ensure_ascii=False` (Unicode is literal)

`sig` is hex-encoded Ed25519 over those bytes, using the Observer's libp2p
identity key. `publisher` is the libp2p peer ID (CIDv1 / base58btc).

`payload_hash` is SHA-256 hex of the canonical unsigned payload.

Timestamps (`until`, `indexed_at`) are **Unix seconds as integers**.

## Kinds

### claim

```json
{"v":1,"kind":"claim","club":"academic","cid":"bafy…","until":1780000900,"publisher":"12D3…","sig":"…"}
```

Every kind includes `club` (the joined club id). clubd stamps it on publish.
A message with a different `club` is dropped even if it arrives on the topic.

Lease: at most one in-flight claim per node. clubd **drops** a second live
claim from the same publisher (refresh of the same CID is allowed). The
catalog keys claims by publisher. Others skip LLM until `until` or until
a classify/skip lands. A node does not park a CID on its own lease.

### skip

```json
{"v":1,"kind":"skip","club":"academic","cid":"bafy…","mime_type":"image/png","reason":"out_of_scope","publisher":"12D3…","sig":"…"}
```

`reason` is `out_of_scope`, `directory`, or `unprocessable` (legacy
academic gossip used `not_academic`; readers accept it as out of scope).
Observers gossip `out_of_scope` and `directory`. Mime/binary
`unprocessable` stays in the local work queue and expires after
`fetch.skip_ttl_seconds` (default 6h). A skip never hides a live
classify. Nodes skip fetch and LLM for a CID when a live skip or
first-seen classify is already in the catalog.

### classify

```json
{
  "v": 1,
  "kind": "classify",
  "club": "academic",
  "cid": "bafy…",
  "mime_type": "application/pdf",
  "size": 184320,
  "filename": "paper.pdf",
  "field": "biology",
  "topic": "CRISPR off-target effects",
  "keywords": "crispr, genome editing",
  "license": "cc-by-4.0",
  "text_sha256": "…",
  "classifier": {"kind": "llm", "model": "gemma-4-12b-it-qat", "prompt_ver": "1"},
  "indexed_at": 1780000000,
  "publisher": "12D3…",
  "sig": "…"
}
```

`classifier.kind` is `llm` or `reuse` (same `text_sha256` as an existing classify).
`field` must be a slug from that club's `clubs/<id>/fields.txt`.
The LLM membership question is
`in_scope` (true = index this CID for the club).

`text_sha256` is SHA-256 of the whitespace-normalized UTF-8 text sample
(PDF/HTML/plain, `max_text_chars`). Each Observer sniffs and classifies locally.
Nodes keep every classify; they do not last-write-wins on CID. The skip-hook
uses the first-seen record once two independent classifies exist, or after
this node has published its own. A node that is not the sole voter fetches
and runs the model (no fingerprint reuse) so display labels can vote.
The web UI shows **voted labels**: one ballot per publisher (latest
independent classify; `reuse` does not vote). `field` and `topic` show the
unique winner; keywords with more agreement come first.

If the model sets `in_scope` false, the node does not publish a classify.
A likely academic-origin prior does not override that. The CID stays
eligible for other nodes (local `llm_disagreed`, no gossiped skip). An
uncertain or empty prior still gossips `out_of_scope`.

### alias

```json
{"v":1,"kind":"alias","club":"academic","alias":"berlin-lab","publisher":"12D3…","sig":"…"}
```

A node’s display name for Observer Ranking. Set from the local admin UI.
Latest alias per publisher wins. Empty `alias` clears the name. No `cid`.
32 characters or fewer after whitespace collapse.

### report

```json
{"v":1,"kind":"report","club":"academic","cid":"bafy…","reason":"wrong","publisher":"12D3…","sig":"…"}
```

`reason` is `wrong`, `abusive`, or `clear`. One report per
`(cid, publisher)`; the latest reason wins. `clear` retracts that
publisher's report.

`wrong` asks every node that has **not** already published a classify for
that CID to fetch and classify it. Those nodes ignore the skip-hook and
do not copy labels via fingerprint reuse (reuse would not vote). Nodes
that already classified, including the reporter, do not re-run the model.
After those classifies land, display labels are a vote.

`abusive` hides the CID from search and browse. One abusive report is
enough (same open-swarm trust as first-seen classify). Classifies stay
in the catalog. Admin can review hidden CIDs and `clear` this node's
report if the hide was a mistake.

Reports are filed from the search UI (`POST /api/report`). A logged-in
admin publishes immediately. A guest may propose `abusive` or `wrong`; that
row stays local until the admin accepts (then this node publishes) or
rejects (the proposal is dropped).

## Gossip

Topics (prefix `ipfs-observer-club/v1/{club}/`):

| kind | topic |
| --- | --- |
| claim | claim |
| skip | skip |
| classify | classify |
| alias | alias |
| report | report |

One observer process joins exactly one club (`club.id` in config.toml, a
folder under `clubs/`). Academic is the default. Different clubs do not
share gossip, snapshots, or `data/<id>/club.sqlite`.

clubd appends **verified** messages to `data/<club>/inbox/YYYY-MM-DD.jsonl`.
The Python observer is the only SQLite writer. Inbox files older than
`inbox_keep_days` (default 7) are deleted; total size is capped at
`inbox_max_bytes`.

Live gossip is rate-limited to `max_msgs_per_peer_per_min` (default 60) **new**
verified payloads per `publisher`. Duplicates are dropped.

Catch-up: libp2p protocol `/ipfs-observer-club/v1/{club}/snapshot`. A peer
serves signed JSONL from `GET /api/snapshot` (classify, current `alias`,
`report`, and persistent skips: `out_of_scope`, `directory`, legacy
`not_academic`). Unprocessable skips are omitted.
The requester verifies each line the same way as gossip. Snapshot transfer
is not rate-limited per message (it is cooldown-limited per peer).

Optional LAN join: clubd mDNS service name `ipfs-observer-club-{club}`.
Invited join is `club.bootstrap_peers` (those peers must be in the same
club). Admin → Peers writes that list and `POST`s clubd `/v1/bootstrap`
so the running daemon dials without restart.

Query/result gossip is **not** in v1.
