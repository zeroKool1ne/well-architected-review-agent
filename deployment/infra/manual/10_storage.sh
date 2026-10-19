#!/usr/bin/env bash
# S3 bucket for uploads & index, plus the DynamoDB table holding review
# state between the two requests.
#
#     source deployment/infra/manual/00_variables.sh
#     bash deployment/infra/manual/10_storage.sh

set -euo pipefail
: "${BUCKET:?source 00_variables.sh first}"

echo "--- S3: ${BUCKET}"
if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "    exists"
else
  # us-east-1 is the one region where a location constraint is rejected.
  aws s3api create-bucket --bucket "${BUCKET}" --region "${AWS_REGION}" >/dev/null
  echo "    created"
fi

# Uploaded diagrams are customer material. Nothing here should ever be public,
# and the account-level default is not something this project controls, so the
# block is set explicitly on the bucket.
aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
echo "    public access blocked"

aws s3api put-bucket-encryption \
  --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
echo "    default encryption on"

# Uploads are evidence for one review and worthless afterwards. Expiring them
# keeps storage flat and limits how long customer diagrams sit around; the
# index is excluded because it is the artefact the function depends on.
aws s3api put-bucket-lifecycle-configuration \
  --bucket "${BUCKET}" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "expire-uploads",
      "Status": "Enabled",
      "Filter": {"Prefix": "uploads/"},
      "Expiration": {"Days": 7}
    }]
  }'
echo "    uploads expire after 7 days"

echo "--- DynamoDB: ${TABLE}"
if aws dynamodb describe-table --table-name "${TABLE}" >/dev/null 2>&1; then
  echo "    exists"
else
  # On-demand billing: this table sees a handful of writes per demo, and
  # provisioned capacity would cost money while idle for no benefit.
  aws dynamodb create-table \
    --table-name "${TABLE}" \
    --attribute-definitions AttributeName=review_id,AttributeType=S \
    --key-schema AttributeName=review_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --tags "Key=Project,Value=wa-review" >/dev/null
  aws dynamodb wait table-exists --table-name "${TABLE}"
  echo "    created"
fi

# Records expire a day after they are written. An unbounded table of abandoned
# uploads is both a cost and a privacy problem, and TTL deletion is free.
aws dynamodb update-time-to-live \
  --table-name "${TABLE}" \
  --time-to-live-specification "Enabled=true,AttributeName=expires_at" >/dev/null 2>&1 || true
echo "    ttl on expires_at"

echo "done"
