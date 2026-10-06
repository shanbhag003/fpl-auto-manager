# Deploying the site and the squad-lookup proxy

The site is `index.html` plus `data/`, served by GitHub Pages straight from the
root of `main`. There's no build step: merging to `main` publishes it within a
minute or two.

Bot and results code reach Lambda through the **Deploy Lambda** workflow
([LAMBDA_DEPLOY.md](LAMBDA_DEPLOY.md)). The lookup proxy uses the same workflow.
It needs creating once in the console first.

---

## The squad-lookup proxy (`fpl-proxy`)

FPL's API sends no CORS header, so the browser can't read it from github.io.
`proxy_lambda.py` forwards exactly two public paths, `/entry/{id}/` and
`/entry/{id}/event/{gw}/picks/`, and rejects everything else. It uses no
credentials, no secrets and no layers.

### 1. Create the function (once, ~5 minutes)

AWS console → **Lambda** → region **ap-south-1** → **Create function**:

| Setting | Value |
|---|---|
| Name | `fpl-proxy` |
| Runtime | Python 3.12 |
| Architecture | either |
| Permissions | the default new role (it only needs to write its own logs) |

Then on the function:

1. **Configuration → General configuration → Edit:** timeout **10 seconds**.
2. **Concurrency: leave it unreserved.** New AWS accounts can run only 10
   functions at once account-wide, and Lambda refuses any reservation that
   leaves fewer than 100 unreserved. The account limit already caps what a
   public URL can cost. To stop a burst of lookups ever crowding out the bot at
   a deadline, request a raise (free, usually approved within a day): **Service
   Quotas → AWS Lambda → Concurrent executions → 1000**, then reserve **5** for
   `fpl-proxy` here.
3. **Configuration → Function URL → Create function URL:**
   - Auth type: **NONE**
   - Expand **Additional settings → Configure cross-origin resource sharing (CORS)**, tick it, and set:
     - Allow origin: `https://shanbhag003.github.io`
     - Allow methods: `GET`
     - Max age: `300`
   - Save, and copy the **Function URL** (`https://….lambda-url.ap-south-1.on.aws/`).

Leave the code as Lambda's "Hello from Lambda!" placeholder. The workflow
recognises it and replaces it; anything else edited in the console is refused.

### 2. Let the deploy role update it (once)

In **CloudShell** (ap-south-1), paste:

```
aws iam put-role-policy --role-name github-fpl-deploy --policy-name update-fpl-lambda-code --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"lambda:GetFunction\",\"lambda:GetFunctionConfiguration\",\"lambda:UpdateFunctionCode\"],\"Resource\":[\"arn:aws:lambda:ap-south-1:$(aws sts get-caller-identity --query Account --output text):function:fpl-auto-manager\",\"arn:aws:lambda:ap-south-1:$(aws sts get-caller-identity --query Account --output text):function:fpl-results\",\"arn:aws:lambda:ap-south-1:$(aws sts get-caller-identity --query Account --output text):function:fpl-proxy\"]}]}"
```

It replaces the role's code-update policy with the same one plus `fpl-proxy`.
Nothing is printed on success. ([`aws_setup.sh`](.github/scripts/aws_setup.sh)
does the same for a fresh setup.)

### 3. Deploy the code

GitHub → **Actions → Deploy Lambda → Run workflow**, function **fpl-proxy**.
After that, any change to `proxy_lambda.py` merged to `main` deploys itself.

Check it in a browser:
`https://….lambda-url.ap-south-1.on.aws/entry/2673853/` should show JSON, and
`…/bootstrap-static/` should show `{"error": "bad_path", …}`.

### 4. Switch the lookup on

In `index.html`, set the URL without a trailing slash:

```js
const PROXY_BASE = 'https://….lambda-url.ap-south-1.on.aws';
```

Merge to `main`. The site gains two tabs, **The bot** and **Your team**, and
team lookups get their own address (`#team/1234567`). While `PROXY_BASE` is
empty there are no tabs and the page is the bot's alone, exactly as before.

### If it stops working

| Symptom on the site | Likely cause |
|---|---|
| "Couldn't reach the lookup service" | Function URL deleted or CORS origin changed |
| "returned an error (502)" | FPL blocked or changed something; check the function's CloudWatch logs |
| "FPL is updating its data" | FPL's own maintenance window, typically right after a deadline |
| "The player list hasn't been published yet" | `fpl-results` hasn't written `data/players.json` yet; it runs every 6h |
