# WALKTHROUGH — how to read this codebase

Written for someone opening the project for the first time. Read it in the
order below and each file will make sense before you reach the file that uses
it.

---

## 0. Run it before you read it

```bash
pip install -r requirements.txt
make demo
```

You should see a job queued, a worker process it, and a job table printed.
Then open `out/sample_heatmap.png`. Seeing the output first makes the code
much easier to follow.

If `make demo` fails, run the pieces separately:

```bash
python scripts/make_sample_slide.py data/sample.tif   # writes a fake slide
python -m wsiqc analyze data/sample.tif               # no database involved
```

`analyze` is the fastest way to debug the imaging layer — no server, no
database, no queue.

---

## 1. Reading order

| # | File | Why it comes here |
|---|------|-------------------|
| 1 | `scripts/make_sample_slide.py` | Understand the input before the code that reads it. A slide is mostly white glass with irregular stained blobs, and one band is deliberately blurred. |
| 2 | `wsiqc/imaging/reader.py` | The single most important file. Everything else assumes windowed reads. |
| 3 | `wsiqc/imaging/mask.py` | Finding tissue. Otsu and morphology, hand-written. |
| 4 | `wsiqc/imaging/tiles.py` | Short. The tile grid as a generator. |
| 5 | `wsiqc/imaging/focus.py` | Short. Laplacian variance as a sharpness proxy. |
| 6 | `wsiqc/imaging/analyze.py` | Ties 2–5 together into one `QCReport`. Read this and you understand the domain half. |
| 7 | `wsiqc/db/models.py` | Three tables. The job state machine lives here. |
| 8 | `wsiqc/db/repository.py` | The clever bit: `claim_next_job`. |
| 9 | `wsiqc/api/main.py` | Thin. Now obvious, because the work is elsewhere. |
| 10 | `wsiqc/worker/runner.py` | Where the two halves meet. |

Skim `config.py`, `logging_setup.py` and `cli.py` last — they are plumbing.

---

## 2. The request flow, step by step

Follow one slide through the system.

**Step 1 — submission.** `POST /slides {"path": "sample.tif"}` arrives at
`submit_slide` in `api/main.py`.

- `_resolve_slide_path` turns the relative path into an absolute one and
  refuses anything outside the configured slide directory.
- `repo.get_or_create_slide` hashes the file contents in 1 MB chunks and looks
  for an existing row with that hash.
- If the slide already existed, `repo.find_reusable_job` looks for a job that
  is queued, running or already succeeded. If it finds one, it returns that job
  and sets `duplicate: true`. **No second job is created.** This is the
  idempotency guarantee.
- Otherwise `repo.create_job` inserts a row with `status = 'queued'`.
- The response is **202 Accepted**, not 200 or 201: the work has been accepted
  but not done.

Nothing has touched the image yet. The request returns in milliseconds
regardless of whether the slide is 20 MB or 2 GB.

**Step 2 — claiming.** The worker (`worker/runner.py`) loops. Each pass calls
`process_one`, which calls `repo.claim_next_job`:

```python
candidate = SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1
result   = UPDATE jobs SET status='running', attempts=attempts+1
           WHERE id=:candidate AND status='queued'
if result.rowcount != 1:
    return None      # another worker got there first
```

The `AND status='queued'` in the UPDATE is what makes this safe. Two workers
can both read the same candidate id, but only one UPDATE will change a row —
the other sees `rowcount == 0` and polls again. This is the pattern to be able
to explain out loud.

**Step 3 — analysis.** The worker submits `run_analysis` to a
`ProcessPoolExecutor`. It passes only plain data — a path, two numbers — never
an ORM object, because those cannot be pickled across a process boundary.

Inside the child process, `analyze_slide` runs:

1. `open_slide(path)` picks a backend and reads the pyramid metadata.
2. `tissue_mask(reader)` reads the slide at roughly 1/32 scale, converts to
   saturation, thresholds with Otsu, cleans up with open/close. Result: a small
   boolean array plus the level it was computed at.
3. `iter_tiles(...)` walks the grid and yields only positions where the mask
   says there is tissue. It is a generator — one tile in flight at a time.
4. For each tile: `read_region` decodes just the stored TIFF tiles that window
   touches, and `focus_score` computes the Laplacian variance.
5. `blur_threshold(scores)` picks a cut-off from this slide's own score
   distribution, with an absolute floor so a well-scanned slide does not report
   false failures.
6. Every tile is marked blurred or not, and a `QCReport` comes back.

Then `write_heatmap` and `write_thumbnail` turn the report into PNGs.

**Step 4 — writing back.** The worker takes a fresh session and calls
`repo.mark_succeeded`, which inserts a `Report` row and flips the job to
`succeeded`. On an exception it calls `repo.mark_failed`, which either re-queues
the job (attempts remaining) or marks it `failed` — and **stores the error
text on the row** either way.

**Step 5 — retrieval.** `GET /jobs/{id}/report`:

