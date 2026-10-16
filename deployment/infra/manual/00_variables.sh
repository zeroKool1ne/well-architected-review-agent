#!/usr/bin/env bash
# Shared names and ids. Source this before any of the numbered scripts:
#
#     source deployment/infra/manual/00_variables.sh
#
# Everything carries the same prefix and the same tag so the resources can be
# told apart from everything else in a shared course account, and so cost can
# be traced without reading the account-level bill.

set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
export PREFIX="wareview"
export PROJECT_TAG="Project=wa-review"

export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

export BUCKET="${PREFIX}-${ACCOUNT_ID}"
export TABLE="${PREFIX}-reviews"
export ROLE="${PREFIX}-lambda"
export POLICY="${PREFIX}-lambda-policy"
export FUNCTION="${PREFIX}-api"

# The index is built locally and shipped as an artefact. Rebuilding it inside
# Lambda would mean downloading 669 pages of PDF and running 1,297 embedding
# calls on every cold start.
export INDEX_KEY="index/index.tar.gz"

echo "account ${ACCOUNT_ID} · region ${AWS_REGION} · prefix ${PREFIX}"
