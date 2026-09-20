"""Start a production container and verify health/auth without cloud credentials."""
import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("api", "web"))
    parser.add_argument("image")
    parser.add_argument("--frontend-only", action="store_true")
    args = parser.parse_args()
    if args.frontend_only and args.service != "web":
        parser.error("--frontend-only applies to the web image")
    container = subprocess.check_output([
        "docker", "run", "-d", "--rm", "-p", "127.0.0.1::8080",
        "-e", "PORT=8080", "-e", "YTFACTORY_QUEUE_BACKEND=memory",
        "-e", "YTFACTORY_REQUIRE_AUTH=1", "-e", "YT_AUTH_ENABLED=1",
        "-e", "OTEL_EXPORTER=inmemory",
        "-e", f"YTFACTORY_FRONTEND_ONLY={int(args.frontend_only)}", args.image,
    ], text=True).strip()
    opener = urllib.request.build_opener(NoRedirect())

    def get(path):
        request = urllib.request.Request(base + path, headers={"Accept": "application/json"})
        try:
            response = opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    try:
        info = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
        port = info["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        health = "/healthz" if args.service == "api" else "/login"
        for attempt in range(60):
            try:
                status, _, _ = get(health)
                if status == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Container did not become healthy")
        if args.service == "api":
            assert get("/api/jobs")[0] == 401, "Anonymous API access must be blocked"
        else:
            status, _, body = get("/")
            assert status == 200 and body.lower().startswith(b"<!doctype html>"), "Invalid homepage HTML"
            status, headers, _ = get("/app")
            assert status in (302, 307) and "/login" in headers.get("Location", ""), "Studio must require login"
            if args.frontend_only:
                assert b"The website is online" in get("/login")[2]
                assert get("/api/jobs")[0] == 503
                assert get("/agent/status")[0] == 503
                status, _, body = get("/healthz")
                assert status == 200 and json.loads(body)["mode"] == "frontend_only"
        print(f"{args.service}: production image health and anonymous auth checks passed")
    except Exception:
        subprocess.run(["docker", "logs", "--tail", "50", container], check=False)
        raise
    finally:
        subprocess.run(["docker", "stop", container], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
