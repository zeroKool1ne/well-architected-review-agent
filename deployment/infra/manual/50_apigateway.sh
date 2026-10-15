#!/usr/bin/env bash
# HTTP API in front of the function.
#
#     source deployment/infra/manual/00_variables.sh
#     bash deployment/infra/manual/50_apigateway.sh
#
# A function URL was the first choice — one fewer service, no extra hop. The
# shared teaching account rejects public function URLs at the organisation
# level: the resource policy is correct and the auth type is NONE, and requests
# still come back 403 while a direct invoke returns 200.
#
# That is not a workaround so much as the arrangement this would have had in
# production anyway. An HTTP API is the place to put throttling, a custom
# domain and an authorizer later, none of which a function URL supports.
#
# HTTP API rather than REST API: roughly a third of the price, lower latency,
# and nothing here needs request validation, API keys or usage plans.

set -euo pipefail
: "${FUNCTION:?source 00_variables.sh first}"

API_NAME="${PREFIX}-http"
FUNCTION_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION}"

echo "--- HTTP API: ${API_NAME}"
API_ID=$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)

if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
  # A payload-format-2.0 proxy integration on $default sends every method and
  # path straight through, which is what an ASGI app behind Mangum expects.
  API_ID=$(aws apigatewayv2 create-api \
    --name "${API_NAME}" \
    --protocol-type HTTP \
    --target "${FUNCTION_ARN}" \
    --route-key 'ANY /{proxy+}' \
    --tags "Project=wa-review" \
    --query ApiId --output text)
  echo "    created ${API_ID}"
else
  echo "    exists ${API_ID}"
fi

# create-api --target builds the integration and route but no permission, so
# the first request would fail with a 500 the logs do not explain.
aws lambda add-permission \
  --function-name "${FUNCTION}" \
  --statement-id apigateway-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
  >/dev/null 2>&1 || true
echo "    invoke permission set"

# 'ANY /{proxy+}' does not match the root path itself, which is where the UI is.
ROOT_ROUTE=$(aws apigatewayv2 get-routes --api-id "${API_ID}" \
  --query "Items[?RouteKey=='ANY /'].RouteId | [0]" --output text)

if [ "${ROOT_ROUTE}" = "None" ] || [ -z "${ROOT_ROUTE}" ]; then
  INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "${API_ID}" \
    --query 'Items[0].IntegrationId' --output text)
  aws apigatewayv2 create-route \
    --api-id "${API_ID}" \
    --route-key 'ANY /' \
    --target "integrations/${INTEGRATION_ID}" >/dev/null
  echo "    root route added"
fi

API_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"

# The function URL is blocked and would only confuse whoever reads this later.
aws lambda delete-function-url-config --function-name "${FUNCTION}" >/dev/null 2>&1 || true

echo
echo "ready: ${API_URL}"
