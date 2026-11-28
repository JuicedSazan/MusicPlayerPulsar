#!/usr/bin/env python3
"""Create a Hugging Face Space from this repository (or check an existing one).

Usage (from the repository root):

    python scripts/deploy_hf_space.py --token hf_xxx --space user/pulsar
    python scripts/deploy_hf_space.py --token hf_xxx --space user/pulsar --check

The script only uses the standard library: it creates the Space with the Docker
SDK, reads the Space repository URL and then uploads this branch's files through
Hugging Face's git server. The token needs the "write" role; it is read from
--token or the HF_TOKEN environment variable and is never written to disk.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

API = "https://huggingface.co/api"
SKIP_DIRS = {".git", "tests", "__pycache__", "logs", ".venv", "vendor-source"}
SKIP_FILES = {"run_local.bat"}


def request(url, token, method="GET", payload=None):
    data = None
    headers = {"Authorization": "Bearer " + token}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body[:400]}
        return exc.code, parsed


def ensure_space(space, token):
    status, data = request(API + "/repos/" + space, token)
    if status == 200:
        print("space exists:", data.get("id"))
        return
    if status not in (401, 404):
        print("unexpected response while checking the space:", status, data)
    payload = {"name": space.split("/")[-1], "type": "space", "sdk": "docker", "private": False}
    status, data = request(API + "/repos/create", token, "POST", payload)
    if status in (200, 201):
        print("space created:", data.get("url") or space)
    elif status == 409:
        print("space already exists:", space)
    else:
        print("could not create the space:", status, data)
        sys.exit(1)


def space_url(space, token):
    status, data = request(API + "/spaces/" + space, token)
    if status == 200:
        siblings = data.get("siblings")
        url = data.get("url") or ("https://huggingface.co/spaces/" + space)
        print("space page:", url)
        print("hosting url: https://" + space.split("/")[0].lower() + "-" + space.split("/")[-1].lower() + ".hf.space")
        return url
    print("space lookup failed:", status, data)
    return None


def push_files(space, token):
    workdir = tempfile.mkdtemp(prefix="hf-space-")
    remote = "https://user:" + token + "@huggingface.co/spaces/" + space
    subprocess.run(["git", "clone", remote, workdir], check=True)
    subprocess.run(["git", "-C", workdir, "checkout", "-B", "main"], check=True)
    copied = 0
    for root, dirs, files in os.walk("."):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            if name in SKIP_FILES:
                continue
            source = os.path.join(root, name)
            relative = os.path.relpath(source, ".")
            if relative.startswith(".github"):
                continue
            target = os.path.join(workdir, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(source, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
            copied += 1
    print("files copied:", copied)
    subprocess.run(["git", "-C", workdir, "add", "-A"], check=True)
    subprocess.run(["git", "-C", workdir, "-c", "user.name=Pulsar Deploy",
                    "-c", "user.email=deploy@pulsar.local",
                    "commit", "-m", "Deploy Pulsar from the vk-easy-login branch"], check=True)
    subprocess.run(["git", "-C", workdir, "push", "origin", "main"], check=True)
    print("pushed to", space)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=os.environ.get("HF_TOKEN", ""))
    parser.add_argument("--space", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.token:
        print("missing token: pass --token or set HF_TOKEN")
        sys.exit(2)
    if args.check:
        space_url(args.space, args.token)
        return
    ensure_space(args.space, args.token)
    push_files(args.space, args.token)
    space_url(args.space, args.token)


if __name__ == "__main__":
    main()
