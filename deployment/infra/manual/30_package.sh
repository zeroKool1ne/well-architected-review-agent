#!/usr/bin/env bash
# Build the deployment zip and upload the index.
#
#     source deployment/infra/manual/00_variables.sh
#     bash deployment/infra/manual/30_package.sh
#
# A zip rather than a container image. The function's dependencies come to
# roughly 60 MB once chromadb and its ONNX runtime are left out, which fits
# inside Lambda's 250 MB unpacked limit comfortably, and a zip needs no
# registry, no Docker daemon and no image pull on cold start.
#
# Wheels are fetched for the target platform rather than this laptop's: pip
# resolves to macOS arm64 wheels by default, and numpy would load as a broken
# binary on the function.

set -euo pipefail
: "${BUCKET:?source 00_variables.sh first}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
BUILD="${ROOT}/build"
ZIP="${ROOT}/build/function.zip"

rm -rf "${BUILD}"
mkdir -p "${BUILD}/package"

echo "--- Dependencies (linux/arm64)"
"${ROOT}/.venv/bin/pip" install \
  --quiet \
  --target "${BUILD}/package" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  --upgrade \
  -r "${ROOT}/deployment/requirements-lambda.txt"

# Metadata, tests and compiled caches ship nothing useful to a function and are
# a meaningful share of the size.
find "${BUILD}/package" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}/package" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}/package" -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true
echo "    $(du -sh "${BUILD}/package" | cut -f1)"

echo "--- Application code"
cp -R "${ROOT}/backend" "${BUILD}/package/backend"
mkdir -p "${BUILD}/package/frontend"
cp "${ROOT}/frontend/index.html" "${BUILD}/package/frontend/index.html"

# The talk ships with the thing it describes, so the demo slide can embed the
# live tool from the same origin.
mkdir -p "${BUILD}/package/docs/slides" "${BUILD}/package/evaluation/results"
cp "${ROOT}/docs/slides/index.html" "${BUILD}/package/docs/slides/" 2>/dev/null || true
cp -R "${ROOT}/docs/diagrams" "${BUILD}/package/docs/" 2>/dev/null || true
cp "${ROOT}/evaluation/results/evaluation.json" "${BUILD}/package/evaluation/results/" 2>/dev/null || true

# Only the images from the datasets; the truth files and the generator are not
# needed at runtime and the png files are what the deck shows.
mkdir -p "${BUILD}/package/evaluation/datasets"
(cd "${ROOT}/evaluation/datasets" && find . -name "diagram.png" -exec rsync -R {} "${BUILD}/package/evaluation/datasets/" \;) 2>/dev/null || true

# Ingestion runs on a laptop, not in the function, and importing it would pull
# in chromadb and pypdf, which are not in the package.
rm -rf "${BUILD}/package/backend/ingestion"
find "${BUILD}/package/backend" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

echo "--- Zip"
(cd "${BUILD}/package" && zip -qr "${ZIP}" .)
echo "    $(du -h "${ZIP}" | cut -f1)  ->  ${ZIP}"

echo "--- Index to S3"
tar -czf "${BUILD}/index.tar.gz" -C "${ROOT}/data" index
aws s3 cp "${BUILD}/index.tar.gz" "s3://${BUCKET}/${INDEX_KEY}" --only-show-errors
echo "    $(du -h "${BUILD}/index.tar.gz" | cut -f1)  ->  s3://${BUCKET}/${INDEX_KEY}"

echo "done"
