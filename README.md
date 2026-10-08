# pdf-remediator

A PDF accessibility remediation assistant. It tags the PDF with Adobe's AutoTag
API if needed, then does the repetitive Acrobat work automatically. Only the
decisions that need a human are left for you.

```
python remediate.py report.pdf
```

produces

| File | What it is |
|---|---|
| `report_accessible.pdf` | the fixed PDF |
| `report_accessible_report.html` | what was fixed, what needs review, what still fails |
| `report_accessible_review.json` | the decisions to confirm (alt text, heading levels, title...) |
| `report_accessible_report.json` | the same report, machine-readable |

## Easiest way: the app

Double-click **PDF Remediator** on the desktop (or run `python remediate.py app`).
It opens in your browser:

1. AutoTag the PDF in Acrobat and save it.
2. Drag it onto the page, or onto the desktop icon.
3. Review the cards. Each figure shows a preview next to its alt text. For each card, choose
   Accept, Reject or Skip, or edit the text.
4. Click **Apply my decisions**, then **Download PDF**.
5. Finish with Preflight (embed fonts) and PAC.

Everything runs on this computer. The app listens only on `127.0.0.1`, and jobs
are kept in `Documents\PDF Remediator`. The only thing that leaves the machine is
the Claude calls (untick "Use Claude" for confidential files). If you have
problems, check `Documents\PDF Remediatorpp.log`.

To recreate the desktop shortcut: target `pythonw.exe -m remediator.app`, and set
"Start in" to the project folder.

## Use it from any device

The same app can run as a private, password-protected website. See **[DEPLOY.md](DEPLOY.md)** (Railway, about $5/month).

## Your workflow → what the tool does

