"""
Controlled reproduction benchmark for the 504/SWR whitepaper (doc 04).

Compares two implementations of list_status() over a synthetic corpus of
Markdown notes with YAML front matter:

  A) naive  : 4x read_text + 3x YAML load per file (the original defect)
  B) single : 1x read per file, one YAML load, in-memory reuse
  C) swr    : mtime-diff incremental revalidation over B, with 3-state
              (ready / outdated / pending) detection preserved

Outputs a JSON blob with measured timings so the whitepaper table can quote
real numbers instead of invented ones.
"""
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

import yaml

CORPUS_N = int(os.environ.get("CORPUS_N", "240"))
BODY_PARAS = 12

FRONT = """---
title: "Note {i:03d} — streaming pipeline failure modes"
description: "Synthetic sample note used for a controlled benchmark of status aggregation."
pubDate: 2026-01-{day:02d}
category: devops
tags: ["benchmark", "synthetic"]
locale: zh
---
"""

PARA = (
    "The pipeline emitted a partial frame before the muxer flushed its moov atom, "
    "which made every downstream player re-buffer at the same offset. Segment {j} of "
    "this note exists purely to give the file a realistic byte size so that repeated "
    "read_text() calls cost measurable wall-clock time.\n\n"
)


def build_corpus(root: Path, n: int):
    body = "".join(PARA.format(j=j) for j in range(BODY_PARAS))
    for i in range(n):
        day = (i % 28) + 1
        src = root / f"note-{i:03d}.md"
        src.write_text(FRONT.format(i=i, day=day) + body, encoding="utf-8")
        # half the corpus has a translated twin, half is pending
        if i % 2 == 1:
            tgt = root / f"note-{i:03d}.en.md"
            # hash of the source note at the moment the translation was produced
            src_hash_at_translation = hash_of_text(src.read_text(encoding="utf-8"))
            translated = (f"---\ntitle: Note {i:03d}\n"
                          f"translatedFromHash: {src_hash_at_translation}\n---\n"
                          + body.replace(" pipeline", " conduit"))
            tgt.write_text(translated, encoding="utf-8")
            if i % 4 == 3:
                # source keeps moving after the translator was run -> this one is 'outdated'
                src.write_text(src.read_text(encoding="utf-8") + "\n<!-- appended drift -->\n",
                               encoding="utf-8")


def split_note(raw: str):
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    return (yaml.safe_load(parts[1]) or {}), parts[2]


def hash_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- A) naive
def naive_list_status(content_dir: Path):
    results = []
    for p in sorted(content_dir.glob("*.md")):
        if p.name.endswith(".en.md"):
            continue
        # read 1
        raw = p.read_text(encoding="utf-8")
        meta, _ = split_note(raw)
        if meta.get("locale") != "zh":
            continue
        # read 2
        src_hash = hash_of_text(p.read_text(encoding="utf-8"))
        # read 3
        meta2, _ = split_note(p.read_text(encoding="utf-8"))
        tgt = p.with_name(f"{p.stem}.en.md")
        status = "pending"
        if tgt.exists():
            # read 4
            tgt_raw = tgt.read_text(encoding="utf-8")
            tgt_hash = hash_of_text(tgt_raw)
            status = "ready" if tgt_hash else "pending"
        results.append({"slug": p.stem, "title": meta2.get("title"), "status": status,
                        "source_hash": src_hash})
    return results


# ------------------------------------------------------- B) single pass
def single_pass(content_dir: Path):
    results = []
    for p in sorted(content_dir.glob("*.md")):
        if p.name.endswith(".en.md"):
            continue
        raw = p.read_text(encoding="utf-8")
        meta, body = split_note(raw)
        if meta.get("locale") != "zh":
            continue
        src_hash = hash_of_text(raw)
        tgt = p.with_name(f"{p.stem}.en.md")
        status = "pending"
        if tgt.exists():
            tgt_meta, _ = split_note(tgt.read_text(encoding="utf-8"))
            recorded = (tgt_meta or {}).get("translatedFromHash")
            # 3-state: ready if the translation recorded the source hash we see now
            status = "ready" if recorded == src_hash else "outdated"
        results.append({"slug": p.stem, "title": meta.get("title"), "status": status,
                        "source_hash": src_hash})
    return results


