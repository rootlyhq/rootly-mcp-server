#!/usr/bin/env bash
# Logs this Semaphore job in to AWS with its OIDC token.
#
#   source .semaphore/oidc-login.sh <role-arn> [session-name] [duration-seconds]
#
# Exchanges SEMAPHORE_OIDC_TOKEN for the role's credentials and exports them
# with AWS_ACCOUNT_ID and AWS_REGION (us-east-1 unless already set). Any
# failure exits and fails the job.

# Run directly, the exports would never reach the job's shell.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "ERROR: source .semaphore/oidc-login.sh; running it exports nothing" >&2
  exit 1
fi

oidc_role_arn="${1:-}"
oidc_session_name="${2:-semaphore-${SEMAPHORE_PROJECT_NAME:-ci}}"
# STS session names allow only [A-Za-z0-9_+=,.@-], up to 64 characters.
oidc_session_name="${oidc_session_name//[^A-Za-z0-9_+=,.@-]/-}"
oidc_session_name="${oidc_session_name:0:64}"
oidc_duration="${3:-3600}"
oidc_expected_account="$(cut -d: -f5 <<<"${oidc_role_arn}")"

if [[ ! "${oidc_expected_account}" =~ ^[0-9]{12}$ ]]; then
  echo "ERROR: oidc-login.sh needs a role ARN"
  exit 1
fi
if [ -z "${SEMAPHORE_OIDC_TOKEN:-}" ]; then
  echo "ERROR: no Semaphore OIDC token in this job"
  exit 1
fi

# Only the role's credentials may be used.
unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

echo "Authenticating with AWS via OIDC..."
if ! oidc_creds=$(aws sts assume-role-with-web-identity --region us-east-1 \
    --role-arn "${oidc_role_arn}" --role-session-name "${oidc_session_name}" \
    --duration-seconds "${oidc_duration}" \
    --web-identity-token "${SEMAPHORE_OIDC_TOKEN}" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text); then
  echo "ERROR: OIDC login failed"
  exit 1
fi
read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN <<<"${oidc_creds}"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset oidc_creds

# Fail rather than act in another account if the role ARN is wrong.
if ! AWS_ACCOUNT_ID=$(aws sts get-caller-identity --region us-east-1 --query Account --output text) ||
  [ "${AWS_ACCOUNT_ID}" != "${oidc_expected_account}" ]; then
  echo "ERROR: OIDC login landed in the wrong account"
  exit 1
fi
export AWS_ACCOUNT_ID
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
echo "OIDC login OK"
