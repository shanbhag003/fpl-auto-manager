# Deploying to Lambda from GitHub

Code changes reach AWS without copy-pasting into the console:

```
edit fpl_bot_hybrid.py / fpl_results.py  →  push to main
  →  "Deploy Lambda" workflow waits for your approval (production environment)
  →  approve  →  code updated on fpl-auto-manager / fpl-results
```

GitHub signs in to AWS with a short-lived OIDC token, so there are no AWS keys
stored anywhere. The role it uses can only read and update the code of these two
functions — not their environment variables, layers, schedule, or anything else
in the account.

Only the handler file inside the deployed zip is replaced. If the deployed code
doesn't match any version in git (someone edited it in the console), the deploy
stops instead of overwriting it.

---

## One-time AWS setup (region ap-south-1)

**Quick way:** open AWS CloudShell in ap-south-1 and run

```
curl -sSL https://raw.githubusercontent.com/shanbhag003/fpl-auto-manager/main/.github/scripts/aws_setup.sh | bash
```

It does steps 1–2 below and prints the role ARN for step 3. The manual steps
are the same thing by hand.

### 1. Add GitHub as an identity provider

IAM → **Identity providers** → **Add provider**

| Field | Value |
|---|---|
| Provider type | OpenID Connect |
| Provider URL | `https://token.actions.githubusercontent.com` |
| Audience | `sts.amazonaws.com` |

(Skip if it already exists — you can only have one per account.)

### 2. Create the role

IAM → **Roles** → **Create role** → **Custom trust policy**, and paste this,
replacing `ACCOUNT_ID` with your 12-digit account ID (top-right menu in the console):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:shanbhag003@67545113/fpl-auto-manager@1321217431:environment:production"
      }
    }
  }]
}
```

The `sub` condition means only a job in this repo's `production` environment —
which needs your approval — can use the role.

Click **Next**, skip attaching managed policies, name it `github-fpl-deploy`, and
create it. Then open the role → **Add permissions** → **Create inline policy** →
**JSON**:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:UpdateFunctionCode"
    ],
    "Resource": [
      "arn:aws:lambda:ap-south-1:ACCOUNT_ID:function:fpl-auto-manager",
      "arn:aws:lambda:ap-south-1:ACCOUNT_ID:function:fpl-results"
    ]
  }]
}
```

Name it `update-fpl-lambda-code`.

### 3. Give GitHub the role ARN

Copy the role's ARN (`arn:aws:iam::ACCOUNT_ID:role/github-fpl-deploy`), then in
GitHub: repo → **Settings** → **Secrets and variables** → **Actions** →
**Environments** tab… or simply run:

```
gh secret set AWS_DEPLOY_ROLE_ARN --env production --repo shanbhag003/fpl-auto-manager
```

and paste the ARN when prompted. It isn't sensitive on its own, but keeping it
as a secret keeps it out of logs.

---

## Using it

- **Normal changes:** merge to main. The Deploy Lambda run appears under
  Actions with a "Review deployments" button — approve it.
- **Redeploy by hand:** Actions → Deploy Lambda → Run workflow, pick the function.
- **Someone edited code in the console:** Actions → Lambda snapshot → Run. It
  downloads the deployed code as an artifact (kept one day, scanned for
  credentials first) so the change can be committed back to the repo. The
  deploy's `force` option overwrites the console edit instead.
