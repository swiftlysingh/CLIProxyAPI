#!/usr/bin/env python3
"""Exercise a CLIProxy binary and the real Claude CLI using fake local accounts."""
import argparse
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


def request(url, payload=None):
    headers = {"Authorization": "Bearer cooldown-e2e-client", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers), response.read().decode()


def run(args):
    artifact = Path(args.output).resolve()
    artifact.mkdir(parents=True, exist_ok=True)
    report = {"binary": str(Path(args.binary).resolve()), "expect_reset": args.expect_reset, "stream": args.stream, "count": args.count}
    now = int(time.time())
    resets = {"cooldown-e2e-early": now + args.early_seconds, "cooldown-e2e-late": now + 7200}
    calls = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            auth = self.headers.get("Authorization", "").removeprefix("Bearer ") or self.headers.get("x-api-key", "")
            if auth not in resets:
                self.send_error(401)
                return
            calls.append({"account": auth.removeprefix("cooldown-e2e-"), "path": self.path, "model": json.loads(raw).get("model")})
            if auth.endswith("late") and args.late_delay:
                time.sleep(args.late_delay)
            body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "This request would exceed your account's rate limit. Please try again later."}}).encode()
            self.send_response(429)
            for key, value in {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Retry-After": str(resets[auth] - int(time.time())),
                "anthropic-ratelimit-unified-status": "rejected",
                "anthropic-ratelimit-unified-reset": str(resets[auth]),
                "anthropic-ratelimit-unified-representative-claim": "five_hour",
            }.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="cliproxy-cooldown-e2e-") as directory:
        root = Path(directory)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            proxy_port = available.getsockname()[1]
        config = {
            "server": {"host": "127.0.0.1", "port": proxy_port},
            "management": {"disable-control-panel": True},
            "access": {"api-keys": ["cooldown-e2e-client"]},
            "oauth": {"auth-dir": str(root / "auths")},
            "routing": {"strategy": "fill-first", "retry": {"request-retry": 0, "max-retry-interval": 0}},
            "requests": {"passthrough-headers": args.passthrough},
            "api-keys": {"claude": [{
                "name": "cooldown-e2e",
                "base-url": f"http://127.0.0.1:{upstream.server_port}",
                "models": [{"name": "claude-opus-5-5"}],
                "keys": [{"api-key": key, "priority": 10 if key.endswith("early") else 0} for key in resets],
            }]},
        }
        config_path = root / "config.yaml"
        config_path.write_text(json.dumps(config))
        log = (artifact / "proxy.log").open("w")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PGSTORE_", "GITSTORE_", "OBJECTSTORE_", "HOME_", "CLIPROXY_"))}
        proxy = subprocess.Popen([args.binary, "--config", str(config_path), "--local-model"], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{proxy_port}"

        def run_claude(label):
            claude_env = dict(os.environ)
            claude_env.update({
                "ANTHROPIC_BASE_URL": base,
                "ANTHROPIC_AUTH_TOKEN": "cooldown-e2e-client",
                "ANTHROPIC_API_KEY": "",
                "CLAUDE_CONFIG_DIR": str(root / "claude"),
                "CLAUDE_CODE_ENTRYPOINT": "t3-code",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_MAX_RETRIES": "0",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
            })
            result = subprocess.run([args.claude, "--print", "--verbose", "--output-format", "stream-json", "--include-partial-messages", "--model", "claude-opus-5-5", "Say OK"], cwd=root, env=claude_env, capture_output=True, text=True, timeout=45)
            (artifact / f"claude-{label}.stdout.jsonl").write_text(result.stdout)
            (artifact / f"claude-{label}.stderr.log").write_text(result.stderr)
            events = []
            for line in result.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            rate_events = [x for x in events if x.get("type") == "rate_limit_event"]
            error_params = [x.get("api_error_params") for x in events if x.get("api_error_params")]
            return {"label": label, "returncode": result.returncode, "rate_limit_events": rate_events, "api_error_params": error_params}

        try:
            started = threading.Event()
            for _ in range(150):
                if proxy.poll() is not None:
                    raise RuntimeError("Proxy exited during startup; inspect proxy.log")
                try:
                    if request(base + "/v1/models")[0] == 200:
                        break
                except (OSError, urllib.error.URLError):
                    started.wait(0.1)
            else:
                raise RuntimeError("Proxy startup failed")
            claude_results = []
            if args.claude and args.claude_first:
                claude_results.append(run_claude("initial-exhaustion"))
            payload = {"model": "claude-opus-5-5", "max_tokens": 16, "messages": [{"role": "user", "content": "Say OK"}], "stream": args.stream}
            responses = []
            labels = ["initial_exhaustion"] if args.expect_expired else ["initial_exhaustion", "already_cooling"]
            for label in labels:
                endpoint = "/v1/messages/count_tokens" if args.count and label == "already_cooling" else "/v1/messages"
                status, headers, body = request(base + endpoint, payload)
                headers = {k.lower(): v for k, v in headers.items()}
                selected = {k: v for k, v in headers.items() if k == "retry-after" or k.startswith("anthropic-ratelimit")}
                responses.append({"label": label, "status": status, "headers": selected, "body": body})
                if status != 429:
                    raise AssertionError(f"{label}: expected 429, got {status}: {body}")
            report["responses"] = responses
            report["upstream_calls"] = calls.copy()
            report["raw_resets"] = {k.removeprefix("cooldown-e2e-"): v for k, v in resets.items()}
            assert len(calls) == 2, f"Expected one attempt per account and no upstream retry while cooling, got {calls}"
            if args.expect_reset:
                for response in responses:
                    deadline = int(response["headers"].get("anthropic-ratelimit-unified-reset", "0"))
                    assert resets["cooldown-e2e-early"] <= deadline <= resets["cooldown-e2e-early"] + 32, f"Wrong pool deadline: {response}"
                assert responses[0]["headers"]["anthropic-ratelimit-unified-reset"] == responses[1]["headers"]["anthropic-ratelimit-unified-reset"], "Initial and subsequent cooldown deadlines differ"
            if args.expect_expired:
                assert "anthropic-ratelimit-unified-reset" not in responses[0]["headers"], "Recovered pool leaked a stale upstream reset"
                assert "retry-after" not in responses[0]["headers"], "Recovered pool leaked a stale Retry-After"
            if args.claude:
                claude_results.append(run_claude("already-cooling"))
                report["claude"] = claude_results
                for result in claude_results:
                    if args.expect_reset:
                        observed = [x.get("rate_limit_info", {}).get("resetsAt") for x in result["rate_limit_events"]]
                        observed += [x.get("rate_limit_info", {}).get("resetsAt") for x in result["api_error_params"]]
                        expected = int(responses[1]["headers"]["anthropic-ratelimit-unified-reset"])
                        assert expected in observed, f"Claude did not emit expected reset {expected} for {result['label']}; observed {observed}"
            report["passed"] = True
        except Exception as error:
            report["passed"] = False
            report["error"] = str(error)
            report["upstream_calls"] = calls.copy()
            raise
        finally:
            proxy.terminate()
            try:
                proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
            log.close()
            upstream.shutdown()
            (artifact / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expect-reset", action="store_true")
    parser.add_argument("--passthrough", action="store_true")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--count", action="store_true")
    parser.add_argument("--claude")
    parser.add_argument("--claude-first", action="store_true")
    parser.add_argument("--early-seconds", type=int, default=3600)
    parser.add_argument("--late-delay", type=float, default=0)
    parser.add_argument("--expect-expired", action="store_true")
    run(parser.parse_args())
