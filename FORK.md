# Earliest cooldown reset fork

This branch is based on CLIProxyAPI v8.0.50, commit
`f7331b5197730226829cab32c31fb4ce546ef8fc`.

When an account pool is exhausted, the proxy reports the earliest effective
recovery time for the requested model. It preserves request eligibility, pinned
credentials, model aliases, and the original upstream error. The deadline includes
the proxy's existing cooldown buffer. Home round aggregation compares absolute
deadlines so time spent attempting another account does not postpone recovery.

Claude 429 responses expose that deadline through Anthropic's unified rate limit
headers, including when `requests.passthrough-headers` is disabled. Trusted local
timing overrides an individual account's upstream headers. Expired pool deadlines
suppress stale upstream reset headers.

Claude Code 2.1.318 emits subscription reset events only in OAuth mode. For a local
proxy, pass the proxy's client key as `CLAUDE_CODE_OAUTH_TOKEN`, keep
`ANTHROPIC_BASE_URL` pointed at the proxy, and remove `ANTHROPIC_AUTH_TOKEN` from
that provider's environment. The same local key still authenticates to CLIProxy;
this does not replace or expose an upstream account credential. Bearer-token mode
does not emit reset events even when the response has valid reset headers.

The proxy omits a representative claim because a pool deadline does not describe
one specific five-hour or seven-day quota window.

## Verification

Build and run the existing suite:

```sh
go build -o cli-proxy-api ./cmd/server
go test ./...
```

The standalone E2E harness uses fake local accounts and temporary configuration.
It does not read provider credentials or send requests to Anthropic. It checks
the request that exhausts the pool and the subsequent cooldown response, with
the earlier account deliberately attempted first.

```sh
python3 test/e2e/claude_cooldown.py --binary ./cli-proxy-api \
  --output /tmp/cliproxy-cooldown-check --expect-reset
```

Add `--stream`, `--passthrough`, or `--count` to cover streaming bootstrap,
header passthrough, or token counting after pool exhaustion. Add
`--claude /opt/homebrew/bin/claude --claude-first` to verify the installed Claude
CLI emits the timestamp on both initial exhaustion and subsequent cooldown.
The harness saves JSON reports, proxy logs, and Claude JSONL output.

To verify an account recovering during slow failover does not advertise another
account's later reset:

```sh
python3 test/e2e/claude_cooldown.py --binary ./cli-proxy-api \
  --output /tmp/cliproxy-cooldown-expired --early-seconds 1 \
  --late-delay 33 --passthrough --expect-expired
```

## Local installation and updates

The custom macOS build must retain the Homebrew configuration default:

```sh
go build -ldflags "-X main.DefaultConfigPath=/opt/homebrew/etc/cliproxyapi.conf" \
  -o cli-proxy-api ./cmd/server
```

The installed custom binary uses the existing Homebrew service. The formula is
pinned so `brew upgrade` does not silently replace the fix. Before replacement,
the original binary, configuration, service definition, and rollback script are
saved under `~/.local/share/cliproxyapi-fork/backups/`.

Update this branch from upstream, rerun verification, and rebuild before replacing
the custom binary. Once an upstream release includes the fix, restore the stock
binary using the saved rollback script, then run `brew upgrade cliproxyapi`.
