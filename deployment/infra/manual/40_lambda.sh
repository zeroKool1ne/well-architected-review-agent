#!/usr/bin/env bash
# Create or update the function and its URL.
#
#     source deployment/infra/manual/00_variables.sh
#     bash deployment/infra/manual/30_package.sh
#     bash deployment/infra/manual/40_lambda.sh
#
# arm64 because Graviton is cheaper per millisecond and nothing here is
# architecture-sensitive.
#
# 1536 MB of memory for a workload that is not memory-hungry: Lambda scales CPU
# with memory, and the cold start has to unpack numpy and read a 7 MB index.
# Paying for a shorter cold start is cheaper than paying for a long one, since
# billing is per millisecond either way.
#
# The function URL is open. For a demo in a shared teaching account that is the
# honest trade-off — IAM auth would mean signing every browser request, which
# turns a two-minute demo into a detour. Production belongs behind an authorizer,
# and 90_teardown.sh exists so this does not stay reachable after the demo.

set -euo pipefail
: "${FUNCTION:?source 00_variables.sh first}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ZIP="fileb://${ROOT}/build/function.zip"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE}"

ENV_VARS="Variables={INDEX_BUCKET=${BUCKET},UPLOAD_BUCKET=${BUCKET},STATE_TABLE=${TABLE},INDEX_KEY=${INDEX_KEY}}"

echo "--- Function: ${FUNCTION}"
if aws lambda get-function --function-name "${FUNCTION}" >/dev/null 2>&1; then
  aws lambda update-function-code \
    --function-name "${FUNCTION}" --zip-file "${ZIP}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION}"

  aws lambda update-function-configuration \
    --function-name "${FUNCTION}" \
    --timeout 90 --memory-size 1536 \
    --environment "${ENV_VARS}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION}"
  echo "    updated"
else
  # IAM is eventually consistent: a role created moments ago is not always
  # assumable yet, and Lambda rejects the create rather than retrying.
  for attempt in 1 2 3 4 5 6; do
    if aws lambda create-function \
        --function-name "${FUNCTION}" \
        --runtime python3.13 \
        --architectures arm64 \
        --role "${ROLE_ARN}" \
        --handler backend.api.main.handler \
        --zip-file "${ZIP}" \
        --timeout 90 \
        --memory-size 1536 \
        --environment "${ENV_VARS}" \
        --tags "Project=wa-review" >/dev/null 2>&1; then
      echo "    created"
      break
    fi
    echo "    waiting for the role to propagate (${attempt}/6)"
    sleep 10
  done
  aws lambda wait function-active --function-name "${FUNCTION}"
fi

# Logs are kept two weeks. The default is forever, which quietly becomes the
# only line on the bill that grows without anyone touching the system.
aws logs put-retention-policy \
  --log-group-name "/aws/lambda/${FUNCTION}" \
  --retention-in-days 14 >/dev/null 2>&1 || true

echo "--- Function URL"
if aws lambda get-function-url-config --function-name "${FUNCTION}" >/dev/null 2>&1; then
  echo "    exists"
else
  aws lambda create-function-url-config \
    --function-name "${FUNCTION}" \
    --auth-type NONE >/dev/null
  aws lambda add-permission \
    --function-name "${FUNCTION}" \
    --statement-id FunctionURLAllowPublicAccess \
    --action lambda:InvokeFunctionUrl \
    --principal "*" \
    --function-url-auth-type NONE >/dev/null
  echo "    created"
fi

URL=$(aws lambda get-function-url-config --function-name "${FUNCTION}" --query FunctionUrl --output text)
echo
echo "ready: ${URL}"
