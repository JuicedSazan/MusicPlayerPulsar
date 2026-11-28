"""Set up the Pulsar app on Google Cloud Run from this repository.

Cloud Run's always-free allowance (2M requests, 180k vCPU-seconds and 360k
GiB-seconds per month) runs the app with 1 GiB of memory, which is enough for the
VK login window. The script creates the Artifact Registry repository, builds the
image from this branch's Dockerfile with Cloud Build and deploys a Cloud Run
service, then prints its URL.

It is a thin wrapper around the official gcloud CLI and uses the Dockerfile
that already sits in this repository. Signing in is interactive:

    gcloud auth login
    python scripts/deploy_gcloud_run.py --project my-project-id

The token/card questions belong to Google's sign-up flow, not to this script.
"""
import argparse
import json
import shutil
import subprocess
import sys

REGION = "europe-west4"
SERVICE = "pulsar"
REPO = "pulsar-images"


def run(args, capture=False):
    print("+", " ".join(args))
    result = subprocess.run(args, text=True, capture_output=capture)
    if result.returncode != 0:
        if capture:
            sys.stderr.write((result.stdout or "")[-2000:])
            sys.stderr.write((result.stderr or "")[-2000:])
        sys.exit(result.returncode)
    return (result.stdout or "").strip() if capture else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--service", default=SERVICE)
    parser.add_argument("--memory", default="2Gi")
    parser.add_argument("--cpu", default="2")
    parser.add_argument("--timeout", default="300")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    if shutil.which("gcloud") is None:
        sys.exit("gcloud CLI не установлен: https://cloud.google.com/sdk/docs/install")

    project = args.project
    image = f"{args.region}-docker.pkg.dev/{project}/{REPO}/{args.service}:latest"

    run(["gcloud", "config", "set", "project", project])
    # Artifact Registry repository (idempotent: ignore "already exists").
    subprocess.run([
        "gcloud", "artifacts", "repositories", "create", REPO,
        "--repository-format=docker", "--location", args.region,
        "--description=Pulsar images",
    ], text=True)

    if not args.skip_build:
        run(["gcloud", "builds", "submit", "--tag", image,
             "--timeout", "3600s", "."])

    run([
        "gcloud", "run", "deploy", args.service,
        "--image", image,
        "--region", args.region,
        "--platform", "managed",
        "--allow-unauthenticated",
        "--memory", args.memory,
        "--cpu", args.cpu,
        "--timeout", args.timeout,
        "--set-env-vars", "PULSE_COOKIE_SECURE=1,PULSE_SESSION_TTL=43200",
        "--port", "10000",
    ])

    url = run([
        "gcloud", "run", "services", "describe", args.service,
        "--region", args.region, "--format", "value(status.url)",
    ], capture=True)
    print("\nСервис запущен:", url)
    print("Проверка:", url + "/health")


if __name__ == "__main__":
    main()
