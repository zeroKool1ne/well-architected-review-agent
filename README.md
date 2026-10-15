# Well-Architected Review Agent

![Python](https://img.shields.io/badge/python-3.13-blue)
![AWS](https://img.shields.io/badge/AWS-Bedrock%20%7C%20Lambda%20%7C%20DynamoDB-FF9900)
![Tests](https://img.shields.io/badge/tests-41%20passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

A multimodal AI pipeline that reads an **AWS architecture diagram as an image** and reviews it against the AWS Well-Architected Framework — with a human confirming the reading at the point where the model is measurably unreliable.

> Ironhack Final Project (GenAI). Individual work.

---

## The problem

When someone asks whether an AWS architecture is sound, usually one artefact exists: a diagram in a slide deck. No Terraform state, no configuration export — a picture.

Checking it by hand means holding 178 best practices in your head and comparing them against that picture. It takes days, so it rarely happens. This automates the first pass, and is explicit about the one step a model cannot be trusted with.

---

## How it works

![Architecture overview](docs/diagrams/01_architecture_overview.svg)

```
PHASE 1 — EXTRACT (AI)          HUMAN GATE          PHASE 2 — REVIEW (AI)

  Upload  →  Extraction Agent → Human confirms  → Pillar Agents ×3 →  Findings
    │              │                  │                  │               │
 S3 + API      Bedrock            DynamoDB          index in S3      Lambda +
   Gateway    (Nova Lite)                                           CloudWatch
```

### Agent topology

![Agent topology](docs/diagrams/02_agent_topology.svg)

| Agent | Role | Tools |
|---|---|---|
| **Supervisor** | Routes, merges, deduplicates, ranks | — |
| **Extraction Agent** | Image → graph, in two passes | `read_diagram` |
| **Pillar Agents ×3** | Security, Cost, Reliability — in parallel | `search_pillars(pillar=…)` |
| **Verifier** | Judges whether each finding is supported | — |

The three pillar agents are **one factory instantiated three times**: same code, different system prompt, different metadata filter on retrieval.

Routing is code, not a model. A reviewer that occasionally forgets to run a pillar would be worse than one that always runs all three, and nothing about that decision needs judgement. Models do the judging — reading a diagram, weighing an architecture against guidance. Code does the control flow.

---

## What the measurements changed

Every structural decision here came from a measurement, and two of them overturned the first design.

### 1. The vision model invents connections

A spike before any code was written, on a real 1924×1959 px diagram:

| | Result |
|---|---|
| Box labels (OCR) | **Excellent** — all 21 read correctly |
| Connections | **Invented** — the boxes were chained into a sequence not in the picture |
| Self-reported confidence | **Unusable** — 0.95 throughout, nothing flagged as unclear |

Splitting extraction into two passes — boxes first, then arrows, with "no arrows visible" made an explicitly acceptable answer — improved this substantially but did not fix it:

| Test case | Nodes | Edges | Hallucinated edges |
|---|---|---|---|
| `01_simple_api` | 100% | 100% | **0%** |
| `02_flawed_webapp` | 100% | 73% | **42%** |

**So a human confirms the graph before any review runs.** Not as a rubber stamp at the end — at the one step where the model is provably weak, and only there.

### 2. Telling a model not to hallucinate does not work

The first review agents had this in their system prompt:

> *Never write that something is missing, disabled or unencrypted because the diagram is silent about it.*

With that instruction in place, the deliberately **flawless** test diagram still drew findings about missing mTLS, an unnamed availability zone and absent CloudFront. Strengthening the wording made it worse.

A model that wants to be useful will find something. Instruction competes with that pull, and loses.

**The fix was structural, not rhetorical:** a separate call that can only judge, never propose. It receives the graph and the proposed findings and returns keep or drop. Having no incentive to find something, it rejects the inferences.

### 3. Specialists see what a generalist misses

Retrieval measured across pillars: for unambiguous questions, filtering changes nothing. For topics belonging to several pillars, an unfiltered search returns one pillar and stops.

```
"the workload is deployed across multiple availability zones"
  unfiltered:       reliability, 3 of 3 — the cost of cross-zone traffic never appears
"backups of the database"
  unfiltered:       reliability, 3 of 3 — whether they are encrypted is never asked
```

Filtering does not produce better passages. It **forces a perspective that would otherwise never be taken**.

---

## Evaluation

Two baselines, each removing exactly one decision and keeping the rest, so the difference is attributable. Means over repeated runs; temperature is 0 and the same diagram still does not produce the same review twice.

`python evaluation/run.py --runs 3` · raw numbers in [`evaluation/results/evaluation.json`](evaluation/results/evaluation.json)

### On the diagram with four planted problems

| Configuration | Found | **Of 4 planted** | False positives | Latency | Cost |
|---|---|---|---|---|---|
| B1 — specialists, no verification | 7 | **4/4** | 3 | 1.7 s | $0.00045 |
| B2 — one generalist, verified | 3.3 | **3/4** | 0.3 | 4.5 s | $0.00047 |
| **Full pipeline** | 6 | **4/4** | 2 | 3.3 s | $0.00056 |

### On the deliberately clean diagram

| Configuration | Findings reported | Correct answer |
|---|---|---|
| B1 — no verification | 5 | 0 |
| **Full pipeline** | 1.3 | 0 |

### Reading the numbers

- **The generalist misses a problem.** 3 of 4 against 4 of 4 — the gap the retrieval measurement predicted. It is also *slower*, because nothing runs in parallel.
- **Verification cuts false positives without costing recall.** Five to roughly one on the clean diagram, and all four real findings survive.
- **Cost is counted, not estimated.** Every Bedrock call records its tokens: about **$0.0005 per review**, or two thousand reviews per dollar.

### What is still wrong

- The clean diagram still draws roughly one false positive per run.
- Pillar attribution drifts: missing backups are sometimes filed under cost.
- Results vary between runs despite temperature 0, which is why everything above is a mean.

---

## Running it

### Local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .
cp .env.example .env

python backend/ingestion/fetch_corpus.py     # download the whitepapers
python backend/ingestion/build_index.py      # chunk, validate, embed (~5 min)
python backend/ingestion/export_index.py     # flat array for deployment

uvicorn backend.api.main:app --reload        # http://localhost:8000
```

Needs AWS credentials with Bedrock access in `us-east-1`, and `graphviz` (`brew install graphviz`) to regenerate test diagrams.

### Deploy

```bash
source deployment/infra/manual/00_variables.sh
bash deployment/infra/manual/10_storage.sh      # S3, DynamoDB
bash deployment/infra/manual/20_iam.sh          # role and policy
bash deployment/infra/manual/30_package.sh      # zip, upload index
bash deployment/infra/manual/40_lambda.sh       # function
bash deployment/infra/manual/50_apigateway.sh   # HTTP API
```

Each service gets its own numbered script so the order is visible and single steps can be repeated. `90_teardown.sh` removes everything.

> **On the demo endpoint:** this runs in a shared teaching account whose access ends with the course, so no permanent URL is promised. The scripts rebuild it in about five minutes, and the stack is torn down after the demo rather than left reachable.

### Tests

```bash
pytest                   # 41 tests, AWS mocked with moto, no network, no cost
ruff check backend/ evaluation/
```

---

## Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Model provider | **Amazon Nova Lite** on Bedrock | Anthropic models are blocked in the shared account — Bedrock requires a use-case form that should not be submitted against an account one does not own. Nova is multimodal, available, cheaper. |
| Model id | `us.amazon.nova-lite-v1:0` | The `us.` inference-profile prefix is mandatory; the bare id is rejected with `ValidationException`. |
| Auth to Bedrock | **IAM role** | No API key stored anywhere. |
| Vector store | **Chroma** to build, **numpy array** to serve | chromadb carries 82 MB of ONNX runtime for an embedding model nothing here calls. Both paths return identical passages at identical distances. |
| Packaging | **zip**, not a container | 60 MB of dependencies fits Lambda's limit with room to spare, and needs no registry or image pull. |
| Compute | Lambda, arm64, 1536 MB | Graviton is cheaper per ms; Lambda scales CPU with memory, so paying for a shorter cold start is cheaper than a long one. |
| HTTP | **API Gateway**, not a function URL | Public function URLs are blocked at the organisation level in this account — correct policy, `AuthType NONE`, still 403. An HTTP API is where throttling and an authorizer belong anyway. |
| HITL state | DynamoDB, two endpoints | Lambda is stateless, and explicit state is inspectable when something goes wrong. |
| Upload | In the request, not presigned | Diagrams sit well under the payload limit; one round trip is simpler to operate and demonstrate than three. |
| Relevance floor | 0.75 cosine distance | Where a pillar has nothing to say, its closest passage sits around 0.87. Below the floor a specialist reports nothing instead of constructing something. |

---

## Repository

```
backend/
  agents/        extraction, pillar factory, verifier, supervisor
  tools/         retrieval with the pillar filter
  api/           FastAPI: /extract, /review/{id}
  core/          config, models, Bedrock access, state store
  ingestion/     corpus download, chunking, indexing, export
frontend/        upload and human correction UI
deployment/
  infra/manual/  one numbered script per service
evaluation/
  datasets/      generated diagrams with their ground truth
  run.py         the evaluation
experiments/     measurements, including what did not ship
tests/           pytest + moto
docs/diagrams/   architecture and agent topology
```

### Test data

Diagrams are generated from code, so the ground truth is exact rather than hand-labelled — which is what makes the hallucinated-edge rate measurable at all. `01_simple_api` is deliberately flawless, to catch a reviewer that invents findings to look useful. `02_flawed_webapp` carries one violation per pillar, stated on the node labels because a vision model can only report what the picture says.

---

## Known limitations

- **Three pillars, not six.** The factory scales to all six; three were run to keep latency and cost in budget, and because ground truth exists for those three.
- **Generated test diagrams look alike.** Same icons, same layout. A hand-drawn diagram from a customer deck is not represented, which is why a real diagram is kept as an additional case.
- **The human gate is mandatory, by design.** This is not a fire-and-forget tool and does not try to be.
- **`REL12-BP06` is absent from the corpus** — it never appears as a heading in the source PDF, only as a cross-reference. Recorded in `KNOWN_ABSENT` so the completeness check stays strict.

---

## Credits and sources

- **AWS Well-Architected Framework** whitepapers — published by AWS, used as the retrieval corpus
- **Amazon Bedrock** — Nova Lite for vision and reasoning, Titan Embed v2 for retrieval
- **Chroma** — vector store for index construction
- **FastAPI**, **Mangum**, **diagrams** — API, Lambda adapter, test fixtures
- **Tooling** — AWS CLI, Claude Code

## License

MIT — see [LICENSE](LICENSE).
