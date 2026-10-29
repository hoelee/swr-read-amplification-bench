# Read amplification, timeout-budget inversion, and a stale-while-revalidate status engine

Benchmark harness and full report for a status endpoint that answered in **433 ms** on a
development machine and returned **HTTP 504** in production — same code, byte for byte.

The interesting part is not that it broke. It is that the difference between "fast enough"
and "504" turned out to be a property of the storage, not of the algorithm.

| | Naive (4 reads/file) | Single pass (1 read/file) | SWR cache hit |
|---|---|---|---|
| Request path | 433.5 ms (measured, local NVMe) | 230.0 ms | **0.3 µs** |
| Reads per page load | 960 | 360 | 0 |
| Projected at 20 ms/read | 18.5 s | 7.9 s | off the request path |

---

## What the endpoint did

A knowledge-base admin view summarises the translation state of every note in the library.
For each source file it asked four questions — which locale, what is the hash, what is the
title, is the translation current — and answered each one by opening the file again. At 240
notes that is **960 whole-file reads** and roughly three YAML front-matter deserialisations
per note, for a page that renders titles and three status words.

On local NVMe, removing three quarters of those reads bought **1.5–1.9×**. That is the trap:
a four-fold read multiplier that costs 433 ms passes review, passes tests, and ships. The
same code is only wrong on storage where each read carries a round trip.

## Why the fix is not a faster loop

The request also failed for a second reason: the timeout budget ran backwards.
**30 s** at the edge gateway, **60 s** at the reverse proxy, **120 s** at the application. Each
layer waits longer than the layer in front of it, so when the gateway abandons the connection
at 30 s the inner layers keep working and write a `200` into a socket nobody is reading. The
same `X-Request-ID` appears as *abandoned* in the gateway log and *completed 200* in the
application log, seconds apart — which means the request's true duration is observable from
neither log alone, and should not be quoted.

The fix is architectural: **requests always read memory, scans always happen in the
background.**

```text
GET /api/translations/status
      │
      ▼
  memory cache fresh? ──yes──▶ return JSON (0.3 µs)
      │ no
      ▼
  a refresh already running? ──yes──▶ return the previous snapshot
      │ no
      ▼
  claim the refresh, start a background thread, return the previous snapshot
      │
      └─ background: single pass + mtime/size signature diff
```

Three properties make it safe rather than merely fast:

- **Stale-while-revalidate** ([RFC 5861](https://www.rfc-editor.org/rfc/rfc5861.html)) — a hit never blocks; stale data is served while a refresh runs.
- **Single flight** ([groupcache](https://github.com/golang/groupcache), [`x/sync/singleflight`](https://pkg.go.dev/golang.org/x/sync/singleflight)) — a burst of five requests triggers exactly one scan; a separate cold-start lock prevents the stampede that "async refresh" would otherwise create.
- **Signature-based incrementality** — `(st_mtime_ns, st_size)` per file; an unchanged signature means **zero `read_text()` calls**. One edited note costs one file read and reuses 239 cached rows.

## Reproduce it

```bash
pip install pyyaml
python swr_bench.py        # ~100 s: generates a 360-file corpus, runs three implementations,
                           # emits the latency projection, prints strictly valid JSON
```

The harness generates its own corpus, so it runs anywhere. Expect, within roughly ±30% on
comparable hardware: cold paths in the hundreds of milliseconds, cache hit sub-microsecond,
incremental refresh around 25 ms, and the classification split **ready 60 / outdated 60 /
pending 120** exactly — that last number is a correctness assertion, not a timing.

## The data in `data/`

| File | What it is |
|---|---|
| `bench-2026-10-30-run{A,B,C}.json` | Three independent full runs (replication session B), verbatim harness stdout |
| `bench-v1.1-prefix-289.6ms.json` | **Pre-correction v1.1 output** — its incremental column is physically impossible (faster as reads get slower). Kept because it is the defect the report corrects in §5.4 |
| `bench-v1.1-prefix-401.5ms.json` | Second pre-correction v1.1 output, same defect |

Two sessions are reported and neither is hidden:

- **Session A (2026-10-29)** — medians `433.5 / 230.0 / 263.1 ms`, cache hit `0.3 µs`, incremental `14.7 ms`. Transcribed from console output; not stored as files.
- **Session B (2026-10-30)** — medians `517.1 / 292.2 / 333.1 ms`, cache hit `0.5 µs`, incremental `25.50 ms`. Archived here.

Session B ran **19–27 % slower** than session A on the cold paths, so cross-session drift is
larger than the ±20 % within-session band. What replicates is the **structure**: zero
filesystem reads on the request path, one file read per incremental refresh, and a cold-path
ratio of 1.77–1.90× (A: 1.50–1.89×). What does not replicate is any absolute figure — which
is why the report quotes medians, ranges and a per-session label, never a bare number.

## Limits, stated plainly

- One machine: Windows 11, local NVMe, single process, Python 3.14.9. The experiment quantifies the *relative* cost of read amplification; it does not reproduce network-volume latency.
- The latency projection in the report **is a model output**. It bills `read_text()` at a fixed cost `L` and does not bill `stat()`, which on SMB/NFS is also a round trip — the incremental path's dominant cost is roughly 480 of them.
- The corpus is synthetic. Real notes have longer bodies and messier front matter, which moves the absolute numbers and not the shape.
- Write-through invalidation assumes the save path calls `invalidate(slug)`; a file dropped in by rsync can be served stale for up to the 60-second TTL. That window is accepted, because adding a `stat` probe to `GET` would invite back the I/O the refactor removed.

## The report

- **English, 14 pages** — [`paper/whitepaper-504-en-v1.3.pdf`](paper/whitepaper-504-en-v1.3.pdf)
- **Chinese, 13 pages** — [`paper/whitepaper-504-zh-v1.3.pdf`](paper/whitepaper-504-zh-v1.3.pdf)
- HTML edition — <https://doc.hoelee.com/whitepaper-504-read-amplification-swr-en.html>

The report carries eight references, each cited for a specific mechanism used (the HTTP caching
specs, the Go single-flight pattern, NFS/SMB round-trip structure, `pathlib.Path.stat`), not as a
literature survey.

## Citing

```bibtex
@techreport{lee2026swr504,
  author      = {Lee, Teong Hoe},
  title       = {Moving a 504 Out of the System: Read Amplification, Timeout Budget
                 Inversion, and a Stale-While-Revalidate Status Engine},
  institution = {Hoelee Enterprise},
  year        = {2026},
  month       = {oct},
  version     = {1.3},
  url         = {https://doc.hoelee.com/whitepaper-504-read-amplification-swr-en.html}
}
```

A DOI will be minted on Zenodo; this README and `CITATION.cff` will be updated with it.

## Licence

- Text and figures: **CC BY 4.0**
- `swr_bench.py`: **MIT**

Architectural patterns described here were engineered by the author. Client identifiers and
production thresholds are sanitised; there is no client data in this repository.
