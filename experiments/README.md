# Experiments

Spikes and dead ends. Kept deliberately: the reasoning behind the production
design is only legible if the discarded options are visible too.

## `vision_probe.py` — can a vision model read an architecture diagram?

Run on 6 Oct 2026 against a real 1924×1959 px AWS architecture diagram.

```bash
python experiments/vision_probe.py path/to/diagram.png
```

**Result:** label OCR was excellent (21/21 boxes correct), connection extraction
was hallucinated (boxes chained linearly into edges absent from the image), and
self-reported confidence was useless (0.95 everywhere, no unreadable regions
flagged).

**Consequence:** the human-in-the-loop gate sits at the extraction step rather
than at the end of the pipeline, and `hallucinated-edge rate` became a primary
evaluation metric.

**Also learned here:** Bedrock rejects bare model ids for current models —
`anthropic.claude-haiku-4-5-...` returns `ValidationException`, while the
`us.`-prefixed inference profile works. Anthropic models were then found to be
unavailable in the shared course account entirely (`ResourceNotFoundException`:
use-case details not submitted), which is why this probe runs on Amazon Nova.