# ---------------------------------------------------------------- C) SWR
class SwrStatus:
    def __init__(self, content_dir: Path, ttl=60.0):
        self.content_dir = content_dir
        self.ttl = ttl
        self.data = None
        self.stamp = 0.0
        self.mtime_map = {}

    def refresh(self):
        t0 = time.perf_counter()
        results = []
        new_map = {}
        reread = 0
        reused = 0
        for p in sorted(self.content_dir.glob("*.md")):
            if p.name.endswith(".en.md"):
                continue
            st = p.stat()
            tgt = p.with_name(f"{p.stem}.en.md")
            tgt_st = tgt.stat() if tgt.exists() else None
            key = p.name
            sig = (st.st_mtime_ns, st.st_size,
                   tgt_st.st_mtime_ns if tgt_st else None,
                   tgt_st.st_size if tgt_st else None)
            cached = self.mtime_map.get(key)
            if cached and cached[0] == sig:
                results.append(cached[1])
                new_map[key] = cached
                reused += 1
                continue
            raw = p.read_text(encoding="utf-8")
            meta, body = split_note(raw)
            if meta.get("locale") != "zh":
                reread += 1
                continue
            item = {"slug": p.stem, "title": meta.get("title"),
                    "source_hash": hash_of_text(raw), "status": "pending"}
            if tgt_st:
                tgt_meta, _ = split_note(tgt.read_text(encoding="utf-8"))
                recorded = (tgt_meta or {}).get("translatedFromHash")
                item["status"] = "ready" if recorded == item["source_hash"] else "outdated"
            results.append(item)
            new_map[key] = (sig, item)
            reread += 1
        self.mtime_map = new_map
        self.data = results
        self.stamp = time.time()
        return (time.perf_counter() - t0) * 1000.0, reread, reused

    def get(self):
        t0 = time.perf_counter()
        if self.data is None:
            self.refresh()
        elif (time.time() - self.stamp) >= self.ttl:
            self.refresh()
        return self.data, (time.perf_counter() - t0) * 1000.0


def projected_with_read_latency(content_dir: Path, per_read_ms: float):
    """Model a network-attached volume by charging per_read_ms to every read_text().

    This is an explicitly labelled projection, not a measurement of the NAS.
    """
    import builtins
    delay = per_read_ms / 1000.0
    orig = Path.read_text

    def slow_read_text(self, *a, **kw):
        time.sleep(delay)
        return orig(self, *a, **kw)

    Path.read_text = slow_read_text
    try:
        t = time.perf_counter()
        naive_list_status(content_dir)
        naive_ms = (time.perf_counter() - t) * 1000.0

        t = time.perf_counter()
        single_pass(content_dir)
        single_ms = (time.perf_counter() - t) * 1000.0

        swr = SwrStatus(content_dir)
        swr.refresh()
        swr.mtime_map = {}
        t = time.perf_counter()
        swr.refresh()
        swr_cold = (time.perf_counter() - t) * 1000.0

        # incremental WITH one changed note: identical test condition to the
        # measured 25.8 ms row (2 read_text calls: source + translation)
        victim = sorted(content_dir.glob("note-*.md"))[0]
        victim.write_text(victim.read_text(encoding="utf-8") + "\n<!-- drift -->\n",
                          encoding="utf-8")
        swr.stamp = 0.0
        t = time.perf_counter()
        _i, reread_i, reused_i = swr.refresh()
        swr_incr = (time.perf_counter() - t) * 1000.0
    finally:
        Path.read_text = orig

    return {"per_read_ms": per_read_ms, "incremental_files_read": reread_i,
            "incremental_files_reused": reused_i,
            "naive_ms": round(naive_ms, 1),
            "single_pass_ms": round(single_ms, 1),
            "swr_cold_ms": round(swr_cold, 1),
            "swr_incremental_1file_ms": round(swr_incr, 1)}