| Manual step | Tool |
|---|---|
| AutoTag | Untagged PDFs go through **Adobe's AutoTag API** (the same engine as Acrobat). `--autotag` re-tags PDFs that already have tags |
| Fix heading structure | Keeps a valid outline and repairs broken ones: all-H1 outlines are re-ranked by font size, publisher tags like `HEAD_1`/`CHAP_TITLE` that are mapped to `<P>` become real headings, masthead headings above the title become text, and skipped levels are removed. Claude reviews the outline using numbering, wording and your past corrections; missed headings are suggested for you to confirm (set `auto_promote_when_ai_agrees = true` to tag them automatically) |
| Accessibility checker | `check` runs the equivalent rules locally, plus a ParentTree integrity check |
| Figure alt text | Claude writes alt text from a render of the figure and its surrounding text. Tiny/decorative figures and empty Figure tags are flagged |
| Link alt text / "link element missing" | Fills link `Contents` from the URL or destination page. Builds `<Link>` tags (link text + OBJR) for annotations outside the tag tree |
| TH / TD / Scope / regularity | Promotes the first row to TH if the table has no headers, sets Scope, flags irregular tables and tables with no TD cells |
| Table summary | Generated from the size and header text |
| Bookmarks | Built from the corrected headings (H1 to H3, nested), each one jumping to its heading. Existing bookmarks are kept |
| Preflight: embed fonts | Missing fonts (Times, Helvetica, Courier, Arial, Times New Roman) are embedded from your Windows fonts, subset to the characters used, with matching widths and a ToUnicode map. AutoTag's invisible Times-Roman spaces fail this on nearly every file |
| Show title | Sets the title (from the first H1 if it's missing or is a file name), and sets DisplayDocTitle |
| Preflight fixups | Language, MarkInfo, PDF/UA id, tab order, wrapping a lone Document root, removing empty tags, LI/LBody repair, marking untagged lines/shapes as Artifacts |
| PAC 3 | **Still PAC** as the final check. The report tells you what will fail before you open it |

## Setup

```
pip install -r requirements.txt
python remediate.py setup          # prompts for keys (input hidden)
python remediate.py setup --status # shows what's configured
```

Keys are saved to `%APPDATA%\pdf-remediator\credentials.json`, never in the
project folder. Environment variables override the saved file:
`ANTHROPIC_API_KEY`, `PDF_SERVICES_CLIENT_ID`, `PDF_SERVICES_CLIENT_SECRET`.

**Claude API key** (AI alt text): console.anthropic.com → API Keys → Create key.
Add credit under Billing. Each figure is one request.

**Adobe PDF Services credentials** (AutoTag):
1. Go to the Adobe credential page:
   https://acrobatservices.adobe.com/dc-integration-creation-app-cdn/main.html?api=pdf-accessibility-auto-tag-api
2. Sign in, pick a credential name, and choose "Create credentials". The free tier
   has a monthly document quota; you can see usage in the Adobe Developer Console.
3. Copy the `client_id` and `client_secret` into `remediate.py setup`.

Each key is optional:
- No Adobe credentials: untagged PDFs are rejected with instructions to AutoTag them in Acrobat.
- No Claude key: figures are listed in the review file so you can type the alt text yourself.

Use `--no-ai` to skip alt text on purpose.

**Privacy:** AutoTag uploads the PDF to Adobe, and alt text sends page images to
Anthropic. For documents under an NDA, check the contract first. You can run
`--no-ai`, and use Acrobat's local AutoTag instead.

## Commands

```
python remediate.py file.pdf                      # remediate one file
python remediate.py C:\jobs\batch-12\             # every PDF in a folder
python remediate.py file.pdf --lang es-US         # non-English document
python remediate.py file.pdf --autotag            # throw away existing tags, re-tag with Adobe
python remediate.py autotag file.pdf              # only AutoTag (-> file_autotagged.pdf)
python remediate.py check file.pdf                # validate only, change nothing
python remediate.py apply file_accessible.pdf file_accessible_review.json
```

### Review loop

1. Open `*_review.json`. Each item has `old`, `value` and `decision`:
   - `"accept"` keeps or applies `value`
   - `"reject"` undoes the change or says no to a suggestion
   - `"skip"` leaves it alone and teaches the tool nothing
   - to use your own text, edit `value` and set `"accept"`

   Changes the tool already made (AI alt text, heading re-levels) default to `accept`.
   Suggestions (missed headings, decorative figures, missing alt) default to `skip`.
2. `python remediate.py apply file_accessible.pdf file_accessible_review.json`
3. `python remediate.py check file_accessible.pdf`, then PAC.

### Learning from your corrections

Every `apply` logs each decision (except `skip`) to
`%APPDATA%\pdf-remediator\learning\corrections.jsonl`. Each entry records what the
tool proposed, what you chose, whether you accepted, edited or rejected it, and
context such as the text, font size, page and figure type. Only short snippets
are stored, never the PDF.

The next run shows your most recent corrections to Claude as examples, so its
alt text and heading levels move toward your judgment. This is few-shot
prompting, not fine-tuning. It works from the first correction.

```
python remediate.py stats                        # how often you accept / edit / reject each kind of fix
python remediate.py apply x.pdf x_review.json --no-learn   # don't record this file
```

**Confidential documents:** examples from one document can appear in Claude
prompts for later documents. They only go to the Anthropic API, but if a
contract forbids that, use `--no-learn` for those files. You can also set
`use_as_examples = false` in `[learning]`.

### Measuring accuracy against your own work

```
python remediate.py eval original.pdf my_finished_version.pdf
```

This runs the tool on the original and compares the result with your finished
PDF: title, language, headings found and at the right level, figure alt
coverage, figures you artifacted that the tool kept, table header cells,
links, and lists. It lists every heading level it got wrong. Results are saved,
so `stats` shows the trend over time.

Build up 10–20 before/after pairs from your own or public documents (see the
note on confidential files above). Re-run `eval` whenever you change a setting
to check that it actually helps.

### Public benchmark (PubMed Central)

`eval` needs your own before/after PDFs. The benchmark works without them. It
uses open-access journal articles from
[PubMed Central](https://registry.opendata.aws/ncbi-pmc), where every article
comes with the publisher's PDF **and** a JATS XML file. The XML marks up the
true structure (section nesting, figures, tables), so it serves as the answer
key. Only CC BY, CC BY-SA and CC0 articles are downloaded.

```
python remediate.py benchmark fetch --n 20                     # tagged PDFs only (no Adobe quota)
python remediate.py benchmark fetch --n 12 --start PMC1175 --dir benchmark/holdout
python remediate.py benchmark run                              # headings via Claude (1 call/doc)
python remediate.py benchmark run --no-ai                      # free
python remediate.py benchmark run --alt-text                   # + alt text (1 call per figure)
```

Each article is scored twice: the publisher's PDF as it came, and the PDF
after the tool ran. Results go into each article's folder and into `stats`.

**Keep a hold-out set.** Tune rules on one folder, and check them on another
folder you never look at while tuning. The first benchmark showed why: rules
tuned on one publisher (Frontiers) scored 99.7% there, but broke correct
headings from other publishers until the rules were fixed. The held-out set also showed
Claude re-levelling *correct* publisher outlines. So when a PDF already has a valid
multi-level outline, Claude's level changes are suggestions in the review file,
not automatic edits.

Articles where most of the text is untagged are listed as "needs AutoTag" and
not scored. With Adobe credentials they get AutoTagged and scored like the rest.

**Other public datasets, and why they aren't used (yet):**

| Dataset | What it's for | Why not now |
|---|---|---|
| DocLayNet, ReadingBank, PubTabNet | Training layout, reading-order and table models | This tool asks Claude for decisions instead of training models. Worth revisiting if you add reading-order repair |
| WIT, Cooper Hewitt | Image-description training data | Wikipedia and museum-object descriptions are a different style from document alt text. Your own corrections are better examples |
| SciCap, Chart-to-Text | Chart and figure descriptions | Possible later: a chart alt-text eval scored by whether the key values are mentioned. Check licenses first (SciCap is non-commercial) |

## Automatic PDF/UA check (veraPDF)

PAC can't be run from a script, so after every run the tool validates the
finished PDF with [veraPDF](https://verapdf.org). veraPDF is the PDF Association's
open-source PDF/UA-1 validator, and it uses the same Matterhorn-based checks as
PAC. The tool fixes what it can, re-validates (up to 3 rounds), and lists anything
left under "remaining failures". The app shows the result as "PDF/UA check".

Things the tool fixes automatically: incomplete CIDSet/CharSet font data,
unnamed optional-content configurations, DisplayDocTitle, the MarkInfo
Suspects flag, and the PDF/UA identifier.

veraPDF is a little stricter than PAC. For example, it flags `.notdef` glyphs in
Acrobat's own OCR text layer, which PAC accepts. PAC stays the final sign-off.

Install: download the installer from verapdf.org and install it to
`%LOCALAPPDATA%eraPDF` (it needs Java 8+). Otherwise set `verapdf_path` under
`[validate]`, or set `verapdf = false` to skip the check.

## Config

Put a `remediator.toml` next to the script or in the working folder. See
`remediator.example.toml`. Everything is on by default.

## Layout

```
remediator/
  structure.py   struct tree read/write; keeps the ParentTree in sync (the hard part)
  content.py     MCID -> text / font size / bold / bbox, via pdfplumber
  fixes/         one module per checklist area
  checks.py      post-fix validation
  ai.py          Claude alt text (optional)
  autotag.py     Adobe PDF Services AutoTag REST client (optional)
  learning.py    correction dataset + few-shot examples
  evaluate.py    compare tool output with your hand-remediated PDF
  benchmark.py   PubMed Central answer-key benchmark
  app.py, web/   drag-and-drop app (local Flask server + review page)
  credentials.py API key storage
  apply.py       applies review decisions
tests/           synthetic "post-AutoTag" PDF with every known problem + pytest
```

`python -m pytest tests`

## Known limits

- A link is wrapped at the marked-content level. If AutoTag put a whole line into
  one MCID, the `<Link>` covers that whole line (it's still valid for PAC).
  Splitting content streams is on the roadmap.
- Decorative figures are flagged, not converted to Artifacts, because that
  needs a content-stream rewrite.
- Acrobat Pro's AutoTag and Preflight can't be scripted: AutoTag isn't exposed to Acrobat JavaScript or COM, and Preflight isn't available to scripts. Tested on Acrobat 2026, where Acrobat did accept COM calls. To tag many files at once, use Acrobat's Action Wizard, or the Adobe AutoTag API.
- Symbol, ZapfDingbats and composite (CID) fonts that aren't embedded are only reported. Use Acrobat Preflight for those.
- Forms (Widget annotations) are only checked, not fixed.
- Coordinates assume a MediaBox starting at 0,0, which is true for nearly every
  PDF. Odd crop boxes may throw off link hit-testing.

## Roadmap

1. ~~Adobe AutoTag API~~ (done)
2. **Desktop UI** (Tauri or Electron + React) around this engine: page preview, click a figure and edit its alt text, a table grid editor for TH/TD
3. ~~Learning from your corrections~~ (done, as few-shot examples). Once there are a few hundred decisions, use them to tune the thresholds automatically or train a small heading classifier
4. **Content-stream splitting** for precise link tags and figure → artifact conversion
5. **veraPDF** in `check` for full PDF/UA-1 machine validation, so you only need PAC for spot checks
6. **Watch folder**: drop PDFs into `inbox/` and get remediated files and reports in `outbox/`
7. **Per-page time log** to measure your effective $/hour on each contract
