#!/usr/bin/env bash
# Remove everything this project created.
#
#     source deployment/infra/manual/00_variables.sh
#     bash deployment/infra/manual/90_teardown.sh
#
# The account is shared and the access ends with the course, so leaving a
# reachable endpoint and a bucket of uploaded diagrams behind is not an option.
# Deleting is also the only honest way to claim the costs stopped.
#
# Everything here is scoped to the project prefix. Nothing in this script
# touches a resource it did not create.

set -euo pipefail
: "${PREFIX:?source 00_variables.sh first}"

echo "This deletes all ${PREFIX}-* resources in account ${ACCOUNT_ID}."
read -r -p "Type the account id to confirm: " CONFIRM
[ "${CONFIRM}" = "${ACCOUNT_ID}" ] || { echo "aborted"; exit 1; }

echo "--- API Gateway"
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${PREFIX}-http'].ApiId | [0]" --output text)
if [ "${API_ID}" != "None" ] && [ -n "${API_ID}" ]; then
  aws apigatewayv2 delete-api --api-id "${API_ID}"
  echo "    deleted ${API_ID}"
fi

echo "--- Lambda"
aws lambda delete-function-url-config --function-name "${FUNCTION}" 2>/dev/null || true
aws lambda delete-function --function-name "${FUNCTION}" 2>/dev/null && echo "    deleted ${FUNCTION}" || true
aws logs delete-log-group --log-group-name "/aws/lambda/${FUNCTION}" 2>/dev/null || true

echo "--- DynamoDB"
aws dynamodb delete-table --table-name "${TABLE}" >/dev/null 2>&1 && echo "    deleted ${TABLE}" || true

echo "--- S3"
# Objects first; a bucket with contents cannot be deleted.
aws s3 rm "s3://${BUCKET}" --recursive --only-show-errors 2>/dev/null || true
aws s3api delete-bucket --bucket "${BUCKET}" 2>/dev/null && echo "    deleted ${BUCKET}" || true

echo "--- IAM"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY}"
aws iam detach-role-policy --role-name "${ROLE}" --policy-arn "${POLICY_ARN}" 2>/dev/null || true
for v in $(aws iam list-policy-versions --policy-arn "${POLICY_ARN}" \
             --query 'Versions[?!IsDefaultVersion].VersionId' --output text 2>/dev/null); do
  aws iam delete-policy-version --policy-arn "${POLICY_ARN}" --version-id "$v" 2>/dev/null || true
done
aws iam delete-policy --policy-arn "${POLICY_ARN}" 2>/dev/null && echo "    deleted ${POLICY}" || true
aws iam delete-role --role-name "${ROLE}" 2>/dev/null && echo "    deleted ${ROLE}" || true

echo
echo "--- Anything left with the prefix?"
aws resourcegroupstaggingapi get-resources \
  --tag-filters "Key=Project,Values=wa-review" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null || true
echo "done"
