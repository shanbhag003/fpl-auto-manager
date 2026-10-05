#!/usr/bin/env bash
# One-time AWS setup for the Deploy Lambda workflow. Paste into AWS CloudShell
# (ap-south-1). Safe to re-run. Prints the role ARN at the end.
set -euo pipefail

# GitHub sends an immutable subject (owner and repo IDs baked in), so a renamed
# or re-created repo with the same name cannot assume this role.
SUB_PREFIX="repo:shanbhag003@67545113/fpl-auto-manager@1321217431"
ROLE="github-fpl-deploy"
REGION="ap-south-1"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
PROVIDER="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"

if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER" >/dev/null 2>&1; then
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com >/dev/null
  echo "Created GitHub identity provider."
fi

cat > /tmp/trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${PROVIDER}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {"StringEquals": {
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": "${SUB_PREFIX}:environment:production"
    }}
  }]
}
EOF

cat > /tmp/perms.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode"],
    "Resource": [
      "arn:aws:lambda:${REGION}:${ACCOUNT}:function:fpl-auto-manager",
      "arn:aws:lambda:${REGION}:${ACCOUNT}:function:fpl-results",
      "arn:aws:lambda:${REGION}:${ACCOUNT}:function:fpl-proxy"
    ]
  }]
}
EOF

if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document file:///tmp/trust.json
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document file:///tmp/trust.json >/dev/null
  echo "Created role $ROLE."
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name update-fpl-lambda-code \
  --policy-document file:///tmp/perms.json

echo
echo "Role ARN (give this to GitHub):"
aws iam get-role --role-name "$ROLE" --query Role.Arn --output text
