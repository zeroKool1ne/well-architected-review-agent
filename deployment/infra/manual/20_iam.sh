#!/usr/bin/env bash
# The execution role for the function, scoped to what the application actually
# does and nothing else.
#
#     source deployment/infra/manual/00_variables.sh
#     bash deployment/infra/manual/20_iam.sh
#
# Why each permission is shaped the way it is:
#
#   bedrock    Two models, named individually. An inference profile needs both
#              the profile ARN and the foundation model ARN in every region the
#              profile can route to — granting only the profile fails at call
#              time with an unhelpful AccessDenied.
#
#   s3         Object operations are scoped to the bucket's contents with /*.
#              ListBucket is a bucket-level action and takes the bucket ARN
#              without /*, which is the usual mistake in this policy.
#
#   dynamodb   Only the three item operations the state store uses. No Scan,
#              no Query: nothing in the application ever reads the table by
#              anything other than its key.
#
#   logs       Written to the function's own log group only.
#
# The one place a wildcard is unavoidable is the log stream: stream names are
# generated per invocation, so they cannot be enumerated in advance. Every
# other resource here is named.

set -euo pipefail
: "${ROLE:?source 00_variables.sh first}"

TRUST=$(cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
JSON
)

echo "--- Role: ${ROLE}"
if aws iam get-role --role-name "${ROLE}" >/dev/null 2>&1; then
  echo "    exists"
else
  aws iam create-role \
    --role-name "${ROLE}" \
    --assume-role-policy-document "${TRUST}" \
    --description "Execution role for the Well-Architected review agent" \
    --tags "Key=Project,Value=wa-review" >/dev/null
  echo "    created"
fi

POLICY_DOC=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeTheTwoModelsWeUse",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": [
        "arn:aws:bedrock:${AWS_REGION}:${ACCOUNT_ID}:inference-profile/us.amazon.nova-lite-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:${AWS_REGION}::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    },
    {
      "Sid": "ReadTheIndexAndWriteUploads",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    },
    {
      "Sid": "ListBucketIsABucketLevelAction",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::${BUCKET}"
    },
    {
      "Sid": "ReviewStateByKeyOnly",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE}"
    },
    {
      "Sid": "OwnLogGroupOnly",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${FUNCTION}:*"
    }
  ]
}
JSON
)

POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY}"

echo "--- Policy: ${POLICY}"
if aws iam get-policy --policy-arn "${POLICY_ARN}" >/dev/null 2>&1; then
  # A customer managed policy keeps at most five versions; clearing the old
  # non-default ones keeps repeated runs of this script from hitting that wall.
  for v in $(aws iam list-policy-versions --policy-arn "${POLICY_ARN}" \
               --query 'Versions[?!IsDefaultVersion].VersionId' --output text); do
    aws iam delete-policy-version --policy-arn "${POLICY_ARN}" --version-id "$v"
  done
  aws iam create-policy-version \
    --policy-arn "${POLICY_ARN}" \
    --policy-document "${POLICY_DOC}" \
    --set-as-default >/dev/null
  echo "    updated"
else
  aws iam create-policy \
    --policy-name "${POLICY}" \
    --policy-document "${POLICY_DOC}" \
    --description "Least-privilege permissions for the review agent" >/dev/null
  echo "    created"
fi

aws iam attach-role-policy --role-name "${ROLE}" --policy-arn "${POLICY_ARN}"
echo "    attached to ${ROLE}"

export ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE}"
echo "role arn: ${ROLE_ARN}"