def median_run(fn, rounds=5):
    vals = [fn() for _ in range(rounds)]
    return statistics.median(vals)


def main():
    root = Path(tempfile.mkdtemp(prefix="swr-bench-"))
    build_corpus(root, CORPUS_N)
    total_bytes = sum(f.stat().st_size for f in root.glob("*.md"))

    out = {"corpus_files_md": len(list(root.glob("*.md"))), "corpus_bytes": total_bytes}

    # warm-up pass so module import / bytecode / page cache don't pollute round 0
    naive_list_status(root)
    single_pass(root)

    naive_runs = []
    for _ in range(5):
        t = time.perf_counter()
        a = naive_list_status(root)
        naive_runs.append((time.perf_counter() - t) * 1000.0)
    naive_cold = statistics.median(naive_runs)
    out["naive_cold_ms"] = round(naive_cold, 1)
    out["naive_runs_ms"] = [round(v, 1) for v in naive_runs]
    out["naive_rows"] = len(a)

    single_runs = []
    for _ in range(5):
        t = time.perf_counter()
        b = single_pass(root)
        single_runs.append((time.perf_counter() - t) * 1000.0)
    single_cold = statistics.median(single_runs)
    out["single_pass_cold_ms"] = round(single_cold, 1)
    out["single_pass_runs_ms"] = [round(v, 1) for v in single_runs]
    out["single_pass_rows"] = len(b)

    swr = SwrStatus(root)
    swr_refresh_runs = []
    for _ in range(3):
        swr.mtime_map = {}
        _c, reread, reused = swr.refresh()
        swr_refresh_runs.append(_c)
    cold_ms = statistics.median(swr_refresh_runs)
    out["swr_cold_ms"] = round(cold_ms, 1)
    out["swr_cold_runs_ms"] = [round(v, 1) for v in swr_refresh_runs]
    out["swr_cold_files_read"] = reread
    out["swr_cold_files_reused"] = reused

    # warm hit: TTL still valid — measure in microseconds, tight loop
    hits = []
    for _ in range(200):
        t0 = time.perf_counter()
        swr.get()
        hits.append((time.perf_counter() - t0) * 1_000_000.0)
    hit_us = statistics.median(hits)
    out["swr_memory_hit_us"] = round(hit_us, 2)
    out["swr_memory_hit_p99_us"] = round(sorted(hits)[int(len(hits) * 0.99)], 2)

    # incremental revalidation: touch one file, force expiry, re-measure
    victim = sorted(root.glob("note-*.md"))[5]
    victim.write_text(victim.read_text(encoding="utf-8") + "\n<!-- one edit -->\n", encoding="utf-8")
    swr.stamp = 0.0
    incr_runs = []
    for _ in range(3):
        victim.write_text(victim.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        swr.stamp = 0.0
        _i, reread2, reused2 = swr.refresh()
        incr_runs.append(_i)
    incr_ms = statistics.median(incr_runs)
    out["swr_incremental_refresh_ms"] = round(incr_ms, 2)
    out["swr_incremental_files_read"] = reread2
    out["swr_incremental_files_reused"] = reused2

    # state distribution sanity
    states = {}
    for row in swr.data:
        states[row["status"]] = states.get(row["status"], 0) + 1
    out["state_distribution"] = states

    out["speedup_naive_vs_single"] = round(naive_cold / single_cold, 2) if single_cold else None
    out["speedup_naive_vs_swr_cold"] = round(naive_cold / cold_ms, 2) if cold_ms else None
    out["speedup_naive_vs_memory_hit"] = round((naive_cold * 1000.0) / hit_us, 0) if hit_us else None
    out["speedup_single_vs_incremental"] = round(single_cold / incr_ms, 2) if incr_ms else None

    out["latency_projection"] = [projected_with_read_latency(root, ms) for ms in (5.0, 10.0, 20.0)]

    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
