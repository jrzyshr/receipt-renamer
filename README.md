# receipt-renamer

Batch-scan a stack of paper receipts, then let a vision LLM give every file a name a human
can actually read:

```
scan_0001.jpg  →  2024-03-14 Blue Bottle Coffee - Meals.jpg
scan_0002.jpg  →  2024-03-15 Shell - Fuel.jpg
scan_0003.jpg  →  Needs Review/scan_0003.jpg     (unreadable — never guessed)
```

Every run is **dry by default**, every applied run is written to an append-only ledger, and
`receipt-renamer undo` puts everything back.

Prefer the business name up front? Pass `--name-format business-first` and the same batch
becomes `Blue Bottle Coffee - Meals - 03-14-2024.jpg`. See
[Filename format](#filename-format).

---

## Install

Requires Python 3.10+.

```bash
git clone <your-repo-url> receipt-renamer
cd receipt-renamer

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# pick the provider(s) you use - extras are additive
pip install -e ".[openai]"         # public OpenAI
pip install -e ".[azure]"          # Azure OpenAI
pip install -e ".[anthropic]"      # Claude
```

Optional extras:

| Extra | Gives you |
| --- | --- |
| `openai` | OpenAI provider (`gpt-4o-mini`, `gpt-4o`) |
| `azure` | Azure OpenAI provider (API-key auth) |
| `azure-entra` | Azure OpenAI provider + Microsoft Entra ID auth (`azure-identity`) |
| `anthropic` | Anthropic provider (Claude) |
| `heic` | iPhone `.heic` / `.heif` input (`pillow-heif`) |
| `pdf` | Single-page PDF input (`pdf2image`, needs poppler) |
| `dev` | pytest |

Verify:

```bash
receipt-renamer --version
```

## Providers and credentials

Credentials are read from the environment. Nothing is ever written to disk by this tool.
`config.load_env()` loads a `.env` from your working directory at startup (see
`.env.example`, and note that `.env` is already gitignored). **Exported variables always win
over `.env`.**

Choose a provider with `--provider` or `RECEIPT_RENAMER_PROVIDER`. Default is
`openai` / `gpt-4o-mini`.

Two variables apply to every provider:

| Variable | Meaning |
| --- | --- |
| `RECEIPT_RENAMER_PROVIDER` | `openai`, `azure`, `anthropic`, or `fake`. Same as `--provider` |
| `RECEIPT_RENAMER_MODEL` | Default model when `--model` is omitted. On Azure it's a fallback for `AZURE_OPENAI_DEPLOYMENT` |
| `RECEIPT_RENAMER_NAME_FORMAT` | `date-first` or `business-first`. Same as `--name-format` |

A `--provider` / `--model` flag always beats the environment.

### OpenAI

```bash
export OPENAI_API_KEY=sk-...
receipt-renamer run --input ~/Scans/Inbox
```

The endpoint is resolved by the SDK: `OPENAI_BASE_URL` if you set it, otherwise
`https://api.openai.com/v1`. That is enough to point at an OpenAI-compatible gateway
(LiteLLM, vLLM, Ollama). For **Azure**, use the dedicated provider below — Azure is not
wire-compatible with a plain base-URL swap.

### Azure OpenAI

Azure differs from the public API in three ways the tool handles for you:

- the endpoint is your own resource, `https://<resource>.openai.azure.com`
- **`--model` is the deployment name** you chose in the portal, not `gpt-4o-mini`
- requests are pinned to an `api-version` (default `2024-10-21`, a GA version that
  supports vision input and JSON mode)

```bash
pip install -e ".[azure]"

export AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
export AZURE_OPENAI_DEPLOYMENT=receipts-4o-mini    # your deployment name
export AZURE_OPENAI_API_KEY=...                    # or use Entra ID, below

receipt-renamer run --input ~/Scans/Inbox --provider azure
```

Or set `RECEIPT_RENAMER_PROVIDER=azure` in `.env` and drop the flag. The startup banner
echoes the resolved endpoint, deployment and auth mode so a misconfiguration is obvious
before you spend money:

```
Scanning /Users/me/Scans/Inbox with azure/receipts-4o-mini @ https://my-resource.openai.azure.com (api-key)
```

**Entra ID (keyless) auth.** Many Azure OpenAI resources have local key auth disabled, or
you simply may not have a key. Leave `AZURE_OPENAI_API_KEY` unset and the tool falls back to
`DefaultAzureCredential`, which picks up the Azure CLI, a managed identity, VS Code, or a
service principal — no key anywhere:

```bash
pip install -e ".[azure-entra]"

# Sign in FOR THIS AUDIENCE. A plain `az login` is often not enough: an existing
# session can lack a refresh token for Cognitive Services and fails with AADSTS9002313.
az login --scope "https://cognitiveservices.azure.com/.default"

receipt-renamer run --input ~/Scans/Inbox --provider azure
```

Your identity needs the **Cognitive Services OpenAI User** role on the resource (Owner or
Contributor on the subscription does *not* imply data-plane access).

The token is acquired **eagerly at startup**, so a credential problem fails immediately with
a fix-it message rather than part-way through a batch. In CI or on a VM, set the standard
`AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_CLIENT_SECRET` service-principal variables, or
rely on the attached managed identity — `DefaultAzureCredential` finds either without code
changes.

| Variable | Required | Notes |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | yes | The resource root. A full request URL, a `/openai` path, or the newer `/openai/v1` form are all accepted and trimmed back automatically |
| `AZURE_OPENAI_DEPLOYMENT` | yes | Or pass `--model <deployment>` |
| `AZURE_OPENAI_API_KEY` | no | Omit to authenticate with Entra ID |
| `AZURE_OPENAI_API_VERSION` | no | Defaults to `2024-10-21` |

Deploy a **vision-capable** model (`gpt-4o-mini` or `gpt-4o`); a text-only deployment will
fail on the image content block.

### Anthropic

```bash
pip install -e ".[anthropic]"
export ANTHROPIC_API_KEY=sk-ant-...
receipt-renamer run --input ~/Scans/Inbox --provider anthropic
```

`ANTHROPIC_BASE_URL` is honoured by that SDK in the same way.

## Scanner workflow tips

- **300 DPI, grayscale.** Higher DPI just costs you scan time; the tool downscales to 2000 px
  on the long edge before upload anyway. Colour rarely helps thermal receipt paper.
- **One receipt per page.** The model extracts a single receipt per image. Tape small receipts
  to a sheet only if you are happy with one filename for the lot.
- **Feed them straight.** Auto-deskew/auto-crop in your scanner software materially improves
  the read on crumpled thermal paper.
- **Scan into a staging folder**, e.g. `~/Scans/Inbox`, and let the tool move the results into
  `Renamed/`. That keeps "not yet processed" and "done" visually obvious.
- **Long faded receipts:** if `confidence` keeps landing under the threshold, try
  `--model gpt-4o` for that batch, or raise `--max-edge 2600`.

## Filename format

Two layouts are supported. Pick one with `--name-format`, or set a default once in `.env`
with `RECEIPT_RENAMER_NAME_FORMAT`. The flag always beats the environment.

| `--name-format` | Result |
| --- | --- |
| `date-first` *(default)* | `2024-03-14 Blue Bottle Coffee - Meals.jpg` |
| `business-first` | `Blue Bottle Coffee - Meals - 03-14-2024.jpg` |

```bash
receipt-renamer run --input ~/Scans/Inbox --name-format business-first --apply
```

```dotenv
# .env — applies to every run and watch
RECEIPT_RENAMER_NAME_FORMAT=business-first
```

`date-first` sorts chronologically in any file browser; `business-first` groups a vendor's
receipts together. Both use the same sanitizing and title-casing rules, and a field the
model could not read is dropped along with its separator rather than leaving an empty gap —
a receipt with no purpose becomes `Blue Bottle Coffee - 03-14-2024.jpg`.

The format only affects **new** names. `undo` replays the ledger's recorded paths, so a run
applied under one format still reverses cleanly after you change the setting.

## Commands

### `run` — process a folder once

Dry run (the default — touches nothing):

```bash
receipt-renamer run --input ~/Scans/Inbox
```

```
                       Planned renames (dry run)
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Original    ┃ → New name                         ┃ Outcome      ┃ Conf.  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━┩
│ scan_01.jpg │ 2024-03-14 Blue Bottle - Meals.jpg │ renamed      │ 0.94   │
│ scan_02.jpg │ Needs Review/scan_02.jpg           │ needs review │ …0.41  │
└─────────────┴────────────────────────────────────┴──────────────┴────────┘
```

Apply it for real:

```bash
receipt-renamer run --input ~/Scans/Inbox --apply
```

Useful options:

```bash
# rename inside the input folder instead of moving to <input>/Renamed
receipt-renamer run --input ~/Scans/Inbox --in-place --apply

# custom destination + subfolders + stricter threshold
receipt-renamer run --input ~/Scans --output ~/Receipts/2024 --recursive \
                    --min-confidence 0.85 --apply

# use Azure, Claude, or a stronger OpenAI model for a difficult batch
receipt-renamer run --input ~/Scans/Inbox --provider azure --apply
receipt-renamer run --input ~/Scans/Inbox --provider azure --model receipts-4o --apply
receipt-renamer run --input ~/Scans/Inbox --provider anthropic --apply
receipt-renamer run --input ~/Scans/Inbox --model gpt-4o --apply

# put the business name first instead of the date
receipt-renamer run --input ~/Scans/Inbox --name-format business-first --apply
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--input, -i` | *(required)* | Folder of scans |
| `--output, -o` | `<input>/Renamed` | Destination folder |
| `--apply` | off | Actually move files (otherwise dry run) |
| `--dry-run` | on | Explicitly force a dry run |
| `--in-place` | off | Rename within the input folder |
| `--recursive, -r` | off | Include subfolders |
| `--verbose, -v` | off | Print a line for every file, not just problem files |
| `--provider, -p` | `openai` | `openai`, `azure`, `anthropic`, or `fake` |
| `--model` | provider default | e.g. `gpt-4o`; for `azure` this is the **deployment name** |
| `--name-format` | `date-first` | `date-first` or `business-first` — see [Filename format](#filename-format) |
| `--min-confidence` | `0.7` | Below this → `Needs Review/` |
| `--max-edge` | `2000` | Downscale long edge (px) before upload |
| `--quality` | `85` | JPEG quality for the uploaded copy |

### What a run looks like

Receipts are processed **one at a time**, and each one is a network round-trip, so a large
batch takes a while — roughly 3-5 seconds per receipt, or 10-20 minutes for a 200-file
folder. The run reports itself as it goes so you can tell work from a hang:

```
Connected to azure/receipts-4o-mini @ https://my-res.openai.azure.com (entra-id) (dry run)
Found 227 receipt(s) in /Users/you/Scans  (1 unreadable file(s) ignored)
needs review  scan_014.jpg → Needs Review/scan_014.jpg  confidence 0.42 is below the 0.70 threshold
needs review  scan_031.jpg → Needs Review/scan_031.jpg  model could not read: date
⠹ Reading scan_052.jpg ━━━━━━━━━╸────────────  52/227 0:03:41 0:12:18
```

Three phases are reported separately: **connecting** to the model (where Entra ID sign-in
blocks, if it is going to), **finding** receipts, then **reading** them with a live count,
elapsed time, and estimated time remaining.

Files that need attention are printed the moment they happen, so you never wait until the
end to discover a problem. Successful renames stay quiet — they are all in the final table
— unless you pass `--verbose`.

**If several files fail in a row**, the run stops rather than continuing. An expired token
or a dead endpoint would otherwise file every remaining receipt under `Needs Review/`,
which looks like hundreds of unreadable scans instead of one outage:

```
Aborted: stopped after 5 consecutive provider failures: 401 PermissionDenied
```

**Ctrl-C** is safe. The run stops, prints what it finished, and still reports the run id, so
anything already moved can be reversed with `undo`.

### `watch` — process scans as they land

```bash
receipt-renamer watch --input ~/Scans/Inbox
```

Uses `watchdog` plus a size/mtime **debounce** (default 2 s, `--debounce`), so a page still
being written by the feeder is never read half-finished. Ctrl-C to stop.

Unlike `run`, `watch` **applies by default** — it exists to be left running, so pass
`--dry-run` if you just want to see decisions scroll past. It accepts the same
`--output`, `--in-place`, `--recursive`, `--provider`, `--model`, `--name-format`,
`--min-confidence`, `--max-edge` and `--quality` options as `run`, and files handled during
one watch session share a single run id, so a later `undo` reverses the whole session.

```bash
receipt-renamer watch --input ~/Scans/Inbox --provider azure --debounce 5
```

### `undo` — reverse the last run

```bash
receipt-renamer undo --input ~/Scans/Inbox --dry-run   # preview
receipt-renamer undo --input ~/Scans/Inbox             # do it
receipt-renamer undo --input ~/Scans/Inbox --run-id 20240314T101500Z-ab12cd34
receipt-renamer undo --input ~/Scans/Inbox --ledger /path/to/.receipt-renamer-log.jsonl
```

Undo reads the ledger, walks the run backwards, and refuses to overwrite anything that has
appeared at an original path in the meantime. Each file comes back as `restored`, `missing`
(it was moved or deleted outside the tool) or `blocked` (something else now occupies the
original path) — nothing is forced. Undoing the most recent run twice is a no-op: a reversed
run is marked in the ledger and skipped when resolving "last run".

## Safety model

- **Dry run by default.** `run` never moves a file without `--apply`.
- **Never guess.** The model is instructed to return `null` rather than invent a field. If the
  date, business, or purpose is null, extraction errors, or `confidence < --min-confidence`,
  the file is moved **untouched** into `Renamed/Needs Review/`.
- **Never overwrite.** Collisions get ` (2)`, ` (3)`, … appended. Dry runs reserve planned
  names too, so the preview matches reality.
- **Never drop.** Every file is either renamed, sent to Needs Review, or reported as skipped.
- **Filenames are sanitised.** `/ \ : * ? " < > |` and control characters are removed,
  whitespace collapsed, components length-capped, and business names sensibly title-cased
  (`THE HOME DEPOT #4501` → `The Home Depot #4501`, but `IBM` stays `IBM`).
- **Your receipts stay out of git.** Scans are financial records. `.gitignore` excludes
  `Renamed/`, `Needs Review/`, `scans/`, `samples/`, and anything matching `*Inbox*/` or
  `*Receipts*/`, along with `.env` and the ledger. If you keep a working folder inside this
  repo under a different name, add it to `.gitignore` before your first `git add`.

## Ledger

Applied runs append one JSON object per file to `.receipt-renamer-log.jsonl` in the input
folder:

```json
{"run_id":"20240314T101500Z-ab12cd34","action":"renamed","original_path":"/…/scan_01.jpg",
 "new_path":"/…/Renamed/2024-03-14 Blue Bottle Coffee - Meals.jpg","date":"2024-03-14",
 "business":"Blue Bottle Coffee","purpose":"Meals","confidence":0.94,"provider":"openai",
 "model":"gpt-4o-mini","reason":null,"timestamp":"2024-03-14T10:15:02+00:00","version":1}
```

That is what makes `undo` possible, and it doubles as an audit trail for expense reports.
Dry runs never write to it.

`provider` and `model` record what actually produced each result, so a mixed batch stays
traceable. On Azure, `provider` is `azure` and `model` is your **deployment name** — which is
also why re-running a batch against a different deployment is auditable after the fact.

## Supported formats

| Format | Status |
| --- | --- |
| `.jpg` `.jpeg` `.png` `.webp` `.tif` `.tiff` `.bmp` `.gif` | Processed |
| `.heic` `.heif` | Processed if `pillow-heif` installed, otherwise **skipped with a message** |
| `.pdf` | First page processed if `pdf2image` + poppler installed, otherwise **skipped** |
| anything else | Skipped with a clear reason; never touched |

A skipped file is never moved, renamed, or deleted — it just doesn't appear in the results
table as a rename. PDF support needs the poppler binaries as well as the Python package:

```bash
brew install poppler          # macOS
sudo apt install poppler-utils # Debian/Ubuntu
pip install -e ".[pdf]"
```

Note the ledger records the extracted **first page** of a PDF under the original `.pdf`
extension; multi-page PDFs are not split.

## Cost expectations

Images are downscaled to 2000 px on the long edge and re-encoded as JPEG before upload,
which is the single biggest cost lever. At that size one receipt is roughly 1–2k image
tokens plus a ~300-token prompt and a tiny JSON response.

With **gpt-4o-mini** that works out to well under a cent per receipt — on the order of
**$0.20–0.60 per 1,000 receipts** at current pricing. **gpt-4o** and **Claude Sonnet** are
roughly 15–20× that, so keep them for the batches `gpt-4o-mini` struggles with.

**Azure OpenAI** is billed per-token on the same basis, at your resource's regional rate
rather than the public list price — check the Azure pricing page (or your EA/MACC terms) for
the deployed model and region, and watch your deployment's TPM quota on large batches, since
throttling shows up as retries rather than errors.

Always sanity check against your provider's current price list; do a `--dry-run` first if the
batch is huge.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `AZURE_OPENAI_ENDPOINT is not set` | Exported in another shell, or in a `.env` that isn't in your current working directory |
| Azure `404 DeploymentNotFound` | `--model` / `AZURE_OPENAI_DEPLOYMENT` must be the **deployment name** from the portal, not the model name |
| Azure `401` / `PermissionDenied` | Wrong key for the resource, or with Entra ID you're missing the *Cognitive Services OpenAI User* role |
| Every file fails with `404 Resource not found` | The request URL is wrong, not your scans. Usually the **deployment name** (`--model` / `AZURE_OPENAI_DEPLOYMENT`) — it's the name you gave the deployment in AI Studio, not the model name. Also check the api-version |
| `run` seems to hang | It is working: check the progress bar's count and ETA. 200+ receipts is normally 10-20 minutes |
| Run stops with `Aborted: ... consecutive provider failures` | An outage, not bad scans. Check auth/endpoint, then re-run — nothing was overwritten |
| `AADSTS9002313` at startup | Your CLI session has no token for this audience. Run `az login --scope "https://cognitiveservices.azure.com/.default"` |
| `Could not acquire a Microsoft Entra ID token` | Not signed in, or `azure-identity` is missing — install `.[azure-entra]` |
| Azure complains about image content | The deployment isn't a vision model — deploy `gpt-4o-mini` or `gpt-4o` |
| `Unsupported data type` / unexpected 400 | Pin a newer `AZURE_OPENAI_API_VERSION`; the default is `2024-10-21` |
| Everything lands in `Needs Review/` | Check the `reason` column. Faded thermal paper often needs `--model gpt-4o` or `--max-edge 2600` |
| Run is slow on a big batch | Files are processed serially by design. Azure throttling (TPM quota) shows up as retries, not errors |
| `No ledger found` on `undo` | `undo` reads `<input>/.receipt-renamer-log.jsonl`; point `--input` at the folder you originally ran against, or pass `--ledger` |

Everything except the network call is offline-testable, so reproduce naming or undo
behaviour with `--provider fake` before blaming the model.

## Development

```bash
pip install -e ".[dev]"
pytest          # 104 tests, no network calls
```

### Layout

```
src/receipt_renamer/
  cli.py         # typer commands: run / watch / undo, rich output
  config.py      # Settings, folder resolution, .env loading
  images.py      # format sniffing, downscale + JPEG re-encode
  extractor.py   # prep -> provider -> validate -> ReceiptData
  naming.py      # sanitize, title-case, name formats, build stem, collision suffixes
  ledger.py      # JSONL append, run lookup, undo
  processor.py   # per-file pipeline; rename vs Needs Review
  watcher.py     # watchdog handler + write-stability debounce
  providers/
    base.py             # VisionProvider protocol, prompt, JSON parsing
    _openai_common.py   # shared chat-completions request/response plumbing
    openai_provider.py  # public OpenAI
    azure_provider.py   # Azure OpenAI (deployments, api-version, Entra ID)
    anthropic_provider.py
    fake.py             # offline provider used by tests and demos
```

`naming.py`, `ledger.py` and `extractor.py`'s validation are pure functions, which is why
the suite can cover collisions, date handling and undo without touching a model.

### Adding a provider

Implement the `VisionProvider` protocol from `providers/base.py` — a `name`, a `model`, and
`extract(image_bytes, *, mime_type) -> RawExtraction` — then register it in
`providers/__init__.py`. Import the SDK **lazily inside `__init__`** so the package still
installs and tests still run without it. If it speaks the OpenAI chat-completions format,
reuse `_openai_common.chat_extract` as the Azure provider does.

### Trying it without an API key

```bash
receipt-renamer run --input ./samples --provider fake            # dry run
receipt-renamer run --input ./samples --provider fake --apply
receipt-renamer undo --input ./samples
```

`FakeProvider` returns canned extractions and records its calls, so the full
rename → ledger → undo path is exercisable offline.

## License

MIT — see [LICENSE](LICENSE).
