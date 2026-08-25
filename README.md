# Slide QC Service

A backend service that accepts whole-slide pathology images, tiles them, scores
every tile for focus quality, and reports which regions of the scan came out
blurred.

The image processing is the payload. The project is really about the shape of a
service that handles files far larger than memory: a queue, a job state machine,
idempotent submission, and workers that can die without losing work.

![focus heatmap](docs/heatmap.png)

*Green tiles are in focus, red tiles failed the focus threshold, grey means no
tissue. The red band is a deliberately out-of-focus region in the sample slide.*

---

## Why this exists

Scanners like Pramana's produce gigapixel images and re-scan slides
automatically when quality control fails. That requires two things a normal CRUD
service does not have: analysis that never loads a whole image into memory, and
a pipeline that can decide a scan is bad. This is a small, honest version of
both.

---

## Quick start

```bash
pip install -r requirements.txt
make demo
```

`make demo` generates a synthetic slide, submits it, runs a worker once, and
prints the job table. No downloads, no native libraries, no server needed.

To run the real service:

```bash
make api      # terminal 1 -- http://localhost:8000/docs
make worker   # terminal 2
```

```bash
curl -X POST localhost:8000/slides \
  -H 'content-type: application/json' \
  -d '{"path": "sample.tif", "tile_size": 256, "target_downsample": 2.0}'

curl localhost:8000/jobs/1
curl localhost:8000/jobs/1/report
curl localhost:8000/jobs/1/heatmap -o heatmap.png
```

Or with Docker:

```bash
docker compose up --build
```

---

## Architecture

```
   client
     │  POST /slides                    (202 Accepted, returns a job id)
     ▼
┌─────────────┐        ┌──────────────┐        ┌──────────────┐
│  FastAPI    │ writes │   database   │ claims │    worker    │
│  (thin)     │───────▶│ slides/jobs/ │◀───────│  (separate   │
│             │        │   reports    │ writes │   process)   │
└─────────────┘        └──────────────┘        └──────┬───────┘
     ▲  GET /jobs/{id}                                │
     │  GET /jobs/{id}/report                         ▼
   client                                     ┌───────────────┐
                                              │ analyze_slide │
                                              │  mask → tiles │
                                              │  → focus      │
                                              └───────────────┘
```

No image processing happens inside a request handler. The API writes a row; a
worker in a different process picks it up. That separation is the point of the
whole design.

### Job state machine

```
   queued ──claim──▶ running ──success──▶ succeeded
     ▲                  │
     │                  ├──failure, attempts left──┐
     │                  │                          │
     └──────────────────┴──worker died, reclaimed──┘
                        │
                        └──failure, attempts exhausted──▶ failed
```

---

## API

| Method | Path                  | Returns | Notes                                             |
| ------ | --------------------- | ------- | ------------------------------------------------- |
| POST   | `/slides`             | 202     | Queues a job. Same file twice returns the same job |
| GET    | `/jobs`               | 200     | `?status=queued&limit=50&offset=0`                 |
| GET    | `/jobs/{id}`          | 200/404 | Job status and timing                              |
| GET    | `/jobs/{id}/report`   | 200     | 409 while pending, 422 if the job failed           |
| GET    | `/jobs/{id}/heatmap`  | 200     | PNG, 410 if the file was cleaned up                |
| GET    | `/health`             | 200/503 | Touches the database — readiness, not liveness     |

Interactive docs at `/docs`.

---

## Performance

Measured on a 1-core container, 4096×3072 synthetic slide (22 MB on disk):

| Tile size | Tiles analysed | Seconds | Tiles/sec | Peak RSS |
| --------- | -------------- | ------- | --------- | -------- |
| 128       | 37             | 0.07    | 495       | 43.5 MB  |
| 256       | 14             | 0.06    | 251       | 43.5 MB  |
| 512       | 5              | 0.06    | 80        | 47.6 MB  |

**Peak memory does not grow with tile size or slide size.** That is the number
worth looking at: the reader decodes only the stored TIFF tiles a requested
window touches, so a 2 GB slide costs the same as a 20 MB one.

Parallel workers on the same 1-core box show no speedup, as expected — there is
one core, and the work is CPU-bound. On a multi-core machine the process pool
scales roughly linearly until JPEG decode saturates disk I/O. The bottleneck is
decode, not the focus maths.

---

## Design decisions worth defending

**Pure NumPy, no OpenCV or scikit-image.** Otsu's method, the Laplacian and the
binary morphology are about forty lines between them. Fewer install failures,
and every step can be explained rather than cited.

**Tile grid is a generator.** A 40x slide at 512px tiles is hundreds of
thousands of positions. Building that list is the first thing that exhausts
memory on a real slide.

**Claiming a job is an UPDATE, not a SELECT.** Two workers can read the same
queued row. The claim carries `status == 'queued'` in its WHERE clause and
checks `rowcount == 1`, so exactly one worker wins the race.

**Stale jobs are reclaimed.** A worker killed mid-job leaves its row in
`running` forever; nothing re-queues it on its own. A visible-timeout sweep
returns it to the queue, which is why analysis must be safe to run twice.

**Idempotency by content hash, not filename.** The same slide submitted under
two names is one slide. The hash is computed in 1 MB chunks, because these
files do not fit in memory.

**Endpoints are `def`, not `async def`.** FastAPI runs sync handlers in a
threadpool. Declaring them `async` and then blocking inside is how a FastAPI
service stops answering under load.

**Submitted paths are validated.** Without the check, `../../etc/passwd` is a
valid slide path.

---

## Layout

```
wsiqc/
  imaging/         no knowledge of HTTP, databases or queues
    reader.py      windowed reads; OpenSlide and TIFF backends
    mask.py        tissue detection (Otsu on saturation)
    tiles.py       lazy tile grid
    focus.py       Laplacian variance sharpness scoring
    analyze.py     orchestrates the above into a QCReport
    render.py      heatmap and thumbnail PNGs
  db/
    models.py      slides, jobs, reports
    session.py     engine, sessions, SQLite pragmas
    repository.py  queries, job claiming, retries
  api/
    main.py        endpoints
    schemas.py     request/response contract
  worker/
    runner.py      claim, run in a child process, write back
  config.py        environment-driven settings
  cli.py           analyze / submit / worker / jobs
scripts/
  make_sample_slide.py
  benchmark.py
tests/             26 tests, run in about 8 seconds
```

---

## Limitations and next steps

Named honestly, because they are the interesting part:

- **SQLite** is the queue and the database. Fine for one machine; Postgres with
  `SELECT ... FOR UPDATE SKIP LOCKED` is the real answer, or Celery on Redis.
- **No authentication.** Every endpoint is open.
- **Slides are referenced by path**, not uploaded. Real deployments would take
  a pre-signed object-storage URL and stream the file rather than buffer it.
- **Z-stack focus fusion** is not implemented. Real scanners capture several
  focal planes and merge them; Laplacian-pyramid focus stacking is the method.
- **Artifact classification** (debris, pen marks, coverslip edges, folded
  tissue) needs a small CNN, and labelled tiles to train it.
- **No DZI export.** A deep-zoom pyramid plus an OpenSeadragon viewer would
  make the result explorable rather than a static heatmap.
- **Focus scoring is relative.** The threshold comes from each slide's own
  score distribution. A calibrated absolute threshold would need real slides
  with known focus quality.

## Running the tests

```bash
pytest -q          # 26 passed in ~8s
```

Every test gets its own temporary database and its own tiny synthetic slide.
Nothing touches a real `wsiqc.db`, and nothing needs a server running.
