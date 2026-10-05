"""Push the bot's Python files to AWS Lambda.

Run by .github/workflows/deploy-lambda.yml. For each function it:

  1. downloads the package currently deployed,
  2. refuses to continue if the deployed handler file is not a version that
     exists anywhere in git history — that means someone edited it in the
     console, and overwriting would silently throw that work away,
  3. swaps in the new handler file and leaves everything else in the zip
     alone. Layers and environment variables are never touched: only
     UpdateFunctionCode is called.
"""
import hashlib
import io
import os
import py_compile
import subprocess
import sys
import urllib.request
import zipfile

import boto3
from botocore.exceptions import ClientError

# Lambda function name -> source file in this repo
FUNCTIONS = {
    'fpl-auto-manager': 'fpl_bot_hybrid.py',
    'fpl-results': 'fpl_results.py',
    'fpl-proxy': 'proxy_lambda.py',
}
# Functions that may not exist yet. Until one is created in the console (and
# the deploy role allowed to update it), its deploy is skipped with a notice
# rather than failing the whole run. See DEPLOY.md.
OPTIONAL = {'fpl-proxy'}
ZERO_SHA = '0' * 40


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True).stdout


def normalise(data):
    return data.replace(b'\r\n', b'\n').rstrip() + b'\n'


def known_versions(path):
    """Every version of `path` that has ever been committed, normalised."""
    shas = git('log', '--format=%H', '--', path).decode().split()
    versions = set()
    for sha in shas:
        try:
            versions.add(normalise(git('show', f'{sha}:{path}')))
        except subprocess.CalledProcessError:
            pass    # the commit that deleted it
    return versions


def targets():
    event = os.environ.get('EVENT')
    if event == 'workflow_dispatch':
        choice = os.environ.get('CHOICE') or 'all'
        return list(FUNCTIONS) if choice in ('all', 'both') else [choice]

    before = os.environ.get('BEFORE') or ZERO_SHA
    if before == ZERO_SHA:
        return list(FUNCTIONS)
    changed = set(git('diff', '--name-only', before, 'HEAD').decode().split())
    return [fn for fn, path in FUNCTIONS.items() if path in changed]


def deploy(client, name, path, force):
    if not os.path.exists(path):
        print(f"::notice::{name}: {path} is not in the repo yet, skipping.")
        return

    py_compile.compile(path, doraise=True)
    source = open(path, 'rb').read()

    try:
        fn = client.get_function(FunctionName=name)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code')
        if name in OPTIONAL and code in ('ResourceNotFoundException', 'AccessDeniedException'):
            print(f"::notice::{name} isn't set up yet ({code}) — skipped. See DEPLOY.md.")
            return
        raise
    handler = fn['Configuration']['Handler']            # e.g. lambda_function.lambda_handler
    module, func = handler.rsplit('.', 1)
    entry = module.replace('.', '/') + '.py'
    if f'def {func}(' not in source.decode('utf-8'):
        sys.exit(f"{name}: handler is '{handler}' but {path} has no def {func}(). Not deploying.")

    with urllib.request.urlopen(fn['Code']['Location']) as r:
        current_zip = r.read()
    with zipfile.ZipFile(io.BytesIO(current_zip)) as z:
        entries = {i.filename: z.read(i.filename) for i in z.infolist() if not i.is_dir()}

    deployed = entries.get(entry)
    if deployed is not None and normalise(deployed) == normalise(source):
        print(f"{name}: already running this version of {path}.")
        return

    if deployed is not None and normalise(deployed) not in known_versions(path):
        msg = (f"{name}: the deployed {entry} doesn't match any version of {path} in git, "
               f"so it was probably edited in the AWS console. Run the 'Lambda snapshot' "
               f"workflow and bring those changes into the repo first.")
        if not force:
            sys.exit(msg)
        print(f"::warning::{msg} Overwriting anyway because force was set.")

    entries[entry] = source
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for filename, data in entries.items():
            z.writestr(filename, data)

    client.update_function_code(FunctionName=name, ZipFile=buf.getvalue())
    client.get_waiter('function_updated').wait(FunctionName=name)
    print(f"{name}: deployed {path} as {entry} "
          f"(sha256 {hashlib.sha256(source).hexdigest()[:12]}).")


def main():
    force = os.environ.get('FORCE') == 'true'
    todo = targets()
    if not todo:
        print("Nothing to deploy.")
        return
    client = boto3.client('lambda')
    for name in todo:
        deploy(client, name, FUNCTIONS[name], force)


if __name__ == '__main__':
    main()
