# Phase 1 — ChatGPT MCP file-transfer probe

Goal: find out, with server-side evidence, what ChatGPT really does with files over MCP before
building a file store around assumptions. The probe (`probe_server.py`) is a standalone MCP server
with synthetic data only. It never opens the Nexus database and runs beside the existing services,
not in place of them.

## Status

| Check | Where | Result |
|---|---|---|
| Server side of all paths (12 checks) | local, real MCP client (`selftest.py`) | **PASS 12/12** (`evidence/selftest.json`) |
| Probe image is legible (control) | `evidence/sample.png` read by Claude | **PASS** (code and colour read correctly) |
| E1–E5 below | ChatGPT | **NOT RUN** (needs the ChatGPT side) |

## Running it

Lowest-impact route: run on the VPS as `ubuntu`, loopback only, with a temporary Cloudflare quick
tunnel. No systemd unit, database or existing tunnel is touched; stopping the two processes undoes it.

```sh
# on the VPS, once
python3 -m venv ~/file-probe/venv && ~/file-probe/venv/bin/pip install "mcp==2.3.0" pillow pymupdf
# copy experiments/file_probe/ to ~/file-probe/src, then:
~/file-probe/venv/bin/python ~/file-probe/src/probe_server.py --port 8790 \
    --evidence-dir ~/file-probe/evidence --public-base https://<quick-tunnel-host>
cloudflared tunnel --url http://127.0.0.1:8790     # prints https://<random>.trycloudflare.com
```

In ChatGPT: Settings → Apps & Connectors → Advanced → Developer mode, then create a connector with
URL `https://<random>.trycloudflare.com/mcp` and no authentication. Start a **new chat** and enable it.

Caveat: this is a public-URL connector, not the OpenAI Secure MCP Tunnel that `nexus-e2e` uses. If
E2 gives a different result from what production would see, repeat it through the e2e tunnel.

## Experiments (each in a new chat; copy the prompt as is)

| # | Prompt | Evidence that counts |
|---|---|---|
| E1 image block | `Call probe_image with variant image_content. Tell me the exact code and circle colour, then call probe_check_answer with them.` | `probe_check_answer` record with `code_correct: true` |
| E2 embedded blob | same with `embedded_resource` | same |
| E3 link | same with `resource_link`, then with `text_url` | same, plus whether a `GET image` record (ChatGPT fetched the URL) appears |
| E4 upload | Attach a PDF and a PNG, then: `Send the attached file to probe_receive_file.` | `probe_receive_file` record: `arg_shape` (what ChatGPT passed) and `sha256` equal to the local file's hash |
| E5 fallback | Attach a ~2 KB text file: `Send it with probe_receive_base64, with its SHA-256.` Repeat with a ~200 KB file | `sha256_match`, and the size at which it fails |

Write down what ChatGPT says in the chat as well: "I can't see images" vs a wrong code tells two
different stories. A correct code read with no tool call doesn't count.

## After the run

```sh
scp your-vps:~/file-probe/evidence/calls.jsonl experiments/file_probe/evidence/chatgpt-calls.jsonl
```

`calls.jsonl` holds the argument shape (keys, lengths, URL host) and hashes, not file contents or
signed URL query strings. `evidence/received/` holds the files themselves: never commit it. Then
stop both processes, and remove the connector in ChatGPT.

## How the results decide Phase 2/3

- **E1 or E2 pass** → `nexus_file_page` returns a rendered page as an image block; vision over PDF
  pages works without new infrastructure.
- **Only E3 passes** → return a short-lived signed URL; needs a public, authenticated download route.
- **E1–E3 all fail** → no image path into the model; return extracted text plus page renders for
  humans only, and say so in the docs.
- **E4 passes** → `nexus_file_put(file)` takes ChatGPT attachments directly (download, hash, store).
- **E4 fails, E5 passes** → base64 intake for small files only, plus a CLI / HTTP upload for large ones.
