# Slides to reMarkable

Turn Canvas lecture PDFs or PowerPoint files into PDFs sized for a reMarkable tablet. Convert local files offline in `notes` or `full` mode, or connect to Canvas and reMarkable cloud for automatic syncing. Circuit extraction is an optional, online mode that asks Claude Code to find and crop circuit schematics.

This repository is a template for your own deployment. The runtime sync and self-test workflows refuse to run in public repositories and do not publish output artifacts. For unattended Actions, choose **Use this template → Create new repository → Private**. Do not create a public fork. Keep any personal deployment history and logs private; sync state is deployment-specific metadata and is committed to your private copy by the workflow.

## Requirements

- Python 3.12 or newer (the pinned NumPy/SciPy versions require it; CI uses Python 3.12).
- LibreOffice for PowerPoint (`.ppt`/`.pptx`) conversion. The cloud workflow installs it. Locally, install it with your operating system's package manager; on Ubuntu, for example, use `sudo apt install libreoffice-impress`. On macOS, install LibreOffice and make `soffice` available on `PATH`.
- The `rmapi` command and a paired reMarkable account only for uploading. Use the maintained [ddvk/rmapi v0.0.35 release](https://github.com/ddvk/rmapi/releases/tag/v0.0.35) for your OS and CPU; the upstream project is archived.
- Claude Code CLI only for `circuits` mode. `notes` and `full` do not use Claude.

For local use, clone this repository, then create an environment and install the Python dependencies:

```sh
git clone https://github.com/Mimmob07/slides-to-remarkable-public.git
cd slides-to-remarkable-public
python3 -m venv .venv
source .venv/bin/activate       # Linux, macOS, WSL
python -m pip install -r requirements.txt
```

The Python packages do not install external command-line programs such as LibreOffice, `rmapi`, or Claude Code.

## Local conversion

Local conversion needs no Canvas or tablet credentials. With `.venv` active:

```sh
python slides2rm.py convert lecture.pdf --mode notes
python slides2rm.py convert lecture.pptx output.pdf --mode full
```

The supplied configuration defaults to `notes`; choose `--mode notes`, `--mode full`, or `--mode circuits`. `notes` puts each slide at the top of a portrait page with writing space below. `full` puts every slide on its own landscape page. `circuits` produces pages for detected circuit schematics, with space to work; it requires the Claude CLI. `--picks FILE` saves Claude's selected boxes on first use and reuses them if the file already exists.

## Configuration

`config.toml` is a tracked, generic example. Edit it for your institution and course. One deployment supports one course match and one tablet folder; for several courses, create separate private copies of the template.

```toml
[canvas]
base_url = "https://canvas.example.edu"
course_match = "COURSE 101"
include = '^Lectures/'
exclude = '(?i)annotated|solutions'
modules = false
timezone = "UTC"

[remarkable]
folder = "/COURSE 101"

[layout]
mode = "notes"
grid = "dots"
```

Use your Canvas institution's actual base URL. `course_match` is a case-insensitive substring matched against active course names and course codes, so choose a string specific enough to identify the intended course. `include` and `exclude` are regular expressions evaluated against `<folder or module>/<file name>`. The include must match and the exclude must not. `modules = true` additionally searches course modules; it adds API requests. When a deck appears in both Files and Modules, its Files path wins for filtering, so use `list` to see the exact path rather than assuming the module name will match. Locked files are skipped until they become available. Grid choices are `dots`, `lines`, or `none`.

## Canvas access

The recommended option is a Canvas access token if your institution enables personal tokens. In Canvas, try **Account → Settings → Approved Integrations → New Access Token**. Set it as `CANVAS_TOKEN` in your local environment, or put it in `~/.config/slides2rm/canvas_token` with access limited to your account. For Actions, add a repository secret named `CANVAS_TOKEN` under **Settings → Secrets and variables → Actions**.

Canvas documents manual tokens for testing and says multi-user applications must use institution-approved OAuth. This project is a self-run script, not a hosted multi-user authentication service. If token creation is disabled, ask your school for its approved integration method.

An optional, advanced alternative is reusing an owner's existing Canvas Student app authorization. In this project, a consenting owner used their already-signed-in Canvas Student app and an encrypted local iPhone backup; the app's existing session credentials were recovered locally and its OAuth refresh flow reused. This is unsupported, revocable, can affect the phone's login, and the backup is extremely sensitive. Prefer approved OAuth.

If you are considering inspecting your own encrypted backup, you can ask a **local AI assistant** something like:

> Does my institution permit me to inspect my own encrypted device backup locally to understand the Canvas Student app session I already authorized? Do not print, upload, or retain any credentials or backup data. Keep the backup's encryption settings intact. Stop if this would require bypassing access controls or accessing anyone else's data, and recommend the institution-approved OAuth option instead.

Do not put credentials in prompts, logs, or this README. The script accepts `CANVAS_REFRESH_TOKEN`, `CANVAS_CLIENT_ID`, and `CANVAS_CLIENT_SECRET` together for refresh-token authentication. All three are required; if set, they take precedence over `CANVAS_TOKEN`. In Actions, store each as a separate secret with those exact names.

## Pairing reMarkable

Install the suitable `rmapi` release linked above. Locally, set `RMAPI_CONFIG` to a **file path**, create its parent directory, then run `rmapi` interactively and follow its pairing link. For example:

```sh
mkdir -p "$HOME/.config/rmapi"
export RMAPI_CONFIG="$HOME/.config/rmapi/rmapi.conf"
rmapi
```

After pairing, exit the interactive client and verify access with `rmapi -ni ls / > /dev/null`. For GitHub Actions, the `RMAPI_CONFIG` secret is the complete **file contents**, not a path. Treat it as a credential; do not print or commit it.

## Optional circuit extraction

Install Claude Code using its [official setup guide](https://code.claude.com/docs/en/setup), then run `claude` locally to sign in. For unattended Actions, create a token with `claude setup-token` and store it in the `CLAUDE_CODE_OAUTH_TOKEN` secret. The workflow does not include an API-key alternative. Circuit slide images are sent to Anthropic for analysis; confirm that you have permission to submit the course material. Neither `notes` nor `full` sends slides to Claude.

## Commands

| Command | What it does |
| --- | --- |
| `python slides2rm.py convert IN [OUT] [--mode notes\|full\|circuits] [--picks FILE]` | Convert a local PDF or PowerPoint to a tablet PDF. |
| `python slides2rm.py list` | Show Canvas candidates, filter decisions, and locked files. |
| `python slides2rm.py fetch [N]` | Download the newest N matching source decks to `out/` for inspection. |
| `python slides2rm.py sync --dry-run` | Preview what a sync would send. |
| `python slides2rm.py sync --baseline` | Mark currently matching available files as already sent; it does not upload them. |
| `python slides2rm.py sync --latest` | Try sending the newest available matching deck again. |
| `python slides2rm.py sync` | Convert and upload files that are new or have changed. |
| `python slides2rm.py sync --once-a-day` | Skip Canvas if new slides were already delivered today in the configured timezone. |

Revisions upload as new documents so notes on an earlier tablet document remain available. Do not delete `state.json` if you want the script to retain its record of delivered revisions. `state.json` is ignored in ordinary local use.

## Private GitHub Actions setup

Create a private repository from the template, edit `config.toml`, add the following **repository secrets** under Settings → Secrets and variables → Actions, and enable Actions:

| Secret | When needed |
| --- | --- |
| `CANVAS_TOKEN` | Normal Canvas access. |
| `RMAPI_CONFIG` | Every cloud sync/self-test; complete pairing file contents, not its path. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Only when `[layout].mode = "circuits"`; generated with `claude setup-token`. |
| `CANVAS_REFRESH_TOKEN`, `CANVAS_CLIENT_ID`, `CANVAS_CLIENT_SECRET` | Advanced fallback instead of `CANVAS_TOKEN`; all three together. |

Open **Actions → Sync slides to reMarkable → Run workflow**. Start with `list` to inspect the real Canvas folder paths and adjust your include/exclude regexes, then `sync --dry-run`. If the current decks should count as already delivered, use `sync --baseline` (this changes state and is not a dry run). Otherwise, `sync --latest` tries the newest deck, and a normal `sync` handles remaining new or changed decks. The separate **Self-test** workflow checks tablet pairing and a synthetic conversion without contacting Canvas.

The workflow force-adds `state.json` and commits this metadata to your private repository using the GitHub Actions bot. The workflow needs repository contents write permission; set workflow permissions to read/write, and check branch protection rules if the push is rejected. Do not copy a personal access token into Actions to work around a blocked push.

No schedule is enabled by default: use the manual button or trigger it externally with `gh workflow run sync.yml --repo YOU/PRIVATE -f 'command=sync --once-a-day'`. You can add a schedule using a UTC cron expression; scheduled runs use `sync --once-a-day` as their fallback command. GitHub scheduled workflows can be delayed and are not exact timers. Adjust UTC timing for daylight saving changes. The once-a-day guard uses `canvas.timezone` and only records a delivery after a successful run dealt with new decks. Do not run `list` or `fetch` in Actions for a public repository.

For example, add this under the existing `on:` mapping in `.github/workflows/sync.yml`, keeping `workflow_dispatch` and its inputs intact:

```yaml
  schedule:
    - cron: '23 12 * * 1,3'  # Monday/Wednesday at 12:23 UTC; change for your class
```

## Development and limitations

Run local unit tests with `python -m unittest -v`; these tests do not access Canvas or reMarkable services. Inspect circuit output before relying on it: schematic detection can omit diagrams or crop them imperfectly. PowerPoint files rely on LibreOffice, and real-world `.pptx` variations may need review. No claim is made that every tablet model has been tested. Canvas or provider API/authentication changes may require updates, and access tokens can expire or be revoked.

## License

The original code in this repository is licensed under MIT. Dependencies retain their own licenses; in particular, PyMuPDF is AGPL or commercial licensed ([license details](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)), and `rmapi` is AGPL. The overall tool stack should not be treated as wholly permissively licensed.

## References

- [Canvas OAuth documentation](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [ddvk/rmapi v0.0.35 release](https://github.com/ddvk/rmapi/releases/tag/v0.0.35)