- 404 if the job does not exist
- 409 if it is still queued or running (the resource will exist, just not yet —
  clients retry on 409 and give up on 404)
- 422 if the job failed, with the stored error as the message
- 200 with the report otherwise

---

## 3. The five things to understand deeply

If you only internalise five things, these:

**1. Why memory stays flat.** `TiffReader.read_region` computes which stored
TIFF tiles intersect the requested window, seeks to each one's byte offset,
decodes it alone, and pastes the overlap into the output. It never calls
`asarray()` on a whole pyramid level. Verify it yourself: `make bench` prints
peak RSS, and it does not move when tile size changes.

**2. Why the grid is a generator.** `iter_tiles` yields. A real 40x slide has
hundreds of thousands of tile positions; building that list is the classic
out-of-memory bug in this domain. There is a test (`test_grid_is_lazy`) whose
only job is to stop someone changing it to a list.

**3. Why processes, not threads.** Tiling and focus scoring are CPU-bound NumPy
work. The GIL serialises Python bytecode across threads, so threads would add
no throughput. `ProcessPoolExecutor` gives each worker its own interpreter.
(NumPy does release the GIL inside some C routines, which is worth mentioning —
but the tile loop as a whole is Python-level.)

**4. Why the queue exists.** A request handler that tiles a 2 GB slide holds a
connection open for minutes, times out behind most proxies, and loses all work
if the process restarts. Queueing turns that into a row in a table that
survives a crash.

**5. What happens when a worker dies.** Nothing, on its own — the job sits in
`running` forever. `reclaim_stale_jobs` sweeps rows whose `started_at` is older
than a timeout and returns them to `queued`. That is why the analysis must be
safe to run twice; this is at-least-once delivery, not exactly-once.

---

## 4. Where to look when something breaks

| Symptom | Look at |
|---------|---------|
| `ImportError: openslide` | Expected without the C library. `open_slide` falls back to TIFF; only `.svs` files need it. |
| Mask is all black or all white | `mask.py`. Check the saturation range — OpenCV uses 0–255, scikit-image uses 0–1. |
| Every tile scores about the same | The mask is not being applied; you are scoring blank glass. Print `tiles_analyzed`. |
| `TypeError: float32 is not JSON serializable` | A NumPy scalar leaked into the report. Cast with `float()`. |
| `DetachedInstanceError` | An ORM object escaped its session. Convert to a Pydantic model inside the session. |
| `database is locked` | SQLite write contention. WAL and busy_timeout are already set in `session.py`; beyond that, move to Postgres. |
| Job stuck in `running` | The worker died. Wait for the reclaim sweep, or lower `WSIQC_STALE_JOB_SECONDS`. |
| API hangs under load | Someone changed an endpoint to `async def` and blocked inside it. |
| Worker cannot reach the database in Docker | Paths differ inside the container. Check the volume mounts in `docker-compose.yml`. |

---

## 5. Configuration

All settings come from the environment with a `WSIQC_` prefix, defaults in
`config.py`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `WSIQC_DATABASE_URL` | `sqlite:///./wsiqc.db` | Any SQLAlchemy URL |
| `WSIQC_SLIDE_DIR` | `data` | Submitted paths must be inside this |
| `WSIQC_OUTPUT_DIR` | `out` | Heatmaps and thumbnails |
| `WSIQC_TILE_SIZE` | `512` | Default tile edge |
| `WSIQC_TARGET_DOWNSAMPLE` | `4.0` | Working magnification |
| `WSIQC_MAX_ATTEMPTS` | `3` | Retries before a job is marked failed |
| `WSIQC_STALE_JOB_SECONDS` | `900` | Reclaim timeout |
| `WSIQC_WORKER_PROCESSES` | `2` | Process pool size |
| `WSIQC_LOG_LEVEL` | `INFO` | |

---

## 6. Using a real slide

Download OpenSlide's `CMU-1.svs` sample (about 180 MB), install the OpenSlide
system library, drop the file in `data/`, and submit it exactly as with the
synthetic slide. Nothing in the code changes — `open_slide` picks the
`OpenSlideReader` backend by file extension. That is what the abstract base
class is for.

Expect the numbers to look different: real tissue coverage is usually 15–40%,
and real focus scores span a much wider range.

---

## 7. Suggested exercises

Rebuilding beats reading. In rough order of value:

1. Add `GET /slides/{id}/jobs` listing every job for a slide.
2. Add a `priority` column and make the worker claim by priority then id.
3. Replace the polling loop with Celery and Redis. The `Worker` class is the
   seam — the analysis code should not change at all.
4. Add DZI export and an OpenSeadragon viewer page.
5. Swap SQLite for Postgres and use `SELECT ... FOR UPDATE SKIP LOCKED`
   instead of the UPDATE-and-check-rowcount claim.
6. Implement Z-stack focus fusion: generate a sample with several focal planes,
   then merge with a Laplacian pyramid.

Each one is a real conversation in an interview, and each one is small enough
to finish in an evening.
