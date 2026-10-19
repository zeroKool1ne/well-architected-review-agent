"""HTTP surface: two endpoints with the human gate between them.

    POST /extract   image in, graph out, stored as pending
    GET  /review/{id}   what the model claimed, for a person to correct
    POST /review/{id}   the corrected graph in, findings out

The split is the gate. /review refuses to run on a graph nobody has confirmed,
which is enforced here rather than left to the caller to respect — the whole
reason the gate exists is that the extracted graph cannot be trusted, and a
gate that can be skipped is not a gate.

The image is uploaded in the request rather than through a presigned URL.
Diagrams are well under Lambda's payload limit, and one round trip is simpler
to operate and to demonstrate than three. S3 still holds the image, written
server-side, so a reviewer can see what was read.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.agents.extraction import extract_graph
from backend.agents.supervisor import review as run_review
from backend.core import store
from backend.core.config import ROOT, settings
from backend.core.models import Graph, ReviewResult

log = logging.getLogger("review-agent")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="Well-Architected Review Agent",
    description="Reads an AWS architecture diagram and reviews it against the "
    "Well-Architected Framework, with a human confirming the reading first.",
    version="1.0.0",
)

FRONTEND = ROOT / "frontend" / "index.html"
SLIDES = ROOT / "docs" / "slides" / "index.html"
EVALUATION = ROOT / "evaluation" / "results" / "evaluation.json"

# Bedrock Nova Lite only accepts these formats 
# anything else will be rejected before function call
FORMATS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp"}

MAX_BYTES = 5 * 1024 * 1024


class ExtractResponse(BaseModel):
    review_id: str
    status: str
    graph: Graph
    elapsed_seconds: float
    note: str


class ConfirmRequest(BaseModel):
    graph: Graph


@app.get("/", include_in_schema=False)
def index():
    if FRONTEND.exists():
        return FileResponse(FRONTEND)
    return JSONResponse({"service": "well-architected-review-agent", "docs": "/docs"})


@app.get("/slides", include_in_schema=False)
def slides():
    """The presentation, served from the same origin as the app.

    Same origin is the point: the demo slide embeds the live tool in an iframe,
    which only works without cross-origin headers if both come from here. It
    also means the talk is deployed with the thing it describes.
    """
    if SLIDES.exists():
        return FileResponse(SLIDES)
    raise HTTPException(status_code=404, detail="Slides not bundled in this deployment")


@app.get("/evaluation.json", include_in_schema=False)
def evaluation():
    """The measured numbers, so the charts in the talk read from the real file."""
    if EVALUATION.exists():
        return FileResponse(EVALUATION, media_type="application/json")
    raise HTTPException(status_code=404, detail="No evaluation results bundled")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": settings.chat_model}


@app.post("/extract", response_model=ExtractResponse)
async def extract(file: UploadFile) -> ExtractResponse:
    """Read a diagram. The result is explicitly unconfirmed."""
    suffix = Path(file.filename or "").suffix.lstrip(".").lower()
    if suffix not in FORMATS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image type '{suffix}'. Use one of: {', '.join(FORMATS)}",
        )

    image = await file.read()
    if not image:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(image) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image is {len(image) / 1e6:.1f} MB; the limit is {MAX_BYTES / 1e6:.0f} MB",
        )

    started = time.perf_counter()
    try:
        graph = extract_graph(image, FORMATS[suffix])
    except ValueError as exc:
        # The model returned something unparseable. That is a model failure, not
        # a client error, and saying so plainly beats a generic 500.
        log.exception("extraction failed")
        raise HTTPException(
            status_code=502, detail=f"Model returned no usable graph: {exc}"
        ) from exc
    elapsed = time.perf_counter() - started

    review_id = store.new_id()
    store.create_pending(review_id, graph, source=file.filename or "upload")

    if settings.upload_bucket:
        from backend.core.aws import s3

        s3().put_object(
            Bucket=settings.upload_bucket,
            Key=f"uploads/{review_id}.{suffix}",
            Body=image,
            ContentType=f"image/{FORMATS[suffix]}",
        )

    log.info(
        "extracted review_id=%s nodes=%d edges=%d in %.1fs",
        review_id, len(graph.nodes), len(graph.edges), elapsed,
    )

    return ExtractResponse(
        review_id=review_id,
        status=store.PENDING,
        graph=graph,
        elapsed_seconds=round(elapsed, 2),
        note=(
            "This reading is unconfirmed. The model reads labels reliably but "
            "invents connections; correct the graph and POST it back to "
            f"/review/{review_id} to start the review."
        ),
    )


@app.get("/review/{review_id}")
def get_review(review_id: str) -> dict:
    record = store.get(review_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No review with id {review_id}")
    return record


@app.post("/review/{review_id}", response_model=ReviewResult)
def post_review(review_id: str, body: ConfirmRequest) -> ReviewResult:
    """Confirm the graph and run the review on it.

    The caller sends the whole graph back rather than a patch. A diagram is
    small, and a full replacement makes the confirmed state unambiguous: what
    comes back is what gets reviewed, with no merge rules to reason about.
    """
    record = store.get(review_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No review with id {review_id}")

    if not body.graph.nodes:
        raise HTTPException(status_code=400, detail="Confirmed graph has no services")

    store.confirm(review_id, body.graph)

    started = time.perf_counter()
    result = run_review(body.graph)
    elapsed = time.perf_counter() - started

    
    store.save_findings(
        review_id,
        # f.model_dump() => pydantic-object instanciated from class Finding in models.py
        # a Finding carries six fields: pillar, issue, evidence, recommendation,
        # citation and severity
        # model_dump() translates object into data dynamoDB and json.dumps can read
        [f.model_dump() for f in result.findings],          
        [f.model_dump() for f in result.rejected],          
    )

    # This is transparency, most tools only show results. 
    # But how to I know this thing really put in the work:
    # findings(with citation, best-practice-nr and page)
    # rejected(what has been thrown away and why)
    # pillars_with_nothing_to_say(which of the 3 agents didn't say anything)
    log.info(
        "reviewed review_id=%s findings=%d rejected=%d silent=%s in %.1fs",
        review_id, len(result.findings), len(result.rejected),
        # translates pillar-enums into readable strings for CloudWatch-Log
        # will go out through POST / review
        [str(p) for p in result.pillars_with_nothing_to_say], elapsed,
    )

    return result   # displayed in browser as JSON


# Images the deck references. Mounted at the paths the slides resolve to, so
# the same file works locally and deployed without rewriting any src.
for mount, directory in (
    ("/evaluation/datasets", ROOT / "evaluation" / "datasets"),
    ("/docs/diagrams", ROOT / "docs" / "diagrams"),
):
    if directory.exists():
        app.mount(mount, StaticFiles(directory=directory), name=mount.strip("/").replace("/", "-"))


# Lambda entry point. Mangum translates the function URL's event into the ASGI
# call FastAPI expects, so the same app runs locally with uvicorn and on Lambda
# unchanged.
try:
    from mangum import Mangum

    handler = Mangum(app, lifespan="off")
except ImportError:  # pragma: no cover - only missing in trimmed environments
    handler = None
