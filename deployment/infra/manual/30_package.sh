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
